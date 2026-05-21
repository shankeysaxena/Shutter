"""
ExperimentRunner — owns the full run loop for one experiment.

Responsibilities:
- Load and sessionize data
- Enforce session validation policy (warn / fail / skip)
- Apply feature pipeline
- Run backtest or replay runtime
- Return a structured ExperimentResult

ReportWriter owns persistence. main.py owns CLI and printing.
"""
import hashlib
import os
import yaml
from dataclasses import dataclass, field
from datetime import datetime, date, time
from typing import Dict, List, Optional, Set

import pandas as pd

from src.data.loader import DataLoader
from src.data.sessionizer import Sessionizer
from src.features.vwap import VWAPFeature
from src.features.opening_range import OpeningRangeFeature
from src.features.gap import GapFeature
from src.features.intraday import IntradaySessionFeature
from src.features.atr import IntradayATRFeature
from src.strategies.orb import ORBStrategy
from src.strategies.vwap_pullback import VWAPPullbackStrategy
from src.strategies.gap_behavior import GapBehaviorStrategy
from src.strategies.iron_fly import IronFlyStrategy
from src.strategies.vwap_reversion import VWAPReversionStrategy
from src.strategies.or_failure_fade import ORFailureFadeStrategy
from src.strategies.compression_breakout import CompressionBreakoutStrategy
from src.execution.simulator import BacktestSimulator
from src.execution.multi_leg_simulator import MultiLegSimulator
from src.runtimes.backtest import BacktestRuntime
from src.runtimes.replay import ReplayRuntime
from src.feeds.replay_feed import ReplayFeed
from src.feeds.option_chain_snapshot import (
    OptionChainFeed,
    SyntheticOptionChainFeed,
    HistoricalOptionChainFeed,
    WeeklyExpiryProvider,
)
from src.analytics.metrics import MetricsEngine
from src.analytics.multi_leg_metrics import compute_multi_leg_summary
from src.core.models import Trade
from src.core.option_models import MultiLegTrade

_VALID_POLICIES = ('warn', 'fail', 'skip')


@dataclass
class ExperimentResult:
    experiment_name: str
    runtime_mode: str
    instruments: List[str]
    all_trades: List[Trade]
    ledger: pd.DataFrame
    summary: Dict
    session_warnings: Dict[str, Dict]  # instrument -> {date_str -> [issues]}
    failed_instruments: Dict[str, str]
    event_log: List[Dict]
    rejections: Dict        # {strategy_name: {reason: count}} — from event_log
    metadata: Dict
    # v2 additions — kept default-empty so old callers don't need to change
    all_multi_leg_trades: List[MultiLegTrade] = field(default_factory=list)
    # v2 §13 — set when an options strategy ran against a synthetic chain feed.
    # Reports and console output must surface this so synthetic P&L is never
    # mistaken for edge evidence.
    data_source_warning: Optional[str] = None


class ExperimentRunner:
    """
    Runs a full experiment from a loaded config dict.
    Returns an ExperimentResult — does not print or write files.
    """

    def __init__(self, config: dict, data_dir: str):
        self.config = config
        self.data_dir = data_dir

    def run(self) -> ExperimentResult:
        config = self.config
        runtime_mode = config['runtime']['mode']
        sv_cfg = config.get('session_validation', {})
        policy = sv_cfg.get('policy', 'warn')
        if policy not in _VALID_POLICIES:
            raise ValueError(f"session_validation.policy must be one of {_VALID_POLICIES}, got {policy!r}")

        loader = DataLoader(data_dir=self.data_dir)
        sessionizer = Sessionizer()
        features = _build_features(config)
        strategies = _build_strategies(config)

        simulator = BacktestSimulator(
            slippage_per_side=config['costs']['slippage_per_side'],
            brokerage=config['costs']['brokerage_per_trade'],
        )

        # Options-aware wiring: build chain feed + multi-leg simulator only if
        # any options strategy is enabled. Otherwise these stay None and the
        # single-leg path is unaffected.
        options_enabled = any(isinstance(s, IronFlyStrategy) for s in strategies)
        chain_feed = _build_chain_feed(config) if options_enabled else None
        ml_simulator = _build_multi_leg_simulator(config) if options_enabled else None

        all_trades: List[Trade] = []
        all_multi_leg_trades: List[MultiLegTrade] = []
        failed_instruments: Dict[str, str] = {}
        all_session_warnings: Dict[str, Dict] = {}
        event_log: List[Dict] = []
        total_sessions_processed = 0

        for instrument in config['instruments']:
            try:
                df = loader.load_historical_data(
                    instrument=instrument,
                    start_date=config.get('date_range', {}).get('start'),
                    end_date=config.get('date_range', {}).get('end'),
                )
                sessions = sessionizer.create_sessions(df)

                # Session quality validation with policy enforcement
                if sv_cfg.get('enabled', True):
                    warnings_by_date = sessionizer.validate_sessions_detailed(
                        sessions,
                        expected_bars_min=sv_cfg.get('min_bars', 300),
                        expected_bars_max=sv_cfg.get('max_bars', 376),
                        warn_missing_minutes=sv_cfg.get('warn_missing_minutes', True),
                    )
                    if warnings_by_date:
                        all_session_warnings[instrument] = {
                            str(dt): issues for dt, issues in warnings_by_date.items()
                        }
                        if policy == 'fail':
                            n = sum(len(v) for v in warnings_by_date.values())
                            raise ValueError(
                                f"session_validation policy=fail: {n} issue(s) for {instrument} "
                                f"on dates: {sorted(str(d) for d in warnings_by_date)}"
                            )
                        elif policy == 'skip':
                            sessions = {
                                dt: s for dt, s in sessions.items()
                                if dt not in warnings_by_date
                            }

                # Apply feature pipeline
                sessions = {
                    dt: _apply_features(s, features) for dt, s in sessions.items()
                }
                total_sessions_processed += len(sessions)

                if runtime_mode == 'backtest':
                    runtime = BacktestRuntime(
                        strategies=strategies, simulator=simulator, config=config,
                        multi_leg_simulator=ml_simulator, chain_feed=chain_feed,
                    )
                    for session_date_, session_df in sessions.items():
                        sl_trades, ml_trades, logs = runtime.run_session_full(
                            instrument, session_date_, session_df
                        )
                        all_trades.extend(sl_trades)
                        all_multi_leg_trades.extend(ml_trades)
                        event_log.extend(logs)

                elif runtime_mode == 'replay':
                    runtime = ReplayRuntime(
                        strategies=strategies, simulator=simulator, config=config,
                        multi_leg_simulator=ml_simulator, chain_feed=chain_feed,
                    )
                    feed = ReplayFeed(sessions=sessions, instrument=instrument)
                    sl_trades, ml_trades, logs = runtime.run_full(feed)
                    all_trades.extend(sl_trades)
                    all_multi_leg_trades.extend(ml_trades)
                    event_log.extend(logs)

            except Exception as e:
                failed_instruments[instrument] = str(e)

        ledger = MetricsEngine.generate_trade_ledger(all_trades, all_multi_leg_trades)
        summary = MetricsEngine.calculate_summary(ledger)
        if all_multi_leg_trades:
            summary['multi_leg'] = compute_multi_leg_summary(all_multi_leg_trades)

        # v2 §13 / Phase 4.8a: surface a data-source warning unless the chain
        # feed declares 'recorded' or 'broker' origin. Defensive default keeps
        # the warning on for synthetic feeds and for historical archives whose
        # manifest hasn't been explicitly marked as real-data.
        data_source_warning: Optional[str] = None
        if options_enabled and chain_feed is not None:
            origin = chain_feed.data_origin
            if origin not in ('recorded', 'broker'):
                data_source_warning = (
                    f"DATA_ORIGIN='{origin}' — multi-leg P&L is not valid for edge "
                    f"claims. Synthetic / unknown-origin chains lack the skew kinks, "
                    f"liquidity premia, and event-day vol shocks that determine real "
                    f"Iron Fly outcomes. See Iron_Fly_Strategy_Spec_v2.md §13."
                )
                summary['data_source_warning'] = data_source_warning

        rejections = MetricsEngine.aggregate_rejections(event_log)
        metadata = _build_metadata(
            config, all_trades, all_session_warnings, total_sessions_processed,
            all_multi_leg_trades,
        )

        return ExperimentResult(
            experiment_name=config.get('experiment_name', 'unknown'),
            runtime_mode=runtime_mode,
            instruments=config['instruments'],
            all_trades=all_trades,
            all_multi_leg_trades=all_multi_leg_trades,
            ledger=ledger,
            summary=summary,
            session_warnings=all_session_warnings,
            failed_instruments=failed_instruments,
            event_log=event_log,
            rejections=rejections,
            metadata=metadata,
            data_source_warning=data_source_warning,
        )


# --- Module-level helpers (not methods, easier to test) ---

def _build_features(config: dict) -> list:
    orb_cfg = config.get('strategies', {}).get('orb', {})
    h, m = map(int, orb_cfg.get('opening_range_start', '09:15').split(':'))
    or_start = time(h, m)
    h, m = map(int, orb_cfg.get('opening_range_end', '09:30').split(':'))
    or_end = time(h, m)
    return [
        VWAPFeature(),
        OpeningRangeFeature(start_time=or_start, end_time=or_end),
        GapFeature(),
        IntradaySessionFeature(),
        IntradayATRFeature(period=14),
    ]


def _build_strategies(config: dict) -> list:
    strats_cfg = config.get('strategies', {})
    strategies = []
    if strats_cfg.get('orb', {}).get('enabled', False):
        strategies.append(ORBStrategy())
    if strats_cfg.get('vwap_pullback', {}).get('enabled', False):
        strategies.append(VWAPPullbackStrategy())
    if strats_cfg.get('gap_behavior', {}).get('enabled', False):
        strategies.append(GapBehaviorStrategy())
    # v2 §11: options strategies are off by default
    if strats_cfg.get('vwap_reversion', {}).get('enabled', False):
        strategies.append(VWAPReversionStrategy())
    if strats_cfg.get('or_failure_fade', {}).get('enabled', False):
        strategies.append(ORFailureFadeStrategy())
    if strats_cfg.get('compression_breakout', {}).get('enabled', False):
        strategies.append(CompressionBreakoutStrategy())
    if strats_cfg.get('iron_fly', {}).get('enabled', False):
        ifly_cfg = strats_cfg.get('iron_fly', {})
        event_days = _load_event_days(ifly_cfg.get('event_days_file'))
        # v2.1 #3: wire IV regime warmup from config so unexpected blocks are visible.
        # Strategy hardcoded these to 60d / 500 obs before; now they're tunable.
        from src.features.iv_regime import IVRegimeFeature
        iv_cfg = ifly_cfg.get('iv_regime_filter', {})
        iv_regime = IVRegimeFeature(
            lookback_days=iv_cfg.get('lookback_days', 60),
            min_observations=iv_cfg.get('min_observations', 500),
        )
        or_min_days = ifly_cfg.get('range_filter', {}).get('or_history_min_days', 5)
        strategies.append(IronFlyStrategy(
            event_days=event_days,
            iv_regime=iv_regime,
            or_history_min_days=or_min_days,
        ))
    return strategies


def _build_chain_feed(config: dict) -> Optional[OptionChainFeed]:
    cfg = config.get('option_chain_feed', {})
    feed_type = cfg.get('type', 'synthetic')
    if feed_type == 'synthetic':
        sc = cfg.get('synthetic', {})
        return SyntheticOptionChainFeed(
            atm_iv=sc.get('atm_iv', 0.15),
            skew=sc.get('skew', -0.02),
            smile=sc.get('smile', 0.30),
            risk_free_rate=sc.get('risk_free_rate', 0.07),
            strike_interval=sc.get('strike_interval', {'NIFTY': 50, 'BANKNIFTY': 100}),
            num_strikes_each_side=sc.get('num_strikes_each_side', 20),
            spread_pct=sc.get('spread_pct', 0.01),
            min_spread=sc.get('min_spread', 0.5),
            expiry_provider=WeeklyExpiryProvider(weekday=sc.get('expiry_weekday', 3)),
            daily_iv_variation=sc.get('daily_iv_variation', False),
        )
    if feed_type == 'historical':
        hist_cfg = cfg.get('historical', {})
        return HistoricalOptionChainFeed(
            snapshot_dir=hist_cfg.get('snapshot_dir', 'data/option_chain_snapshots'),
            strict_schema=hist_cfg.get('strict_schema', True),
        )
    raise ValueError(f"Unknown option_chain_feed.type: {feed_type!r}")


def _build_multi_leg_simulator(config: dict) -> MultiLegSimulator:
    cfg = config.get('multi_leg_simulator', {})
    return MultiLegSimulator(
        mode=cfg.get('mode', 'realistic'),
        tick_size=cfg.get('tick_size', 0.05),
        brokerage_per_leg=cfg.get('brokerage_per_leg', 20.0),
    )


def _load_event_days(path: Optional[str]) -> Set[date]:
    """One ISO date per line, no header. Missing file = empty set (advisory)."""
    if not path or not os.path.exists(path):
        return set()
    days: Set[date] = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                days.add(date.fromisoformat(line))
    return days


def _apply_features(session_df: pd.DataFrame, features: list) -> pd.DataFrame:
    for feature in features:
        session_df = feature.calculate(session_df)
    return session_df


def _build_metadata(
    config: dict,
    all_trades: List[Trade],
    all_session_warnings: Dict,
    total_sessions_processed: int,
    all_multi_leg_trades: Optional[List[MultiLegTrade]] = None,
) -> Dict:
    config_hash = 'sha256:' + hashlib.sha256(
        yaml.dump(config, sort_keys=True).encode()
    ).hexdigest()[:16]

    strategies_enabled = [
        name for name, cfg in config.get('strategies', {}).items()
        if cfg.get('enabled', False)
    ]
    sessions_with_warnings = sum(len(by_date) for by_date in all_session_warnings.values())
    total_warning_count = sum(
        sum(len(v) for v in by_date.values())
        for by_date in all_session_warnings.values()
    )
    return {
        'experiment_name': config.get('experiment_name', 'unknown'),
        'run_timestamp': datetime.now().isoformat(timespec='seconds'),
        'runtime_mode': config['runtime']['mode'],
        'instruments': config['instruments'],
        'date_range': config.get('date_range', {}),
        'config_hash': config_hash,
        'strategies_enabled': strategies_enabled,
        'total_trades': len(all_trades),
        'total_multi_leg_trades': len(all_multi_leg_trades) if all_multi_leg_trades else 0,
        'total_sessions_processed': total_sessions_processed,
        'sessions_with_warnings': sessions_with_warnings,
        'total_session_warning_count': total_warning_count,
        'session_validation_policy': config.get('session_validation', {}).get('policy', 'warn'),
    }
