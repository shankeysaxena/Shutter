import sys
import yaml

from src.backtest.experiment import ExperimentResult, ExperimentRunner
from src.analytics.reporting import ReportWriter

_VALID_RUNTIME_MODES = ('backtest', 'replay')


def _validate_config(config: dict) -> None:
    """Raise ValueError with a clear message for any missing or invalid config field."""
    errors = []

    mode = config.get('runtime', {}).get('mode')
    if mode not in _VALID_RUNTIME_MODES:
        errors.append(f"runtime.mode must be one of {_VALID_RUNTIME_MODES}, got: {mode!r}")

    if not config.get('instruments'):
        errors.append("instruments list is empty or missing")

    costs = config.get('costs', {})
    for key in ('slippage_per_side', 'brokerage_per_trade'):
        val = costs.get(key)
        if val is None:
            errors.append(f"costs.{key} is missing")
        elif val < 0:
            errors.append(f"costs.{key} must be >= 0, got {val}")

    risk = config.get('risk', {})
    if not risk.get('lot_size'):
        errors.append("risk.lot_size mapping is missing")
    max_trades = risk.get('max_total_trades_per_day')
    if max_trades is not None and max_trades < 1:
        errors.append(f"risk.max_total_trades_per_day must be >= 1, got {max_trades}")

    if errors:
        raise ValueError("Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors))


def _print_result(result: ExperimentResult) -> None:
    if result.failed_instruments:
        print("\n--- INSTRUMENT FAILURES ---")
        for inst, err in result.failed_instruments.items():
            print(f"  {inst}: {err}")
        n_ok = len(result.instruments) - len(result.failed_instruments)
        print(f"  Results reflect {n_ok}/{len(result.instruments)} instruments.")
        if n_ok == 0:
            print("  All instruments failed. No results.")
            return

    if result.session_warnings:
        policy = result.metadata.get('session_validation_policy', 'warn')
        print(f"\n--- SESSION WARNINGS (policy={policy}) ---")
        for instrument, by_date in result.session_warnings.items():
            for dt, issues in by_date.items():
                for issue in issues:
                    print(f"  [{instrument} {dt}] {issue}")

    if result.data_source_warning:
        bar = '=' * 78
        print(f"\n{bar}\n!! {result.data_source_warning}\n{bar}")

    mode = result.runtime_mode.upper()
    print(f"\n--- {mode} SUMMARY ({result.experiment_name}) ---")
    if not result.summary:
        print("  No trades found.")
        return

    for key, val in result.summary.items():
        print(f"  {key}: {val}")

    if not result.ledger.empty:
        print("\nLedger Sample:")
        cols = ['strategy', 'instrument', 'entry_time', 'exit_time',
                'direction', 'net_pnl', 'r_multiple']
        print(result.ledger[cols].head(20).to_string(index=False))


def run(config_path: str, data_dir: str, output_dir: str = 'runs') -> None:
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    _validate_config(config)

    runner = ExperimentRunner(config, data_dir)
    result = runner.run()

    _print_result(result)

    # Always persist — even zero-trade runs capture metadata and warnings
    output_cfg = config.get('output', {})
    save_event_log = output_cfg.get('save_event_log', False)
    writer = ReportWriter(base_output_dir=output_dir)
    out_path = writer.write(result, config, save_event_log=save_event_log)
    print(f"\nArtifacts saved → {out_path}")


if __name__ == '__main__':
    config_file = sys.argv[1] if len(sys.argv) > 1 else 'config/base.yaml'
    data_folder = sys.argv[2] if len(sys.argv) > 2 else 'data/raw'
    output_folder = sys.argv[3] if len(sys.argv) > 3 else 'runs'
    run(config_file, data_folder, output_folder)
