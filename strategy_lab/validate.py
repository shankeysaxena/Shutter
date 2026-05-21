"""
Phase 4.7 validation CLI.

Usage:
  python validate.py <sweep_config> [data_dir] [output_dir]

  sweep_config — YAML file describing what to run (see config/sweep.yaml)
  data_dir     — default: data/raw
  output_dir   — default: runs

Sweep config supports three independent sections (all optional):

  base_config: config/base.yaml   # required

  date_ranges:                    # 4.7A — multi-year validation
    - label: "2022"
      start: "2022-01-01"
      end:   "2022-12-31"

  param_sweeps:                   # 4.7B — parameter sensitivity
    - param: strategies.vwap_pullback.pullback_zone_pct
      values: [0.001, 0.002, 0.003, 0.005]

  compare_runtimes: true          # 4.7C — auto backtest vs replay comparison

Each section produces its own artifacts under output_dir.
"""
import sys
import json
import yaml
from pathlib import Path

from src.backtest.experiment import ExperimentRunner
from src.backtest.multi_run import MultiRunner
from src.analytics.reporting import ReportWriter
from src.analytics.comparison import compare_runs, print_comparison


def _load_base_config(sweep_cfg: dict) -> dict:
    base_path = sweep_cfg.get('base_config', 'config/base.yaml')
    with open(base_path) as f:
        return yaml.safe_load(f)


def _run_date_ranges(sweep_cfg: dict, base_config: dict, data_dir: str,
                     output_dir: str, writer: ReportWriter) -> None:
    date_ranges = sweep_cfg.get('date_ranges', [])
    if not date_ranges:
        return

    print("\n=== 4.7A — Multi-year validation ===")
    runner = MultiRunner(base_config, data_dir)
    labeled = runner.run_date_ranges(date_ranges)
    summary_df = MultiRunner.summarize(labeled)

    # Save individual run artifacts
    for label, result in labeled:
        run_dir = writer.write(result, base_config)
        print(f"  [{label}] trades={result.metadata['total_trades']}  "
              f"pnl={result.summary.get('total_net_pnl')}  "
              f"win_rate={result.summary.get('win_rate')}  → {run_dir.name}")

    # Save comparison table
    rows = summary_df.to_dict(orient='records')
    csv_path = writer.write_sweep_summary(rows, 'date_range_comparison', output_dir)
    print(f"\n  Comparison table → {csv_path}")
    print(summary_df.to_string(index=False))


def _run_param_sweeps(sweep_cfg: dict, base_config: dict, data_dir: str,
                      output_dir: str, writer: ReportWriter) -> None:
    param_sweeps = sweep_cfg.get('param_sweeps', [])
    if not param_sweeps:
        return

    print("\n=== 4.7B — Parameter sensitivity ===")
    runner = MultiRunner(base_config, data_dir)

    for spec in param_sweeps:
        param = spec['param']
        values = spec['values']
        short_key = param.split('.')[-1]
        print(f"\n  Sweeping {param}: {values}")

        labeled = runner.run_param_sweep(param, values)
        summary_df = MultiRunner.summarize(labeled)

        rows = summary_df.to_dict(orient='records')
        csv_path = writer.write_sweep_summary(rows, f'sweep_{short_key}', output_dir)
        print(f"  Results → {csv_path}")
        print(summary_df[['label', 'total_trades', 'win_rate',
                           'total_net_pnl', 'max_drawdown']].to_string(index=False))


def _run_runtime_comparison(base_config: dict, data_dir: str,
                             output_dir: str, writer: ReportWriter) -> None:
    print("\n=== 4.7C — Backtest vs Replay comparison ===")

    bt_config = {**base_config, 'runtime': {'mode': 'backtest'}}
    rp_config = {**base_config, 'runtime': {'mode': 'replay'}}

    bt_result = ExperimentRunner(bt_config, data_dir).run()
    rp_result = ExperimentRunner(rp_config, data_dir).run()

    comparison = compare_runs(bt_result.ledger, rp_result.ledger)
    print_comparison(comparison)

    # Persist comparison artifact
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    comparison_path = out / 'runtime_comparison.json'
    with open(comparison_path, 'w') as f:
        json.dump(comparison, f, indent=2, default=str)
    print(f"\n  Comparison artifact → {comparison_path}")

    # Save individual run artifacts too
    writer.write(bt_result, bt_config)
    writer.write(rp_result, rp_config)


def run(sweep_config_path: str, data_dir: str = 'data/raw',
        output_dir: str = 'runs') -> None:
    with open(sweep_config_path) as f:
        sweep_cfg = yaml.safe_load(f)

    base_config = _load_base_config(sweep_cfg)
    writer = ReportWriter(base_output_dir=output_dir)

    _run_date_ranges(sweep_cfg, base_config, data_dir, output_dir, writer)
    _run_param_sweeps(sweep_cfg, base_config, data_dir, output_dir, writer)

    if sweep_cfg.get('compare_runtimes', False):
        _run_runtime_comparison(base_config, data_dir, output_dir, writer)

    print(f"\nAll artifacts saved under: {output_dir}/")


if __name__ == '__main__':
    sweep_file = sys.argv[1] if len(sys.argv) > 1 else 'config/sweep.yaml'
    data_folder = sys.argv[2] if len(sys.argv) > 2 else 'data/raw'
    output_folder = sys.argv[3] if len(sys.argv) > 3 else 'runs'
    run(sweep_file, data_folder, output_folder)
