"""
Phase 4.9A — Regime allocation simulation CLI.

Runs four allocation policies against an existing backtest run and compares
P&L, drawdown, and trade count. Also measures pre-session regime accuracy.

Usage:
    python3 tools/run_allocation_simulation.py \\
        --run-dir runs/<run_folder> \\
        --data-dir data/raw/zerodha \\
        --instrument NIFTY

Outputs:
    <run_dir>/allocation/
        policy_comparison.csv       — aggregate metrics for each policy
        session_pnl_by_policy.csv   — per-session daily P&L under each policy
        pre_session_accuracy.json   — how well pre-session classifier matches oracle
        pre_session_regimes.csv     — pre-session classifications per session
        pre_session_policy_comparison.csv — same policy comparison using pre-session gate
"""
import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import pandas as pd  # noqa: E402

from src.analytics.pre_session_regime import (  # noqa: E402
    accuracy_vs_oracle,
    estimate_pre_session_regimes,
)
from src.analytics.regime_allocation import (  # noqa: E402
    AllocationPolicy,
    PRESET_POLICIES,
    compare_policies,
    session_pnl_by_policy,
    simulate_allocation,
)


def _print_section(title: str, df: pd.DataFrame) -> None:
    width = 72
    print(f"\n{'═' * width}")
    print(f"  {title}")
    print(f"{'═' * width}")
    print(df.to_string(index=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Regime allocation simulation')
    parser.add_argument('--run-dir',    required=True)
    parser.add_argument('--data-dir',   required=True)
    parser.add_argument('--instrument', default='NIFTY')
    args = parser.parse_args(argv)

    run_dir  = Path(args.run_dir)
    data_dir = Path(args.data_dir)

    # Load trades
    trades_df = pd.read_csv(run_dir / 'trades.csv')

    # Filter to single instrument if requested
    if args.instrument and 'instrument' in trades_df.columns:
        trades_df = trades_df[trades_df['instrument'] == args.instrument].copy()
        print(f"Filtered to {args.instrument}: {len(trades_df)} trades")

    # Load oracle session regimes
    regime_path = run_dir / 'regimes' / 'session_regimes.csv'
    if not regime_path.exists():
        print(f"ERROR: {regime_path} not found. Run run_regime_analysis.py first.",
              file=sys.stderr)
        return 1
    oracle_regimes = pd.read_csv(regime_path)

    # Load OHLCV for pre-session features
    ohlcv_path = data_dir / f'{args.instrument}.csv'
    if not ohlcv_path.exists():
        print(f"ERROR: {ohlcv_path} not found.", file=sys.stderr)
        return 1
    ohlcv_df = pd.read_csv(ohlcv_path, parse_dates=['timestamp'])

    out = run_dir / 'allocation'
    out.mkdir(parents=True, exist_ok=True)

    # ─── ORACLE SIMULATION ───────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print(f"  ORACLE SIMULATION  (uses current session's actual regime)")
    print(f"  {args.instrument}  {len(trades_df)} trades  {oracle_regimes['session_date'].nunique()} sessions")

    oracle_results   = simulate_allocation(trades_df, oracle_regimes)
    oracle_summary   = compare_policies(oracle_results)
    session_pnl_df   = session_pnl_by_policy(oracle_results)

    oracle_summary.to_csv(out / 'policy_comparison.csv', index=False)
    session_pnl_df.to_csv(out / 'session_pnl_by_policy.csv', index=False)

    _print_section('POLICY COMPARISON — ORACLE', oracle_summary)

    # ─── REGIME DISTRIBUTION ─────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    counts = oracle_regimes['regime'].value_counts()
    print("  SESSION DISTRIBUTION")
    for regime, n in counts.items():
        bar = '█' * n
        pct = n / len(oracle_regimes) * 100
        print(f"    {regime:<12} {n:>4} ({pct:5.1f}%)  {bar[:50]}")

    # ─── STRATEGY × REGIME BREAKDOWN ────────────────────────────────────────
    from src.analytics.regime_allocation import (
        REGIME_BAD, REGIME_NEUTRAL, REGIME_GOOD
    )
    baseline_trades = oracle_results['BASELINE']
    baseline_trades['regime_label'] = baseline_trades['regime']
    print(f"\n{'─'*72}")
    print("  STRATEGY × REGIME P&L  (oracle)")
    for strat, grp in baseline_trades.groupby('strategy'):
        for regime in [REGIME_BAD, REGIME_NEUTRAL, REGIME_GOOD]:
            sub = grp[grp['regime'] == regime]['net_pnl'].dropna()
            if sub.empty:
                continue
            winners = sub[sub > 0]
            losers  = sub[sub <= 0]
            gw = winners.sum(); gl = abs(losers.sum())
            pf  = round(gw / gl, 2) if gl > 0 else '∞'
            wr  = round(len(winners) / len(sub), 3)
            print(f"    {strat:<16} {regime:<10} n={len(sub):>4}  "
                  f"wr={wr:.0%}  pnl={sub.sum():>10,.0f}  PF={pf}")

    # ─── PRE-SESSION ESTIMATION ──────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print("  PRE-SESSION REGIME ESTIMATION  (uses only info before 09:30)")
    pre_session_df = estimate_pre_session_regimes(ohlcv_df)
    pre_session_df.to_csv(out / 'pre_session_regimes.csv', index=False)

    # Accuracy
    accuracy = accuracy_vs_oracle(pre_session_df, oracle_regimes)
    with open(out / 'pre_session_accuracy.json', 'w') as f:
        json.dump(accuracy, f, indent=2)

    print(f"\n  Accuracy vs oracle: {accuracy['exact_match_pct']:.1%}  "
          f"({accuracy['total_sessions']} sessions compared)")
    print("  Confusion (predicted→actual):")
    for label, count in accuracy['confusion'].items():
        pred, actual = label.split('→')
        correct = '✓' if pred == actual else '✗'
        print(f"    {correct}  {label:30s}  {count}")

    # Pre-session policy comparison
    pre_results  = simulate_allocation(trades_df, pre_session_df.rename(
        columns={'pre_session_regime': 'regime'}
    ))
    pre_summary  = compare_policies(pre_results)
    pre_summary.to_csv(out / 'pre_session_policy_comparison.csv', index=False)
    _print_section('POLICY COMPARISON — PRE-SESSION', pre_summary)

    print(f"\n  Side-by-side CONSERVATIVE policy:")
    oracle_cons_pnl = oracle_results['CONSERVATIVE']['net_pnl'].sum()
    pre_cons_pnl    = pre_results['CONSERVATIVE']['net_pnl'].sum() if 'CONSERVATIVE' in pre_results else 0
    print(f"    Oracle P&L:      ₹{oracle_cons_pnl:>12,.2f}")
    print(f"    Pre-session P&L: ₹{pre_cons_pnl:>12,.2f}")
    print(f"    Gap (lookahead): ₹{oracle_cons_pnl - pre_cons_pnl:>12,.2f}")

    print(f"\nSaved → {out}/")
    return 0


if __name__ == '__main__':
    sys.exit(main())
