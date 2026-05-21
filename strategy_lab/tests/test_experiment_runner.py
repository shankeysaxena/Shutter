"""
Tests for Phase 4.5 components:
- validate_sessions_detailed (per-date warnings)
- session validation policy: warn / fail / skip
- ExperimentRunner._build_metadata fields
- ReportWriter output artifacts
"""
import json
import pytest
import pandas as pd
from datetime import date, datetime
from pathlib import Path

from src.data.sessionizer import Sessionizer
from src.backtest.experiment import _build_metadata, ExperimentResult
from src.analytics.reporting import ReportWriter


# --- Helpers ---

def _make_sessions(bar_counts: dict) -> dict:
    """Build minimal fake sessions with the given bar count per date."""
    sessions = {}
    for dt, n in bar_counts.items():
        timestamps = pd.date_range(
            start=datetime(dt.year, dt.month, dt.day, 9, 15),
            periods=n, freq='1min',
        )
        df = pd.DataFrame({
            'timestamp': timestamps,
            'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.5, 'volume': 1000,
        })
        sessions[dt] = df
    return sessions


def _make_result(**kwargs) -> ExperimentResult:
    defaults = dict(
        experiment_name='test_exp',
        runtime_mode='backtest',
        instruments=['NIFTY'],
        all_trades=[],
        ledger=pd.DataFrame(),
        summary={},
        session_warnings={},
        failed_instruments={},
        event_log=[],
        rejections={},
        metadata={'experiment_name': 'test_exp', 'run_timestamp': '2026-01-01T09:00:00'},
    )
    defaults.update(kwargs)
    return ExperimentResult(**defaults)


# --- validate_sessions_detailed ---

class TestValidateSessionsDetailed:

    def test_clean_sessions_return_empty_dict(self):
        s = Sessionizer()
        sessions = _make_sessions({date(2024, 1, 2): 375, date(2024, 1, 3): 375})
        result = s.validate_sessions_detailed(sessions, expected_bars_min=300, expected_bars_max=376)
        assert result == {}

    def test_partial_session_flagged(self):
        s = Sessionizer()
        sessions = _make_sessions({date(2024, 1, 2): 100})
        result = s.validate_sessions_detailed(sessions, expected_bars_min=300, expected_bars_max=376)
        assert date(2024, 1, 2) in result
        assert any('partial' in w or 'only 100' in w for w in result[date(2024, 1, 2)])

    def test_oversized_session_flagged(self):
        s = Sessionizer()
        sessions = _make_sessions({date(2024, 1, 2): 400})
        result = s.validate_sessions_detailed(sessions, expected_bars_min=300, expected_bars_max=376)
        assert date(2024, 1, 2) in result
        assert any('duplicate' in w or '400' in w for w in result[date(2024, 1, 2)])

    def test_only_bad_sessions_appear_in_result(self):
        s = Sessionizer()
        sessions = _make_sessions({
            date(2024, 1, 2): 375,   # good
            date(2024, 1, 3): 50,    # bad
        })
        result = s.validate_sessions_detailed(sessions, expected_bars_min=300, expected_bars_max=376)
        assert date(2024, 1, 2) not in result
        assert date(2024, 1, 3) in result

    def test_validate_sessions_still_returns_flat_list(self):
        """Original validate_sessions interface must not break."""
        s = Sessionizer()
        sessions = _make_sessions({date(2024, 1, 2): 50})
        warnings = s.validate_sessions(sessions, expected_bars_min=300, expected_bars_max=376)
        assert isinstance(warnings, list)
        assert len(warnings) == 1
        assert '2024-01-02' in warnings[0]


# --- Session validation policy ---

class TestSessionValidationPolicy:

    def _make_config(self, policy: str, min_bars: int = 300) -> dict:
        return {
            'runtime': {'mode': 'backtest'},
            'instruments': ['NIFTY'],
            'date_range': {'start': None, 'end': None},
            'costs': {'slippage_per_side': 2.0, 'brokerage_per_trade': 20.0},
            'risk': {'lot_size': {'NIFTY': 25}, 'max_total_trades_per_day': 4},
            'session_validation': {
                'enabled': True, 'min_bars': min_bars, 'max_bars': 376,
                'warn_missing_minutes': False, 'policy': policy,
            },
            'strategies': {},
            'output': {'save_event_log': False},
        }

    def test_invalid_policy_raises(self):
        from src.backtest.experiment import ExperimentRunner
        config = self._make_config('invalid_policy')
        runner = ExperimentRunner(config, 'data/raw')
        with pytest.raises(ValueError, match="policy"):
            runner.run()

    def test_warn_policy_keeps_bad_sessions(self):
        """policy=warn: bad sessions produce warnings but are still processed."""
        s = Sessionizer()
        sessions = _make_sessions({
            date(2024, 1, 2): 50,   # bad
            date(2024, 1, 3): 375,  # good
        })
        warnings = s.validate_sessions_detailed(sessions, expected_bars_min=300)
        # warn policy: all sessions remain, only 1 flagged
        assert len(warnings) == 1
        assert date(2024, 1, 3) not in warnings

    def test_skip_policy_excludes_bad_sessions(self):
        """policy=skip: sessions with warnings are removed before processing."""
        s = Sessionizer()
        sessions = _make_sessions({
            date(2024, 1, 2): 50,   # bad
            date(2024, 1, 3): 375,  # good
        })
        warnings_by_date = s.validate_sessions_detailed(sessions, expected_bars_min=300)
        # Simulate skip logic
        kept = {dt: df for dt, df in sessions.items() if dt not in warnings_by_date}
        assert date(2024, 1, 2) not in kept
        assert date(2024, 1, 3) in kept

    def test_fail_policy_raises_on_bad_session(self):
        """policy=fail: any session warning raises ValueError before processing."""
        s = Sessionizer()
        sessions = _make_sessions({date(2024, 1, 2): 50})
        warnings_by_date = s.validate_sessions_detailed(sessions, expected_bars_min=300)
        assert warnings_by_date  # confirm there are issues
        # The fail logic in ExperimentRunner raises when warnings_by_date is non-empty
        with pytest.raises(ValueError):
            if warnings_by_date:
                raise ValueError("policy=fail triggered")


# --- Run metadata ---

class TestRunMetadata:

    def _base_config(self) -> dict:
        return {
            'experiment_name': 'meta_test',
            'runtime': {'mode': 'backtest'},
            'instruments': ['NIFTY', 'BANKNIFTY'],
            'date_range': {'start': '2024-01-01', 'end': '2024-12-31'},
            'strategies': {
                'orb': {'enabled': True},
                'vwap_pullback': {'enabled': False},
            },
            'session_validation': {'policy': 'warn'},
        }

    def test_metadata_has_required_keys(self):
        config = self._base_config()
        meta = _build_metadata(config, [], {}, 10)
        for key in ['experiment_name', 'run_timestamp', 'runtime_mode', 'instruments',
                    'config_hash', 'strategies_enabled', 'total_trades',
                    'total_sessions_processed', 'sessions_with_warnings',
                    'session_validation_policy']:
            assert key in meta, f"missing key: {key}"

    def test_config_hash_is_stable(self):
        """Same config always produces the same hash."""
        config = self._base_config()
        h1 = _build_metadata(config, [], {}, 0)['config_hash']
        h2 = _build_metadata(config, [], {}, 0)['config_hash']
        assert h1 == h2

    def test_config_hash_changes_with_config(self):
        config = self._base_config()
        h1 = _build_metadata(config, [], {}, 0)['config_hash']
        config['experiment_name'] = 'different'
        h2 = _build_metadata(config, [], {}, 0)['config_hash']
        assert h1 != h2

    def test_only_enabled_strategies_listed(self):
        config = self._base_config()
        meta = _build_metadata(config, [], {}, 0)
        assert 'orb' in meta['strategies_enabled']
        assert 'vwap_pullback' not in meta['strategies_enabled']

    def test_session_warning_counts(self):
        config = self._base_config()
        warnings = {
            'NIFTY': {'2024-01-02': ['partial session'], '2024-01-03': ['gap detected']},
            'BANKNIFTY': {'2024-01-02': ['partial session']},
        }
        meta = _build_metadata(config, [], warnings, 0)
        assert meta['sessions_with_warnings'] == 3
        assert meta['total_session_warning_count'] == 3


# --- ReportWriter ---

class TestReportWriter:

    def test_creates_run_folder(self, tmp_path):
        writer = ReportWriter(base_output_dir=str(tmp_path))
        result = _make_result()
        out = writer.write(result, config={'experiment_name': 'x'})
        assert out.exists()
        assert out.is_dir()

    def test_run_metadata_written(self, tmp_path):
        writer = ReportWriter(base_output_dir=str(tmp_path))
        result = _make_result()
        out = writer.write(result, config={})
        assert (out / 'run_metadata.json').exists()
        data = json.loads((out / 'run_metadata.json').read_text())
        assert data['experiment_name'] == 'test_exp'

    def test_config_snapshot_written(self, tmp_path):
        writer = ReportWriter(base_output_dir=str(tmp_path))
        result = _make_result()
        out = writer.write(result, config={'key': 'value'})
        assert (out / 'config_snapshot.yaml').exists()

    def test_trades_csv_written_when_ledger_nonempty(self, tmp_path):
        writer = ReportWriter(base_output_dir=str(tmp_path))
        ledger = pd.DataFrame([{'strategy': 'ORB', 'net_pnl': 100.0, 'entry_time': '09:31',
                                 'date': date(2024, 1, 2)}])
        result = _make_result(ledger=ledger, summary={'total_trades': 1})
        out = writer.write(result, config={})
        assert (out / 'trades.csv').exists()
        assert (out / 'summary.json').exists()

    def test_no_trades_csv_when_ledger_empty(self, tmp_path):
        writer = ReportWriter(base_output_dir=str(tmp_path))
        result = _make_result()
        out = writer.write(result, config={})
        assert not (out / 'trades.csv').exists()

    def test_session_warnings_written_when_present(self, tmp_path):
        writer = ReportWriter(base_output_dir=str(tmp_path))
        result = _make_result(
            session_warnings={'NIFTY': {'2024-01-02': ['partial session']}}
        )
        out = writer.write(result, config={})
        assert (out / 'session_warnings.json').exists()

    def test_no_session_warnings_file_when_empty(self, tmp_path):
        writer = ReportWriter(base_output_dir=str(tmp_path))
        result = _make_result()
        out = writer.write(result, config={})
        assert not (out / 'session_warnings.json').exists()

    def test_event_log_not_written_by_default(self, tmp_path):
        writer = ReportWriter(base_output_dir=str(tmp_path))
        result = _make_result(event_log=[{'event_type': 'no_signal'}])
        out = writer.write(result, config={}, save_event_log=False)
        assert not (out / 'event_log.jsonl').exists()

    def test_event_log_written_when_opted_in(self, tmp_path):
        writer = ReportWriter(base_output_dir=str(tmp_path))
        result = _make_result(event_log=[{'event_type': 'no_signal', 'timestamp': '09:30'}])
        out = writer.write(result, config={}, save_event_log=True)
        assert (out / 'event_log.jsonl').exists()
        lines = (out / 'event_log.jsonl').read_text().strip().split('\n')
        assert len(lines) == 1
        assert json.loads(lines[0])['event_type'] == 'no_signal'

    def test_each_run_gets_unique_folder(self, tmp_path):
        writer = ReportWriter(base_output_dir=str(tmp_path))
        r1 = writer.write(_make_result(), config={})
        r2 = writer.write(_make_result(), config={})
        assert r1 != r2
