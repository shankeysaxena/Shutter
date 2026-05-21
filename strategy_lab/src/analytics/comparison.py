"""
Comparison utility for backtest vs replay vs live paper runs.

The purpose is to detect divergence between runtimes early.
If backtest and replay produce different results on the same data and config,
there is a bug in one of them — not a strategy problem.
"""
import pandas as pd
from typing import Dict


def compare_runs(
    ledger_a: pd.DataFrame,
    ledger_b: pd.DataFrame,
    label_a: str = 'backtest',
    label_b: str = 'replay',
) -> Dict:
    """
    Compares two trade ledgers produced by different runtimes on the same data.

    Returns a summary dict with:
    - per-metric comparison
    - a 'diverged' flag (True if any key metric differs beyond tolerance)
    - a list of human-readable discrepancy descriptions
    """
    result: Dict = {
        label_a: {},
        label_b: {},
        'discrepancies': [],
        'diverged': False,
    }

    def _summarise(ledger: pd.DataFrame) -> Dict:
        if ledger.empty:
            return {'trade_count': 0, 'total_net_pnl': 0.0, 'win_rate': None}
        winners = ledger[ledger['net_pnl'] > 0]
        return {
            'trade_count': len(ledger),
            'total_net_pnl': round(ledger['net_pnl'].sum(), 4),
            'win_rate': round(len(winners) / len(ledger), 4),
        }

    summary_a = _summarise(ledger_a)
    summary_b = _summarise(ledger_b)
    result[label_a] = summary_a
    result[label_b] = summary_b

    # Trade count must be identical
    if summary_a['trade_count'] != summary_b['trade_count']:
        result['discrepancies'].append(
            f"trade_count differs: {label_a}={summary_a['trade_count']} "
            f"{label_b}={summary_b['trade_count']}"
        )
        result['diverged'] = True

    # Total PnL should match exactly (same fills, same exits, same data)
    pnl_diff = abs(summary_a['total_net_pnl'] - summary_b['total_net_pnl'])
    if pnl_diff > 0.01:
        result['discrepancies'].append(
            f"total_net_pnl differs by {pnl_diff:.4f}: "
            f"{label_a}={summary_a['total_net_pnl']} "
            f"{label_b}={summary_b['total_net_pnl']}"
        )
        result['diverged'] = True

    # Win rate should match
    if (summary_a['win_rate'] is not None and summary_b['win_rate'] is not None
            and abs(summary_a['win_rate'] - summary_b['win_rate']) > 0.001):
        result['discrepancies'].append(
            f"win_rate differs: {label_a}={summary_a['win_rate']} "
            f"{label_b}={summary_b['win_rate']}"
        )
        result['diverged'] = True

    # Trade-level detail if counts match
    if not result['diverged'] and not ledger_a.empty and not ledger_b.empty:
        result['trade_level_match'] = _compare_trades(ledger_a, ledger_b, label_a, label_b)

    return result


_MATCH_KEY = ['strategy', 'instrument', 'date', 'entry_time', 'direction']


def _compare_trades(
    ledger_a: pd.DataFrame,
    ledger_b: pd.DataFrame,
    label_a: str,
    label_b: str,
) -> Dict:
    """
    Aligns trades by composite key (strategy, instrument, entry_time, direction)
    and compares entry/exit prices and pnl.
    """
    mismatches = []
    unmatched_a = []
    unmatched_b = []

    key_cols = [c for c in _MATCH_KEY if c in ledger_a.columns and c in ledger_b.columns]
    a_indexed = ledger_a.set_index(key_cols) if key_cols else ledger_a.reset_index()
    b_indexed = ledger_b.set_index(key_cols) if key_cols else ledger_b.reset_index()

    a_keys = set(a_indexed.index)
    b_keys = set(b_indexed.index)

    unmatched_a = sorted(str(k) for k in a_keys - b_keys)
    unmatched_b = sorted(str(k) for k in b_keys - a_keys)

    for key in a_keys & b_keys:
        row_a = a_indexed.loc[key]
        row_b = b_indexed.loc[key]
        label = str(key)
        for col in ['exit_reason']:
            va, vb = row_a.get(col), row_b.get(col)
            if va != vb:
                mismatches.append(f"{label} {col}: {label_a}={va} {label_b}={vb}")
        for col in ['entry_price', 'exit_price', 'net_pnl']:
            va, vb = row_a.get(col), row_b.get(col)
            if va is not None and vb is not None and abs(float(va) - float(vb)) > 0.01:
                mismatches.append(f"{label} {col}: {label_a}={float(va):.4f} {label_b}={float(vb):.4f}")

    all_issues = mismatches + (
        [f"in {label_a} only: {k}" for k in unmatched_a] +
        [f"in {label_b} only: {k}" for k in unmatched_b]
    )
    return {'matched': len(all_issues) == 0, 'mismatches': all_issues}


def print_comparison(result: Dict) -> None:
    """Prints a human-readable comparison report."""
    labels = [k for k in result if k not in ('discrepancies', 'diverged', 'trade_level_match')]
    print("\n--- RUNTIME COMPARISON ---")
    for label in labels:
        s = result[label]
        print(f"  {label}: trades={s['trade_count']}  pnl={s['total_net_pnl']}  win_rate={s['win_rate']}")

    if result['diverged']:
        print("\n  DIVERGENCE DETECTED:")
        for d in result['discrepancies']:
            print(f"    - {d}")
    else:
        print("\n  Runtimes are consistent.")
        if 'trade_level_match' in result:
            tl = result['trade_level_match']
            if tl['matched']:
                print("  Trade-level match: all prices and outcomes agree.")
            else:
                print(f"  Trade-level mismatches ({len(tl['mismatches'])}):")
                for m in tl['mismatches']:
                    print(f"    - {m}")
