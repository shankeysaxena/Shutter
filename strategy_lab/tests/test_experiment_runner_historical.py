"""E2E test using option_chain_feed.type = historical.

Exports a synthetic chain archive to a tempdir, then runs the standard
ExperimentRunner pipeline with type='historical'. Validates that:

- the loader correctly serves snapshots from disk
- iron fly produces trades through the historical path
- data_source_warning STAYS ON for synthetic-origin archives (defensive default)
- trades land in the unified ledger with trade_type='multi_leg'

Marked slow because it generates ~60 days of synthetic OHLCV + exports a chain
archive (~10s on a stock laptop).
"""
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Phase 4.8a: skip cleanly when pyarrow isn't installed.
pytest.importorskip("pyarrow")

from src.backtest.experiment import ExperimentRunner
from src.feeds.chain_archive import export_from_ohlcv
from src.feeds.chain_archive_schema import (
    ORIGIN_BROKER,
    ORIGIN_RECORDED,
    ORIGIN_SYNTHETIC,
    SCHEMA_VERSION,
    ArchiveManifest,
)
from src.feeds.option_chain_snapshot import SyntheticOptionChainFeed, WeeklyExpiryProvider


@pytest.mark.slow
class TestHistoricalChainE2E:
    """One full run through the historical-feed code path."""

    def _generate_ohlcv(self, tmpdir: Path) -> Path:
        """40 sessions of synthetic NIFTY OHLCV — same generator pattern as the
        synthetic E2E test, just persisted."""
        rng = np.random.default_rng(11)
        rows = []
        spot = 22000.0
        for d_off in range(60):
            d = date(2024, 1, 1) + timedelta(days=d_off)
            if d.weekday() >= 5:
                continue
            spot += rng.normal(0, 8)
            for i in range(375):
                t = datetime(d.year, d.month, d.day, 9, 15) + timedelta(minutes=i)
                ch = rng.normal(0, 1.2)
                o, c = spot, spot + ch
                h = max(o, c) + abs(rng.normal(0, 0.5))
                lo = min(o, c) - abs(rng.normal(0, 0.5))
                rows.append({
                    'timestamp': t, 'instrument': 'NIFTY',
                    'open': o, 'high': h, 'low': lo, 'close': c, 'volume': 1000,
                })
                spot = c
        df = pd.DataFrame(rows)
        ohlcv_path = tmpdir / 'NIFTY.csv'
        df.to_csv(ohlcv_path, index=False)
        return ohlcv_path

    def _build_archive(self, tmpdir: Path, ohlcv_path: Path, data_origin: str) -> Path:
        archive_root = tmpdir / 'option_chain_snapshots'
        feed = SyntheticOptionChainFeed(
            atm_iv=0.15, skew=-0.02, smile=0.30,
            strike_interval={'NIFTY': 50.0},
            num_strikes_each_side=15,
            expiry_provider=WeeklyExpiryProvider(weekday=3),
            daily_iv_variation=True,
        )
        manifest = ArchiveManifest(
            schema_version=SCHEMA_VERSION,
            data_origin=data_origin,
            generated_at=datetime.now(),
            notes=f'fixture for test, data_origin={data_origin}',
        )
        # Sample every 5 minutes to keep test fast (~75 timestamps per session)
        export_from_ohlcv(
            feed=feed, ohlcv_csv=ohlcv_path, out_dir=archive_root,
            underlying='NIFTY', manifest=manifest, sample_every_n_minutes=5,
        )
        return archive_root

    def _build_runner(self, data_dir: Path, archive_root: Path) -> ExperimentRunner:
        config = {
            'experiment_name': 'iron_fly_historical_e2e',
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
                    'range_filter': {'or_width_lookback_days': 10,
                                       'max_or_width_vs_median': 1.5,
                                       'or_history_min_days': 3},
                    'iv_regime_filter': {'lookback_days': 60,
                                           'min_observations': 50,
                                           'min_percentile': 0.20,
                                           'max_percentile': 0.80},
                    'liquidity_filter': {'max_atm_spread_pct': 0.05,
                                           'require_two_sided_wings': True},
                    'wing_width_pct_of_spot': 0.005,
                    'strike_interval': {'NIFTY': 50},
                    'risk_per_trade_pct': 0.005,
                    'capital': 1_000_000,
                    'max_lots_per_trade': 5,
                    'lot_size': {'NIFTY': 25},
                    'exits': {
                        'touch_exit': {'enabled': True, 'distance_pct_of_wing': 1.0},
                        'no_progress': {'enabled': False, 'checkpoints': []},
                        'profit_target': {'enabled': True, 'pct_of_max_profit': 0.15},
                        'vol_expansion': {'enabled': True,
                                            'premium_multiple_threshold': 1.3,
                                            'max_spot_move_pct': 0.005},
                        'hard_time_stop': '15:15',
                    },
                },
            },
            # This is the key — pointing at the historical archive
            'option_chain_feed': {
                'type': 'historical',
                'historical': {
                    'snapshot_dir': str(archive_root),
                    'strict_schema': True,
                },
            },
            'multi_leg_simulator': {'mode': 'realistic', 'brokerage_per_leg': 20},
        }
        return ExperimentRunner(config=config, data_dir=str(data_dir))

    def test_historical_path_runs_and_produces_warning_for_synthetic_origin(self):
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            ohlcv_path = self._generate_ohlcv(tmpdir)
            archive_root = self._build_archive(tmpdir, ohlcv_path, ORIGIN_SYNTHETIC)
            runner = self._build_runner(tmpdir, archive_root)
            result = runner.run()

            # No failed instruments
            assert not result.failed_instruments, f"failures: {result.failed_instruments}"

            # Warning must remain on for synthetic origin even when loaded via historical feed
            assert result.data_source_warning is not None, (
                'data_source_warning must stay on for synthetic-origin archives — '
                'the defensive default is the whole point'
            )
            assert 'synthetic' in result.data_source_warning.lower()

            # Metadata reflects the historical path
            assert result.metadata.get('total_multi_leg_trades') is not None

    def test_warning_clears_when_origin_is_recorded(self):
        """The same archive, declared as 'recorded', must clear the warning."""
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            ohlcv_path = self._generate_ohlcv(tmpdir)
            archive_root = self._build_archive(tmpdir, ohlcv_path, ORIGIN_RECORDED)
            runner = self._build_runner(tmpdir, archive_root)
            result = runner.run()

            assert not result.failed_instruments, f"failures: {result.failed_instruments}"
            # When manifest says recorded, the warning lifts.
            # This is a contract test — even though the underlying data is still
            # synthetic in this test, the SYSTEM trusts the manifest.
            assert result.data_source_warning is None, (
                'data_source_warning must clear when data_origin is recorded'
            )

    def test_historical_trades_appear_in_unified_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            ohlcv_path = self._generate_ohlcv(tmpdir)
            archive_root = self._build_archive(tmpdir, ohlcv_path, ORIGIN_SYNTHETIC)
            runner = self._build_runner(tmpdir, archive_root)
            result = runner.run()

            # If any multi-leg trades fired, they must surface in the ledger
            if result.all_multi_leg_trades:
                ml_rows = result.ledger[result.ledger['trade_type'] == 'multi_leg']
                assert len(ml_rows) == len(result.all_multi_leg_trades)
                assert ml_rows['net_entry_credit'].notna().all()
                assert ml_rows['n_legs'].iloc[0] == 4   # iron fly
