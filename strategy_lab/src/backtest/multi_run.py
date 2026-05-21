"""
MultiRunner — runs ExperimentRunner across multiple dimensions for robustness validation.

Two modes:
  run_date_ranges()  — same config, different date ranges (multi-year validation)
  run_param_sweep()  — same config + date range, one parameter varied (sensitivity)

Both return a list of (label, ExperimentResult) and a flat summary DataFrame
suitable for year-over-year or sensitivity comparison.
"""
import copy
from typing import Any, Dict, List, Tuple

import pandas as pd

from src.backtest.experiment import ExperimentResult, ExperimentRunner


class MultiRunner:
    """
    Orchestrates multiple ExperimentRunner calls for robustness analysis.
    Does not write files — caller hands results to ReportWriter.
    """

    def __init__(self, base_config: dict, data_dir: str):
        self.base_config = base_config
        self.data_dir = data_dir

    def run_date_ranges(
        self,
        date_ranges: List[Dict[str, str]],
    ) -> List[Tuple[str, ExperimentResult]]:
        """
        Run the same strategy config across multiple date ranges.

        date_ranges: list of dicts with keys:
          label  — human-readable name, e.g. '2022'
          start  — ISO date string, e.g. '2022-01-01'
          end    — ISO date string, e.g. '2022-12-31'

        Returns: [(label, ExperimentResult), ...]
        """
        results = []
        for dr in date_ranges:
            label = dr.get('label', f"{dr['start']}_{dr['end']}")
            config = copy.deepcopy(self.base_config)
            config['date_range'] = {'start': dr['start'], 'end': dr['end']}
            config['experiment_name'] = f"{config.get('experiment_name', 'exp')}_{label}"
            result = ExperimentRunner(config, self.data_dir).run()
            results.append((label, result))
        return results

    def run_param_sweep(
        self,
        param_path: str,
        values: List[Any],
    ) -> List[Tuple[str, ExperimentResult]]:
        """
        Vary one config parameter across a list of values.

        param_path: dot-notation path, e.g. 'strategies.vwap_pullback.pullback_zone_pct'
        values: list of values to test, e.g. [0.001, 0.002, 0.003, 0.005]

        Returns: [(label, ExperimentResult), ...]
        """
        short_key = param_path.split('.')[-1]
        results = []
        for val in values:
            label = f"{short_key}={val}"
            config = copy.deepcopy(self.base_config)
            _set_nested(config, param_path, val)
            config['experiment_name'] = f"{config.get('experiment_name', 'exp')}_{label}"
            result = ExperimentRunner(config, self.data_dir).run()
            result.metadata['sweep_param_path'] = param_path
            result.metadata['sweep_param_value'] = val
            results.append((label, result))
        return results

    @staticmethod
    def summarize(labeled_results: List[Tuple[str, ExperimentResult]]) -> pd.DataFrame:
        """
        Flatten a list of (label, ExperimentResult) into a comparison DataFrame.
        One row per run. Suitable for year-over-year or sensitivity tables.
        """
        rows = []
        for label, result in labeled_results:
            s = result.summary
            row: Dict = {
                'label': label,
                'param_path': result.metadata.get('sweep_param_path'),
                'param_value': result.metadata.get('sweep_param_value'),
            }
            for key in ['total_trades', 'win_rate', 'total_net_pnl', 'avg_win',
                        'avg_loss', 'expectancy', 'profit_factor', 'max_drawdown',
                        'max_consecutive_losses', 'avg_r_multiple']:
                row[key] = s.get(key)

            # No-trade day rate: sessions processed vs days with at least one trade
            sessions = result.metadata.get('total_sessions_processed', 0)
            trade_days = result.ledger['date'].nunique() if not result.ledger.empty else 0
            row['sessions_processed'] = sessions
            row['trade_days'] = trade_days
            row['no_trade_day_rate'] = (
                round(1 - trade_days / sessions, 4) if sessions > 0 else None
            )
            row['sessions_with_warnings'] = result.metadata.get('sessions_with_warnings', 0)
            row['failed_instruments'] = len(result.failed_instruments)
            rows.append(row)

        return pd.DataFrame(rows)


# --- Helpers ---

def _set_nested(d: dict, path: str, value: Any) -> None:
    """Set a value in a nested dict using dot-notation path. Mutates d in place."""
    keys = path.split('.')
    target = d
    for key in keys[:-1]:
        target = target.setdefault(key, {})
    target[keys[-1]] = value
