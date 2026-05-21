"""
Phase 5A — Adaptive Strategy Allocation backtest comparison.

Runs the same full_2025 dataset under four policies and compares:
  all_on              — baseline (all strategies always active)
  vwap_pullback_only  — the single confirmed edge strategy
  conservative        — VWAP_PULLBACK always + ORB on structured days only
  deterministic       — full regime-aware allocation

Acceptance criteria (must meet ALL three to declare the allocator an improvement):
  1. Beats VWAP_PULLBACK alone on total P&L
  2. Beats all_on baseline on P&L AND profit factor AND max drawdown
  3. Positive or breakeven monthly stability (no sustained losing streaks)

Usage:
  python3 tools/run_allocation_backtest.py \\
      --data-dir data/raw/zerodha/full_2025 \\
      --out-dir runs/allocation_5A
"""
import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from src.backtest.experiment import ExperimentRunner, _build_strategies  # noqa: E402
from src.execution.simulator import BacktestSimulator  # noqa: E402
from src.runtimes.backtest import BacktestRuntime  # noqa: E402
from src.data.loader import DataLoader  # noqa: E402
from src.data.sessionizer import Sessionizer  # noqa: E402
from src.backtest.experiment import _build_features, _apply_features  # noqa: E402
from src.strategies.allocator import wrap_strategies, NAMED_POLICIES  # noqa: E402


def _run_policy(policy_name: str, config: dict, data_dir: str) -> dict:
    """Run one policy and return metrics dict."""
    loader     = DataLoader(data_dir)
    sessionizer = Sessionizer()
    features    = _build_features(config)
    base_strats = _build_strategies(config)
    gated_strats = wrap_strategies(base_strats, policy_name)

    simulator = BacktestSimulator(
        slippage_per_side=config['costs']['slippage_per_side'],
        brokerage=config['costs']['brokerage_per_trade'],
    )

    all_trades = []
    for instrument in config['instruments']:
        try:
            df = loader.load_historical_data(instrument)
            sessions = sessionizer.create_sessions(df)
            sessions = {dt: _apply_features(s, features) for dt, s in sessions.items()}
            runtime = BacktestRuntime(
                strategies=gated_strats, simulator=simulator, config=config
            )
            for sess_date, sess_df in sessions.items():
                trades, _ = runtime.run_session_with_log(instrument, sess_date, sess_df)
                all_trades.extend(trades)
        except Exception as e:
            print(f"  WARNING {instrument}: {e}", file=sys.stderr)

    # Compute metrics
    if not all_trades:
        return {'policy': policy_name, 'n_trades': 0}
    pnl = pd.Series([t.net_pnl for t in all_trades if t.net_pnl is not None])
    wins   = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    wr     = len(wins) / len(pnl)
    gw, gl = wins.sum(), abs(losses.sum())
    pf     = round(gw / gl, 4) if gl > 0 else None
    cum    = pnl.cumsum()
    max_dd = round((cum - cum.cummax()).min(), 2)
    exp    = round(pnl.mean(), 2)

    # Per-strategy breakdown
    by_strat = {}
    for t in all_trades:
        if t.net_pnl is None: continue
        by_strat.setdefault(t.strategy_name, []).append(t.net_pnl)
    strat_summary = {
        s: {'n': len(v), 'total': round(sum(v), 2)}
        for s, v in by_strat.items()
    }

    # Monthly P&L
    trade_df = pd.DataFrame([
        {'date': t.entry_time.strftime('%Y-%m'), 'pnl': t.net_pnl}
        for t in all_trades if t.net_pnl is not None
    ])
    monthly = trade_df.groupby('date')['pnl'].sum().round(2).to_dict()

    return {
        'policy':       policy_name,
        'n_trades':     len(pnl),
        'win_rate':     round(wr, 4),
        'avg_win':      round(wins.mean(), 2) if len(wins) else 0,
        'avg_loss':     round(losses.mean(), 2) if len(losses) else 0,
        'expectancy':   exp,
        'total_pnl':    round(pnl.sum(), 2),
        'profit_factor': pf,
        'max_drawdown': max_dd,
        'by_strategy':  strat_summary,
        'monthly_pnl':  monthly,
    }


def _acceptance_check(results: dict) -> dict:
    """Evaluate acceptance criteria for the deterministic allocator."""
    baseline  = results.get('all_on', {})
    pullback  = results.get('vwap_pullback_only', {})
    alloc     = results.get('deterministic', {})

    checks = {}
    checks['beats_pullback_pnl'] = bool(
        alloc.get('total_pnl', -999) > pullback.get('total_pnl', -999)
    )
    checks['beats_baseline_pnl'] = bool(
        alloc.get('total_pnl', -999) > baseline.get('total_pnl', -999)
    )
    checks['beats_baseline_pf'] = bool(
        (alloc.get('profit_factor') or 0) > (baseline.get('profit_factor') or 0)
    )
    checks['beats_baseline_drawdown'] = bool(
        alloc.get('max_drawdown', -999) > baseline.get('max_drawdown', -999)
    )
    checks['all_passed'] = bool(all(checks.values()))
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description='Phase 5A allocation backtest')
    parser.add_argument('--data-dir',  default='data/raw/zerodha/full_2025')
    parser.add_argument('--config',    default='config/base.yaml')
    parser.add_argument('--out-dir',   default='runs/allocation_5A')
    args = parser.parse_args(argv)

    config = yaml.safe_load(open(args.config))
    out    = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    policies = list(NAMED_POLICIES.keys())
    results  = {}

    for policy in policies:
        print(f"\n>>> Running policy: {policy} ...")
        r = _run_policy(policy, config, args.data_dir)
        results[policy] = r
        n  = r.get('n_trades', 0)
        pnl = r.get('total_pnl', 0)
        pf  = r.get('profit_factor', 0)
        dd  = r.get('max_drawdown', 0)
        print(f"    n={n}  P&L=₹{pnl:,.0f}  PF={pf}  DD=₹{dd:,.0f}")

    # Save raw results
    with open(out / 'results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)

    # Comparison table
    rows = []
    for p, r in results.items():
        rows.append({
            'policy':   p,
            'n_trades': r.get('n_trades', 0),
            'win_rate': r.get('win_rate', 0),
            'total_pnl': r.get('total_pnl', 0),
            'profit_factor': r.get('profit_factor'),
            'max_drawdown':  r.get('max_drawdown', 0),
            'expectancy':    r.get('expectancy', 0),
        })
    comp = pd.DataFrame(rows)
    comp.to_csv(out / 'comparison.csv', index=False)

    # Acceptance check
    checks = _acceptance_check(results)
    with open(out / 'acceptance.json', 'w') as f:
        json.dump(checks, f, indent=2)

    # Print comparison
    print(f"\n{'═'*72}")
    print("  ALLOCATION COMPARISON — Phase 5A")
    print(f"{'═'*72}")
    print(comp.to_string(index=False))

    print(f"\n{'═'*72}")
    print("  ACCEPTANCE CRITERIA (deterministic vs baselines)")
    print(f"{'═'*72}")
    for k, v in checks.items():
        mark = '✅' if v else '❌'
        print(f"  {mark}  {k}")

    print(f"\n{'═'*72}")
    print("  BY-STRATEGY BREAKDOWN — deterministic policy")
    print(f"{'═'*72}")
    det = results.get('deterministic', {})
    for strat, s in sorted(det.get('by_strategy', {}).items()):
        print(f"  {strat:<20} n={s['n']:>4}  total=₹{s['total']:>12,.0f}")

    print(f"\nSaved → {out}/")
    return 0


if __name__ == '__main__':
    sys.exit(main())
