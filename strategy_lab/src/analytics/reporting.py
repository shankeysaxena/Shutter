"""
ReportWriter — persists experiment artifacts to a timestamped run folder.

Output structure per run:
  runs/{experiment_name}_{YYYYMMDD_HHMMSS}/
    run_metadata.json           — timestamp, config hash, trade count, session stats
    config_snapshot.yaml        — exact config used for this run
    trades.csv                  — full trade ledger
    summary.json                — scalar metrics
    by_strategy.csv             — per-strategy breakdown
    by_day.csv                  — daily net PnL
    rejections_by_strategy.json — no_signal counts grouped by strategy + reason
    session_warnings.json       — quality issues (omitted if none)
    failed_instruments.json     — instruments that errored (omitted if none)
    event_log.jsonl             — full bar-by-bar event log (opt-in via save_event_log)

For multi-run sweeps, write_sweep_summary() saves a single CSV comparing all runs.
"""
import json
import yaml
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, List

import pandas as pd

if TYPE_CHECKING:
    from src.backtest.experiment import ExperimentResult

from src.analytics.metrics import MetricsEngine
from src.analytics.multi_leg_metrics import (
    compute_multi_leg_summary,
    multi_leg_leg_ledger,
    exit_reason_breakdown,
)


class ReportWriter:
    """
    Writes all artifacts for one ExperimentResult to disk.
    Call write() once per run — it creates the folder and returns its path.
    """

    def __init__(self, base_output_dir: str = 'runs'):
        self.base_output_dir = Path(base_output_dir)

    def write(
        self,
        result: 'ExperimentResult',
        config: dict,
        save_event_log: bool = False,
    ) -> Path:
        """
        Persist all artifacts. Returns the run folder path.
        Creates the folder (and any parents) if it does not exist.
        """
        run_dir = self._make_run_dir(result.experiment_name)

        _write_json(run_dir / 'run_metadata.json', result.metadata)
        _write_yaml(run_dir / 'config_snapshot.yaml', config)

        if not result.ledger.empty:
            result.ledger.to_csv(run_dir / 'trades.csv', index=False)

            by_strategy = MetricsEngine.pnl_by_strategy(result.ledger)
            if not by_strategy.empty:
                by_strategy.to_csv(run_dir / 'by_strategy.csv', index=False)

            by_day = MetricsEngine.pnl_by_day(result.ledger)
            if not by_day.empty:
                by_day.to_csv(run_dir / 'by_day.csv', index=False)

        if result.summary:
            _write_json(run_dir / 'summary.json', result.summary)

        if result.session_warnings:
            _write_json(run_dir / 'session_warnings.json', result.session_warnings)

        if result.failed_instruments:
            _write_json(run_dir / 'failed_instruments.json', result.failed_instruments)

        if result.rejections:
            _write_json(run_dir / 'rejections_by_strategy.json', result.rejections)

        if save_event_log and result.event_log:
            with open(run_dir / 'event_log.jsonl', 'w') as f:
                for entry in result.event_log:
                    f.write(json.dumps(entry, default=str) + '\n')

        # Multi-leg artifacts (only written when multi-leg trades exist)
        if result.all_multi_leg_trades:
            _write_json(
                run_dir / 'multi_leg_summary.json',
                compute_multi_leg_summary(result.all_multi_leg_trades),
            )
            leg_df = multi_leg_leg_ledger(result.all_multi_leg_trades)
            if not leg_df.empty:
                leg_df.to_csv(run_dir / 'multi_leg_trades.csv', index=False)
            exit_df = exit_reason_breakdown(result.all_multi_leg_trades)
            if not exit_df.empty:
                exit_df.to_csv(run_dir / 'exit_reasons.csv', index=False)

        return run_dir

    def _make_run_dir(self, experiment_name: str) -> Path:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:20]  # YYYYMMDD_HHMMSS_mmm
        run_dir = self.base_output_dir / f"{experiment_name}_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir


    def write_sweep_summary(
        self,
        rows: List[dict],
        sweep_name: str,
        output_dir: str,
    ) -> Path:
        """
        Persist a multi-run comparison table (date-range sweep or param sweep).
        Saves to {output_dir}/{sweep_name}.csv and {sweep_name}.json.
        Returns the CSV path.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(rows)
        csv_path = out / f"{sweep_name}.csv"
        df.to_csv(csv_path, index=False)
        _write_json(out / f"{sweep_name}.json", df.to_dict(orient='records'))
        return csv_path


def _write_json(path: Path, data: dict) -> None:
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)


def _write_yaml(path: Path, data: dict) -> None:
    with open(path, 'w') as f:
        yaml.dump(data, f, sort_keys=False, allow_unicode=True)
