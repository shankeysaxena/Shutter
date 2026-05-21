"""
Phase 4.8B.3 — Counterfactual regime gating analysis.

Answers the question: what would the same trades look like under different
regime gates — WITHOUT changing strategy logic.

Scenarios evaluated for each strategy and combined:
  UNGATED           — baseline (all trades)
  GOOD+NEUTRAL      — exclude BAD_ORB days
  GOOD_ORB_ONLY     — only the strictest filter
  NEUTRAL_ONLY      — neutral-only (middle band)
  BAD_ORB_ONLY      — what you would be avoiding

Usage:
    python3 tools/run_counterfactual.py \\
        --run-dir runs/<run_folder> \\
        --data-dir data/raw/zerodha

Outputs:
    <run_dir>/counterfactual/
        gating_summary.csv    — all scenarios × all strategies
        gating_combined.csv   — all strategies combined per scenario
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import pandas as pd  # noqa: E402

from src.analytics.regime import (  # noqa: E402
    REGIME_BAD,
    REGIME_GOOD,
    REGIME_NEUTRAL,
    regimes_to_df,
    tag_sessions,
)


# ─────────────────────────────────────────────────────────────────────────────
# Gate definitions
# ─────────────────────────────────────────────────────────────────────────────

GATES = {
    'UNGATED':       {REGIME_GOOD, REGIME_NEUTRAL, REGIME_BAD},
    'GOOD+NEUTRAL':  {REGIME_GOOD, REGIME_NEUTRAL},
    'GOOD_ORB_ONLY': {REGIME_GOOD},
    'NEUTRAL_ONLY':  {REGIME_NEUTRAL},
    'BAD_ORB_ONLY':  {REGIME_BAD},
}


def _metrics(pnl: pd.Series, r: pd.Series = None) -> Dict:
    if pnl.empty:
        return {'n_trades': 0, 'win_rate': None, 'avg_win': None,
                'avg_loss': None, 'expectancy': None, 'total_pnl': None,
                'profit_factor': None, 'avg_r': None, 'max_drawdown': None}
    winners = pnl[pnl > 0]
    losers  = pnl[pnl <= 0]
    win_rate = len(winners) / len(pnl)
    avg_win  = round(winners.mean(), 2) if len(winners) else 0.0
    avg_loss = round(losers.mean(),  2) if len(losers)  else 0.0
    expectancy = round(win_rate * avg_win + (1 - win_rate) * avg_loss, 2)
    gw = winners.sum(); gl = abs(losers.sum())
    pf = round(gw / gl, 4) if gl > 0 else None
    avg_r = round(r.mean(), 4) if r is not None and not r.empty else None
    cum = pnl.cumsum()
    max_dd = round((cum - cum.cummax()).min(), 2)
    return {
        'n_trades':      len(pnl),
        'win_rate':      round(win_rate, 4),
        'avg_win':       avg_win,
        'avg_loss':      avg_loss,
        'expectancy':    expectancy,
        'total_pnl':     round(pnl.sum(), 2),
        'profit_factor': pf,
        'avg_r':         avg_r,
        'max_drawdown':  max_dd,
    }


def run_counterfactual(
    trades_df: pd.DataFrame,
    regime_map: Dict,
    per_strategy: bool = True,
) -> pd.DataFrame:
    trades = trades_df.copy()
    trades['entry_time'] = pd.to_datetime(trades['entry_time'])
    trades['trade_date'] = trades['entry_time'].dt.date
    trades['regime'] = trades['trade_date'].map(regime_map).fillna(REGIME_NEUTRAL)

    rows = []
    strategies = sorted(trades['strategy'].unique()) if per_strategy else ['ALL']

    for scenario, allowed in GATES.items():
        gated = trades[trades['regime'].isin(allowed)]

        for strat in strategies:
            sub = gated[gated['strategy'] == strat] if strat != 'ALL' else gated
            m = _metrics(sub['net_pnl'].dropna(),
                         sub['r_multiple'].dropna() if 'r_multiple' in sub else None)
            rows.append({'scenario': scenario, 'strategy': strat, **m})

    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description='Counterfactual regime gating analysis')
    parser.add_argument('--run-dir',    required=True)
    parser.add_argument('--data-dir',   required=True)
    parser.add_argument('--instrument', default='NIFTY')
    args = parser.parse_args(argv)

    run_dir  = Path(args.run_dir)
    data_dir = Path(args.data_dir)

    trades_df = pd.read_csv(run_dir / 'trades.csv')
    ohlcv_df  = pd.read_csv(data_dir / f'{args.instrument}.csv', parse_dates=['timestamp'])

    # Get OR widths from trade metadata (accurate)
    or_widths = {}
    for _, row in trades_df.iterrows():
        try:
            meta = json.loads(row['metadata_json']) if isinstance(row['metadata_json'], str) else {}
            orh, orl = meta.get('or_high'), meta.get('or_low')
            if orh and orl and orl > 0:
                d = pd.to_datetime(row['entry_time']).date()
                or_widths[d] = (orh - orl) / ((orh + orl) / 2)
        except Exception:
            pass

    regimes    = tag_sessions(ohlcv_df, or_width_by_date=or_widths)
    regime_map = {r.session_date: r.regime for r in regimes}

    # Regime count summary
    from collections import Counter
    counts = Counter(r.regime for r in regimes)

    # Per-strategy counterfactual
    per_strat_df = run_counterfactual(trades_df, regime_map, per_strategy=True)

    # Combined (all strategies)
    combined_rows = []
    trades_tagged = trades_df.copy()
    trades_tagged['entry_time'] = pd.to_datetime(trades_tagged['entry_time'])
    trades_tagged['trade_date'] = trades_tagged['entry_time'].dt.date
    trades_tagged['regime'] = trades_tagged['trade_date'].map(regime_map).fillna(REGIME_NEUTRAL)
    for scenario, allowed in GATES.items():
        gated = trades_tagged[trades_tagged['regime'].isin(allowed)]
        m = _metrics(gated['net_pnl'].dropna(),
                     gated['r_multiple'].dropna() if 'r_multiple' in gated else None)
        combined_rows.append({'scenario': scenario, 'strategy': 'ALL', **m})
    combined_df = pd.DataFrame(combined_rows)

    # Save
    out = run_dir / 'counterfactual'
    out.mkdir(parents=True, exist_ok=True)
    per_strat_df.to_csv(out / 'gating_summary.csv', index=False)
    combined_df.to_csv(out / 'gating_combined.csv', index=False)

    # Print
    print(f"\n{'═'*65}")
    print("  REGIME DISTRIBUTION  (frozen v1 definition)")
    print(f"{'═'*65}")
    for regime in [REGIME_GOOD, REGIME_NEUTRAL, REGIME_BAD]:
        n = counts.get(regime, 0)
        bar = '█' * n
        print(f"  {regime:<12} {n:>3} sessions  {bar}")

    print(f"\n{'═'*65}")
    print("  COUNTERFACTUAL — COMBINED (all strategies)")
    print(f"{'═'*65}")
    print(combined_df.to_string(index=False))

    print(f"\n{'═'*65}")
    print("  COUNTERFACTUAL — PER STRATEGY")
    print(f"{'═'*65}")
    for strat, grp in per_strat_df.groupby('strategy'):
        print(f"\n  {strat}")
        print(grp.drop(columns='strategy').to_string(index=False))

    print(f"\nSaved → {out}/")
    return 0


if __name__ == '__main__':
    sys.exit(main())
