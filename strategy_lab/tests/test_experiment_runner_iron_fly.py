"""Tests for v2 §11–§13 — Iron Fly wired through ExperimentRunner end-to-end."""
import os
import tempfile

import pytest

from src.backtest.experiment import (
    _build_strategies,
    _build_chain_feed,
    _build_multi_leg_simulator,
    _load_event_days,
)
from src.feeds.option_chain_snapshot import (
    SyntheticOptionChainFeed,
    HistoricalOptionChainFeed,
)
from src.execution.multi_leg_simulator import MultiLegSimulator
from src.strategies.iron_fly import IronFlyStrategy
from src.strategies.orb import ORBStrategy


class TestBuildStrategies:
    def test_iron_fly_skipped_when_disabled(self):
        cfg = {'strategies': {
            'orb': {'enabled': True},
            'iron_fly': {'enabled': False},
        }}
        names = [s.name for s in _build_strategies(cfg)]
        assert 'IRON_FLY' not in names
        assert 'ORB' in names

    def test_iron_fly_included_when_enabled(self):
        cfg = {'strategies': {
            'orb': {'enabled': False},
            'iron_fly': {'enabled': True},
        }}
        names = [s.name for s in _build_strategies(cfg)]
        assert names == ['IRON_FLY']

    def test_iron_fly_off_by_default(self):
        """If `enabled` key is missing, iron_fly should not be built."""
        cfg = {'strategies': {'iron_fly': {}}}
        names = [s.name for s in _build_strategies(cfg)]
        assert 'IRON_FLY' not in names


class TestBuildChainFeed:
    def test_synthetic_is_default(self):
        cfg = {'option_chain_feed': {'synthetic': {'atm_iv': 0.18}}}
        feed = _build_chain_feed(cfg)
        assert isinstance(feed, SyntheticOptionChainFeed)
        assert feed.atm_iv == 0.18

    def test_synthetic_explicit_type(self):
        cfg = {'option_chain_feed': {'type': 'synthetic', 'synthetic': {}}}
        feed = _build_chain_feed(cfg)
        assert isinstance(feed, SyntheticOptionChainFeed)

    def test_historical(self):
        pytest.importorskip("pyarrow")   # HistoricalOptionChainFeed.__init__ requires pyarrow
        cfg = {'option_chain_feed': {
            'type': 'historical',
            'historical': {'snapshot_dir': '/tmp/snaps'},
        }}
        feed = _build_chain_feed(cfg)
        assert isinstance(feed, HistoricalOptionChainFeed)

    def test_unknown_type_raises(self):
        cfg = {'option_chain_feed': {'type': 'wishful_thinking'}}
        with pytest.raises(ValueError, match='Unknown'):
            _build_chain_feed(cfg)


class TestBuildMultiLegSimulator:
    def test_defaults_to_realistic(self):
        sim = _build_multi_leg_simulator({})
        assert isinstance(sim, MultiLegSimulator)
        assert sim.mode == 'realistic'

    def test_mode_override(self):
        sim = _build_multi_leg_simulator({'multi_leg_simulator': {'mode': 'ideal'}})
        assert sim.mode == 'ideal'

    def test_brokerage_override(self):
        sim = _build_multi_leg_simulator(
            {'multi_leg_simulator': {'brokerage_per_leg': 50}}
        )
        assert sim.brokerage_per_leg == 50


class TestLoadEventDays:
    def test_missing_path_returns_empty(self):
        assert _load_event_days(None) == set()
        assert _load_event_days('/no/such/path.csv') == set()

    def test_loads_dates_from_file(self):
        with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False) as f:
            f.write("2024-04-05\n# comment\n2024-06-19\n\n2024-09-18\n")
            path = f.name
        try:
            from datetime import date
            days = _load_event_days(path)
            assert days == {date(2024, 4, 5), date(2024, 6, 19), date(2024, 9, 18)}
        finally:
            os.unlink(path)


@pytest.mark.slow
class TestEndToEndIronFlyExperiment:
    """One full ExperimentRunner.run() against a small generated dataset.
    Verifies that multi-leg trades surface in result + ledger + summary, and
    that the synthetic-chain warning fires.

    Marked slow: spins up ~9000 bars of synthetic data and runs the full
    feature + strategy pipeline; takes ~10s. Run with: pytest -m slow"""

    def _make_runner(self):
        import pandas as pd
        from datetime import datetime, timedelta
        import numpy as np
        import tempfile, os
        from src.backtest.experiment import ExperimentRunner

        # Generate 40 trading days of NIFTY data
        rng = np.random.default_rng(7)
        rows = []
        spot = 22000.0
        for d_off in range(60):
            d = datetime(2024, 1, 1) + timedelta(days=d_off)
            if d.weekday() >= 5:
                continue
            spot += rng.normal(0, 8)
            for i in range(375):
                t = datetime(d.year, d.month, d.day, 9, 15) + timedelta(minutes=i)
                ch = rng.normal(0, 1.2)
                o, c = spot, spot + ch
                h = max(o, c) + abs(rng.normal(0, 0.5))
                lo = min(o, c) - abs(rng.normal(0, 0.5))
                rows.append({'timestamp': t, 'instrument': 'NIFTY',
                             'open': o, 'high': h, 'low': lo, 'close': c, 'volume': 1000})
                spot = c
        df = pd.DataFrame(rows)

        tmpdir = tempfile.mkdtemp()
        df.to_csv(os.path.join(tmpdir, 'NIFTY.csv'), index=False)

        config = {
            'experiment_name': 'iron_fly_e2e_test',
            'runtime': {'mode': 'backtest'},
            'instruments': ['NIFTY'],
            'date_range': {'start': '2024-01-01', 'end': '2024-03-31'},
            'costs': {'slippage_per_side': 0, 'brokerage_per_trade': 20},
            'risk': {'mode': 'fixed_lot', 'lot_size': {'NIFTY': 25},
                     'max_total_trades_per_day': 4},
            'session_validation': {'enabled': False, 'policy': 'warn'},
            'strategies': {
                'iron_fly': {
                    'enabled': True,
                    'underlyings': ['NIFTY'],
                    'allowed_dte': [0, 1, 2, 3],
                    'event_day_blacklist_0dte': False,
                    'entry_window_start': '09:45',
                    'entry_window_end': '13:30',
                    'trend_filter': {'max_vwap_distance_pct': 0.0025},
                    'range_filter': {'or_width_lookback_days': 10, 'max_or_width_vs_median': 1.5},
                    'iv_regime_filter': {'lookback_days': 60, 'min_percentile': 0.20, 'max_percentile': 0.80},
                    'liquidity_filter': {'max_atm_spread_pct': 0.05, 'require_two_sided_wings': True},
                    'wing_width_pct_of_spot': 0.005,
                    'strike_interval': {'NIFTY': 50},
                    'risk_per_trade_pct': 0.005,
                    'capital': 1000000,
                    'max_lots_per_trade': 5,
                    'lot_size': {'NIFTY': 25},
                    'exits': {
                        'touch_exit': {'enabled': True, 'distance_pct_of_wing': 1.0},
                        'no_progress': {'enabled': False, 'checkpoints': []},
                        'profit_target': {'enabled': True, 'pct_of_max_profit': 0.15},
                        'vol_expansion': {'enabled': True, 'premium_multiple_threshold': 1.3,
                                            'max_spot_move_pct': 0.005},
                        'hard_time_stop': '15:15',
                    },
                },
            },
            'option_chain_feed': {
                'type': 'synthetic',
                'synthetic': {'atm_iv': 0.15, 'daily_iv_variation': False, 'expiry_weekday': 3,
                                'strike_interval': {'NIFTY': 50}},
            },
            'multi_leg_simulator': {'mode': 'realistic', 'brokerage_per_leg': 20},
        }
        return ExperimentRunner(config=config, data_dir=tmpdir)

    def test_runs_without_error_and_emits_warning(self):
        runner = self._make_runner()
        result = runner.run()
        # Whether trades fire on this data is incidental; the warning must always appear
        assert result.data_source_warning is not None
        # Check the source-of-truth wording: data_origin tag + edge-claims caveat.
        # Wording was tightened in Phase 4.8a; loosened assertion to track meaning, not exact text.
        assert 'synthetic' in result.data_source_warning.lower()
        assert 'not valid for edge claims' in result.data_source_warning.lower()
        assert result.metadata.get('total_multi_leg_trades') is not None

    def test_unified_ledger_distinguishes_trade_types(self):
        runner = self._make_runner()
        result = runner.run()
        # If any multi-leg trades exist, they must show up in the unified ledger
        if result.all_multi_leg_trades:
            assert not result.ledger.empty
            ml_rows = result.ledger[result.ledger['trade_type'] == 'multi_leg']
            assert len(ml_rows) == len(result.all_multi_leg_trades)
            # Multi-leg rows carry net_entry_credit; single-leg rows do not
            assert ml_rows['net_entry_credit'].notna().all()
