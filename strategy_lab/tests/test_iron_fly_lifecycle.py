"""v2.1 #2 — Deterministic Iron Fly lifecycle test.

The E2E experiment-runner test only verifies wiring; it can pass with zero
trades. This test constructs the per-bar inputs such that all entry filters
provably pass, then walks the engine through signal -> fill -> exit and
verifies the trade lands in the unified ledger with a recognizable exit.

Why this is its own test: relying on random data to "happen to" produce an
Iron Fly trade leaves a gap where filter bugs silently regress to zero-trade
behavior and the wiring test still passes.
"""
from datetime import date, datetime, timedelta
from typing import List

import pytest

from src.analytics.metrics import MetricsEngine
from src.analytics.multi_leg_metrics import compute_multi_leg_summary
from src.core.engine import BarEngine
from src.core.models import BarEvent, Candle, EngineState, FeatureSnapshot
from src.execution.multi_leg_simulator import MultiLegSimulator, MODE_IDEAL
from src.execution.simulator import BacktestSimulator
from src.features.iv_regime import IVRegimeFeature
from src.feeds.option_chain_snapshot import (
    SyntheticOptionChainFeed,
    WeeklyExpiryProvider,
)
from src.strategies.iron_fly import IronFlyStrategy, PHASE_DONE


def _bar(ts: datetime, close: float, vwap: float, or_width: float = 40.0,
         or_ready: bool = True, high: float = None, low: float = None) -> BarEvent:
    high = high if high is not None else close + 1
    low = low if low is not None else close - 1
    candle = Candle(ts, 'NIFTY', close, high, low, close, 1000)
    features = FeatureSnapshot(
        session_date=ts.date(), minute_index=ts.minute,
        prior_close=close - 5, vwap=vwap,
        vwap_distance=(close - vwap) / vwap if vwap else 0,
        above_vwap=close > vwap, below_vwap=close < vwap,
        or_high=close + or_width / 2, or_low=close - or_width / 2,
        or_width=or_width, or_ready=or_ready,
        gap_pct=0, gap_direction=None,
        session_high_so_far=close + 5, session_low_so_far=close - 5,
    )
    return BarEvent(candle, features, True, 'backtest')


_CONFIG = {
    'risk': {'lot_size': {'NIFTY': 25}, 'max_total_trades_per_day': 4},
    'strategies': {
        'iron_fly': {
            'enabled': True,
            'underlyings': ['NIFTY'],
            'allowed_dte': [0, 1, 2, 3, 4],   # widened so any expiry-Thursday-day-of-week works
            'event_day_blacklist_0dte': False,
            'entry_window_start': '09:45',
            'entry_window_end': '13:30',
            'trend_filter': {'max_vwap_distance_pct': 0.0025},
            'range_filter': {
                'or_width_lookback_days': 20,
                'max_or_width_vs_median': 2.0,
                'or_history_min_days': 1,
            },
            'iv_regime_filter': {
                'lookback_days': 60,
                'min_observations': 5,
                'min_percentile': 0.1,
                'max_percentile': 0.9,
            },
            'liquidity_filter': {'max_atm_spread_pct': 0.05, 'require_two_sided_wings': True},
            'wing_width_pct_of_spot': 0.005,
            'strike_interval': {'NIFTY': 50},
            'risk_per_trade_pct': 0.005,
            'capital': 1_000_000,
            'max_lots_per_trade': 5,
            'lot_size': {'NIFTY': 25},
            'exits': {
                'touch_exit': {'enabled': True, 'distance_pct_of_wing': 1.0},
                'no_progress': {'enabled': False, 'checkpoints': []},
                'profit_target': {'enabled': True, 'pct_of_max_profit': 0.05},  # easy to hit
                'vol_expansion': {'enabled': False, 'premium_multiple_threshold': 99,
                                    'max_spot_move_pct': 0.005},
                'hard_time_stop': '15:15',
            },
            'one_trade_per_underlying_per_day': True,
        }
    }
}


def _make_engine():
    # Pre-warmed IV regime with a spread of values so percentile of 0.15 is mid-band
    iv = IVRegimeFeature(lookback_days=60, min_observations=5)
    base = datetime(2024, 1, 1, 9, 30)
    for i in range(20):
        iv.update('NIFTY', base + timedelta(days=i), 0.10 + 0.005 * i)  # 0.10..0.195

    strategy = IronFlyStrategy(iv_regime=iv, or_history_min_days=1)
    # Seed OR-width history for prior days so median is available
    for i in range(10):
        strategy._or_width_history.setdefault('NIFTY', []).append(
            (date(2024, 1, 1) + timedelta(days=i), 40.0)
        )

    feed = SyntheticOptionChainFeed(
        atm_iv=0.15,
        skew=-0.02,
        smile=0.30,
        num_strikes_each_side=20,
        strike_interval={'NIFTY': 50, 'BANKNIFTY': 100},
        # 2024-01-17 is Wednesday; expiry on Thursday 2024-01-18 -> DTE=1
        expiry_provider=WeeklyExpiryProvider(weekday=3),
    )

    engine = BarEngine(
        strategies=[strategy],
        simulator=BacktestSimulator(),
        config=_CONFIG,
        multi_leg_simulator=MultiLegSimulator(mode=MODE_IDEAL, brokerage_per_leg=0),
        chain_feed=feed,
    )
    return engine, strategy


class TestIronFlyLifecycle:
    def test_signal_fill_exit_appear_in_ledger(self):
        """Walk one trading day; assert iron fly produces one complete trade."""
        engine, strategy = _make_engine()
        sess = date(2024, 1, 17)  # Wednesday -> DTE=1 to Thursday expiry
        state = EngineState(
            instrument='NIFTY', session_date=sess,
            open_trades=[], closed_trades=[], queued_signals=[],
            per_strategy_day_trade_count={strategy.name: 0},
        )

        # Bar 1 — 09:45 — signal should be emitted (all filters set up to pass)
        engine.process_bar(_bar(datetime(2024, 1, 17, 9, 45), close=22000, vwap=22000),
                            state, 'NIFTY')
        assert len(state.queued_multi_leg_signals) == 1, "signal must be emitted at 09:45"

        # Bar 2 — 09:46 — signal fills, trade opens
        engine.process_bar(_bar(datetime(2024, 1, 17, 9, 46), close=22000, vwap=22000),
                            state, 'NIFTY')
        assert len(state.open_multi_leg_trades) == 1
        assert state.open_multi_leg_trades[0].net_entry_credit > 0

        # Bar 3 — 09:47 — same spot. Profit target threshold is 5% of max profit, but
        # ideal-mode entry mid+exit mid round-trip is zero so we need a small delta.
        # Move spot slightly toward expiry close => theta works on shorts faster than wings.
        # Easier path: advance time to 15:14, hit hard_time_stop. That guarantees exit.
        # But profit_target at 5% should also fire within an hour with theta in our favor.
        engine.process_bar(_bar(datetime(2024, 1, 17, 14, 0), close=22000, vwap=22000),
                            state, 'NIFTY')

        # By 14:00, either profit target (theta capture > 5% of max) or no other exit
        # has fired. Walk forward to hard stop to guarantee close.
        if state.open_multi_leg_trades:
            engine.process_bar(_bar(datetime(2024, 1, 17, 15, 15), close=22000, vwap=22000),
                                state, 'NIFTY')

        assert len(state.open_multi_leg_trades) == 0, "trade must be closed by EOD"
        assert len(state.closed_multi_leg_trades) == 1
        trade = state.closed_multi_leg_trades[0]
        assert trade.exit_reason is not None
        assert trade.exit_reason in {'PROFIT_TARGET', 'HARD_TIME_STOP', 'TOUCH_EXIT_CALL', 'TOUCH_EXIT_PUT'}
        assert trade.net_pnl is not None
        assert trade.gross_pnl is not None

        # Strategy state machine reached terminal phase
        st = strategy._state_for('NIFTY', sess)
        assert st.phase == PHASE_DONE

    def test_lifecycle_trade_appears_in_unified_ledger(self):
        """Same lifecycle, but verify the closed trade lands in the ledger and metrics."""
        engine, strategy = _make_engine()
        sess = date(2024, 1, 17)
        state = EngineState(
            instrument='NIFTY', session_date=sess,
            open_trades=[], closed_trades=[], queued_signals=[],
            per_strategy_day_trade_count={strategy.name: 0},
        )

        engine.process_bar(_bar(datetime(2024, 1, 17, 9, 45), close=22000, vwap=22000),
                            state, 'NIFTY')
        engine.process_bar(_bar(datetime(2024, 1, 17, 9, 46), close=22000, vwap=22000),
                            state, 'NIFTY')
        engine.process_bar(_bar(datetime(2024, 1, 17, 15, 15), close=22000, vwap=22000),
                            state, 'NIFTY')

        assert len(state.closed_multi_leg_trades) == 1

        ledger = MetricsEngine.generate_trade_ledger([], state.closed_multi_leg_trades)
        ml_rows = ledger[ledger['trade_type'] == 'multi_leg']
        assert len(ml_rows) == 1
        row = ml_rows.iloc[0]
        assert row['strategy'] == 'IRON_FLY'
        assert row['structure'] == 'IRON_FLY'
        assert row['n_legs'] == 4
        assert row['net_entry_credit'] > 0
        assert row['exit_reason'] is not None

        # And in the multi-leg summary
        summary = compute_multi_leg_summary(state.closed_multi_leg_trades)
        assert summary['total_trades'] == 1
        assert 'exits_by_reason' in summary

    def test_touch_exit_fires_when_spot_breaches_wing_intra_bar(self):
        """A bar whose high crosses touch_upper must exit, even if close retraces."""
        engine, strategy = _make_engine()
        sess = date(2024, 1, 17)
        state = EngineState(
            instrument='NIFTY', session_date=sess,
            open_trades=[], closed_trades=[], queued_signals=[],
            per_strategy_day_trade_count={strategy.name: 0},
        )

        engine.process_bar(_bar(datetime(2024, 1, 17, 9, 45), close=22000, vwap=22000),
                            state, 'NIFTY')
        engine.process_bar(_bar(datetime(2024, 1, 17, 9, 46), close=22000, vwap=22000),
                            state, 'NIFTY')
        assert len(state.open_multi_leg_trades) == 1

        # touch_upper computed at entry as ATM + wing_width = 22000 + 100 = 22100.
        # Bar with close at 22000 but high spiking to 22150 must trigger touch.
        engine.process_bar(
            _bar(datetime(2024, 1, 17, 9, 47), close=22000, vwap=22000,
                  high=22150, low=21990),
            state, 'NIFTY',
        )

        assert len(state.open_multi_leg_trades) == 0
        assert len(state.closed_multi_leg_trades) == 1
        assert state.closed_multi_leg_trades[0].exit_reason == 'TOUCH_EXIT_CALL'
