"""
Tests for Phase 4.7 components:
- aggregate_rejections
- MultiRunner date ranges and param sweeps
- MultiRunner.summarize no-trade-day-rate and column set
- _set_nested helper
"""
import pytest
import pandas as pd
from datetime import date, datetime
from unittest.mock import patch, MagicMock

from src.analytics.metrics import MetricsEngine
from src.backtest.multi_run import MultiRunner, _set_nested
from src.backtest.experiment import ExperimentResult


# --- aggregate_rejections ---

class TestAggregateRejections:

    def _make_log(self, entries):
        return [{'event_type': 'no_signal', **e} for e in entries]

    def test_empty_log_returns_empty(self):
        assert MetricsEngine.aggregate_rejections([]) == {}

    def test_counts_by_strategy_and_reason(self):
        log = self._make_log([
            {'strategy': 'ORB', 'reason': 'no_breakout'},
            {'strategy': 'ORB', 'reason': 'no_breakout'},
            {'strategy': 'ORB', 'reason': 'after_cutoff'},
            {'strategy': 'VWAP_PULLBACK', 'reason': 'in_pullback'},
        ])
        result = MetricsEngine.aggregate_rejections(log)
        assert result['ORB']['no_breakout'] == 2
        assert result['ORB']['after_cutoff'] == 1
        assert result['VWAP_PULLBACK']['in_pullback'] == 1

    def test_non_no_signal_events_ignored(self):
        log = [
            {'event_type': 'entry_filled', 'strategy': 'ORB'},
            {'event_type': 'exit_target', 'strategy': 'ORB'},
            {'event_type': 'no_signal', 'strategy': 'ORB', 'reason': 'no_breakout'},
        ]
        result = MetricsEngine.aggregate_rejections(log)
        assert list(result.keys()) == ['ORB']
        assert result['ORB'] == {'no_breakout': 1}

    def test_missing_strategy_key_uses_unknown(self):
        log = [{'event_type': 'no_signal', 'reason': 'after_cutoff'}]
        result = MetricsEngine.aggregate_rejections(log)
        assert 'unknown' in result

    def test_missing_reason_key_uses_unknown(self):
        log = [{'event_type': 'no_signal', 'strategy': 'ORB'}]
        result = MetricsEngine.aggregate_rejections(log)
        assert result['ORB'].get('unknown', 0) == 1


# --- _set_nested ---

class TestSetNested:

    def test_single_level(self):
        d = {'a': 1}
        _set_nested(d, 'a', 99)
        assert d['a'] == 99

    def test_two_levels(self):
        d = {'strategies': {'orb': {'target_r': 2.0}}}
        _set_nested(d, 'strategies.orb.target_r', 3.0)
        assert d['strategies']['orb']['target_r'] == 3.0

    def test_creates_missing_intermediate_keys(self):
        d = {}
        _set_nested(d, 'a.b.c', 42)
        assert d['a']['b']['c'] == 42

    def test_does_not_mutate_other_keys(self):
        d = {'costs': {'slippage_per_side': 2.0, 'brokerage_per_trade': 20.0}}
        _set_nested(d, 'costs.slippage_per_side', 5.0)
        assert d['costs']['brokerage_per_trade'] == 20.0


# --- MultiRunner ---

def _fake_result(label: str, trades: int = 5, pnl: float = 1000.0) -> ExperimentResult:
    """Minimal ExperimentResult for testing MultiRunner.summarize."""
    ts = datetime(2024, 1, 2, 9, 31)
    ledger = pd.DataFrame([{
        'strategy': 'ORB', 'instrument': 'NIFTY', 'direction': 'LONG',
        'entry_time': ts, 'exit_time': ts, 'net_pnl': pnl / trades,
        'date': date(2024, 1, 2), 'r_multiple': 2.0,
    }] * trades) if trades > 0 else pd.DataFrame()

    summary = {
        'total_trades': trades, 'win_rate': 0.6, 'total_net_pnl': pnl,
        'avg_win': 200.0, 'avg_loss': -100.0, 'expectancy': 80.0,
        'profit_factor': 2.0, 'max_drawdown': -300.0,
        'max_consecutive_losses': 2, 'avg_r_multiple': 1.5,
    }
    return ExperimentResult(
        experiment_name=f'exp_{label}', runtime_mode='backtest',
        instruments=['NIFTY'], all_trades=[], ledger=ledger, summary=summary,
        session_warnings={}, failed_instruments={}, event_log=[],
        rejections={}, metadata={'total_sessions_processed': 10,
                                  'sessions_with_warnings': 0},
    )


class TestMultiRunnerSummarize:

    def test_returns_dataframe(self):
        labeled = [('2022', _fake_result('2022')), ('2023', _fake_result('2023'))]
        df = MultiRunner.summarize(labeled)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2

    def test_label_column_present(self):
        labeled = [('2022', _fake_result('2022'))]
        df = MultiRunner.summarize(labeled)
        assert 'label' in df.columns
        assert df.iloc[0]['label'] == '2022'

    def test_all_summary_fields_present(self):
        labeled = [('2022', _fake_result('2022'))]
        df = MultiRunner.summarize(labeled)
        for col in ['total_trades', 'win_rate', 'total_net_pnl', 'max_drawdown',
                    'no_trade_day_rate', 'sessions_processed', 'param_path', 'param_value']:
            assert col in df.columns, f"missing column: {col}"

    def test_param_path_and_value_none_for_date_range_runs(self):
        labeled = [('2022', _fake_result('2022'))]
        df = MultiRunner.summarize(labeled)
        assert df.iloc[0]['param_path'] is None
        assert df.iloc[0]['param_value'] is None

    def test_no_trade_day_rate_computed(self):
        labeled = [('2022', _fake_result('2022', trades=5))]
        df = MultiRunner.summarize(labeled)
        # 10 sessions, 1 unique trade day → rate = 1 - 1/10 = 0.9
        assert df.iloc[0]['no_trade_day_rate'] == pytest.approx(0.9)

    def test_no_trade_day_rate_zero_when_all_days_traded(self):
        result = _fake_result('x', trades=10)
        # Give trades on 10 different days
        dates = [date(2024, 1, i + 1) for i in range(10)]
        result.ledger = pd.DataFrame([{
            'strategy': 'ORB', 'instrument': 'NIFTY', 'direction': 'LONG',
            'entry_time': datetime(2024, 1, i + 1, 9, 31),
            'exit_time': datetime(2024, 1, i + 1, 11, 0),
            'net_pnl': 100.0, 'date': dates[i], 'r_multiple': 2.0,
        } for i in range(10)])
        df = MultiRunner.summarize([('x', result)])
        assert df.iloc[0]['no_trade_day_rate'] == pytest.approx(0.0)

    def test_empty_trades_result(self):
        result = _fake_result('empty', trades=0)
        df = MultiRunner.summarize([('empty', result)])
        assert df.iloc[0]['total_trades'] == 0
        # 10 sessions, 0 trade days → rate = 1.0 (all sessions had no trades)
        assert df.iloc[0]['no_trade_day_rate'] == pytest.approx(1.0)


class TestMultiRunnerParamSweep:

    def _base_config(self):
        return {
            'experiment_name': 'test',
            'runtime': {'mode': 'backtest'},
            'instruments': ['NIFTY'],
            'date_range': {'start': '2024-01-01', 'end': '2024-12-31'},
            'costs': {'slippage_per_side': 2.0, 'brokerage_per_trade': 20.0},
            'risk': {'lot_size': {'NIFTY': 25}, 'max_total_trades_per_day': 4},
            'strategies': {'orb': {'enabled': False}},
            'session_validation': {'enabled': False},
        }

    def test_sweep_does_not_mutate_base_config(self):
        """Each sweep run must get its own deep copy — base config must be unchanged."""
        base = self._base_config()
        original_slippage = base['costs']['slippage_per_side']

        with patch('src.backtest.multi_run.ExperimentRunner') as MockRunner:
            mock_result = MagicMock()
            mock_result.summary = {}
            mock_result.metadata = {'total_sessions_processed': 0, 'sessions_with_warnings': 0}
            mock_result.ledger = pd.DataFrame()
            mock_result.failed_instruments = {}
            MockRunner.return_value.run.return_value = mock_result

            mr = MultiRunner(base, 'data/raw')
            mr.run_param_sweep('costs.slippage_per_side', [1.0, 5.0])

        assert base['costs']['slippage_per_side'] == original_slippage

    def test_sweep_passes_correct_values(self):
        """Each ExperimentRunner must receive the config with the right param value."""
        base = self._base_config()
        seen_values = []

        def fake_runner(config, data_dir):
            seen_values.append(config['costs']['slippage_per_side'])
            m = MagicMock()
            m.run.return_value = MagicMock(
                summary={}, metadata={'total_sessions_processed': 0, 'sessions_with_warnings': 0},
                ledger=pd.DataFrame(), failed_instruments={},
            )
            return m

        with patch('src.backtest.multi_run.ExperimentRunner', side_effect=fake_runner):
            mr = MultiRunner(base, 'data/raw')
            mr.run_param_sweep('costs.slippage_per_side', [1.0, 3.0, 5.0])

        assert seen_values == [1.0, 3.0, 5.0]

    def test_summarize_sweep_has_param_path_and_value(self):
        """summarize() must expose param_path and param_value stamped by run_param_sweep."""
        base = self._base_config()

        def fake_runner(config, data_dir):
            m = MagicMock()
            m.run.return_value = MagicMock(
                summary={}, metadata={'total_sessions_processed': 0, 'sessions_with_warnings': 0},
                ledger=pd.DataFrame(), failed_instruments={},
            )
            return m

        with patch('src.backtest.multi_run.ExperimentRunner', side_effect=fake_runner):
            mr = MultiRunner(base, 'data/raw')
            labeled = mr.run_param_sweep('costs.slippage_per_side', [1.0, 3.0])

        df = MultiRunner.summarize(labeled)
        assert list(df['param_path']) == ['costs.slippage_per_side', 'costs.slippage_per_side']
        assert list(df['param_value']) == [1.0, 3.0]
