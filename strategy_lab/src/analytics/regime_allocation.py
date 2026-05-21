"""
Phase 4.9A — Regime Allocation Engine (analytical only).

Simulates what P&L would look like if strategies were enabled/disabled
based on the session's market regime. Does NOT modify strategy runtime —
the engine filters trades from an existing backtest ledger.

Two classification modes:
  ORACLE       — uses current session's actual OR width, gap, range (upper bound;
                  theoretically knowable at 09:30 before any trade entry)
  PRE_SESSION  — uses only prior session's features + current gap (deployable;
                  computed from what's known before the session opens)

Evidence base (NIFTY + BANKNIFTY, 2025, 498 sessions):
  VWAP_PULLBACK in BAD_ORB  → PF 1.74 (NIFTY), PF 1.59 (BANKNIFTY)   ✅ confirmed
  ORB in NEUTRAL            → PF 1.09 (NIFTY), PF 1.13 (BANKNIFTY)   ✅ confirmed
  ORB in BAD_ORB            → PF 0.85 (NIFTY), PF 0.81 (BANKNIFTY)   ❌ avoid
  VWAP_REVERSION            → 59% win rate but wrong R:R everywhere    ⚠️  defer calibration
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Set

import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Allocation policy
# ─────────────────────────────────────────────────────────────────────────────

REGIME_GOOD    = 'GOOD_ORB'
REGIME_BAD     = 'BAD_ORB'
REGIME_NEUTRAL = 'NEUTRAL'


@dataclass
class AllocationPolicy:
    """
    Maps each regime to a set of strategy names that are enabled.
    'ALL' means no restriction.
    """
    name: str
    enabled: Dict[str, Set[str]]    # {regime_label -> set of strategy names, or {'ALL'}}
    description: str = ''

    def is_enabled(self, strategy: str, regime: str) -> bool:
        allowed = self.enabled.get(regime, set())
        return 'ALL' in allowed or strategy in allowed


# Pre-built policies derived from empirical evidence
PRESET_POLICIES: Dict[str, AllocationPolicy] = {
    'BASELINE': AllocationPolicy(
        name='BASELINE',
        enabled={
            REGIME_BAD:     {'ALL'},
            REGIME_NEUTRAL: {'ALL'},
            REGIME_GOOD:    {'ALL'},
        },
        description='No gating — all strategies always active (current behavior)',
    ),
    'CONSERVATIVE': AllocationPolicy(
        name='CONSERVATIVE',
        enabled={
            REGIME_BAD:     {'VWAP_PULLBACK'},          # only confirmed edge in BAD_ORB
            REGIME_NEUTRAL: {'ORB'},                    # ORB + NEUTRAL = confirmed positive
            REGIME_GOOD:    {'ORB', 'GAP_BEHAVIOR'},    # breakout-friendly regime
        },
        description='One strategy per regime, highest-evidence signals only',
    ),
    'FULL_REGIME': AllocationPolicy(
        name='FULL_REGIME',
        enabled={
            REGIME_BAD:     {'VWAP_PULLBACK', 'VWAP_REVERSION'},
            REGIME_NEUTRAL: {'ORB', 'VWAP_PULLBACK'},
            REGIME_GOOD:    {'ORB', 'GAP_BEHAVIOR'},
        },
        description='Full regime-aware allocation including VWAP_REVERSION (uncalibrated)',
    ),
    'NO_BAD_ORB': AllocationPolicy(
        name='NO_BAD_ORB',
        enabled={
            REGIME_BAD:     set(),                      # sit out BAD_ORB entirely
            REGIME_NEUTRAL: {'ALL'},
            REGIME_GOOD:    {'ALL'},
        },
        description='Just avoid trading on BAD_ORB days — simplest possible gate',
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Core simulation
# ─────────────────────────────────────────────────────────────────────────────

def simulate_allocation(
    trades_df: pd.DataFrame,
    session_regimes: pd.DataFrame,
    policies: Optional[List[AllocationPolicy]] = None,
) -> Dict[str, pd.DataFrame]:
    """
    For each policy, filter `trades_df` to only trades allowed under that policy
    for the session's regime, and return the filtered ledger.

    Parameters
    ----------
    trades_df       : unified ledger from trades.csv
    session_regimes : DataFrame with columns [session_date, regime, instrument?]
                      Output of run_regime_analysis.
    policies        : list of AllocationPolicy; defaults to all PRESET_POLICIES

    Returns
    -------
    dict {policy_name: filtered_trades_df}
    """
    if policies is None:
        policies = list(PRESET_POLICIES.values())

    trades = trades_df.copy()
    trades['entry_time'] = pd.to_datetime(trades['entry_time'])
    trades['trade_date'] = trades['entry_time'].dt.date

    # Build (date, instrument) → regime map; if no instrument column, use date only
    if 'instrument' in session_regimes.columns:
        regime_map = {
            (pd.to_datetime(r['session_date']).date(), r['instrument']): r['regime']
            for _, r in session_regimes.iterrows()
        }
        trades['regime'] = trades.apply(
            lambda r: regime_map.get((r['trade_date'], r['instrument']), REGIME_NEUTRAL), axis=1
        )
    else:
        regime_map = {
            pd.to_datetime(r['session_date']).date(): r['regime']
            for _, r in session_regimes.iterrows()
        }
        trades['regime'] = trades['trade_date'].map(regime_map).fillna(REGIME_NEUTRAL)

    results = {}
    for policy in policies:
        mask = trades.apply(
            lambda r: policy.is_enabled(r['strategy'], r['regime']), axis=1
        )
        results[policy.name] = trades[mask].copy()
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Policy comparison
# ─────────────────────────────────────────────────────────────────────────────

def compare_policies(
    simulation_results: Dict[str, pd.DataFrame],
    baseline_key: str = 'BASELINE',
) -> pd.DataFrame:
    """
    Compute aggregate metrics for each policy and return a comparison table.
    Includes a `vs_baseline_pnl` delta column.
    """
    rows = []
    for policy_name, df in simulation_results.items():
        if df.empty:
            rows.append({
                'policy': policy_name, 'n_trades': 0,
                'win_rate': None, 'total_pnl': 0, 'profit_factor': None,
                'avg_r': None, 'max_drawdown': 0, 'expectancy': None,
                'vs_baseline_pnl': None,
            })
            continue
        pnl = df['net_pnl'].dropna()
        winners = pnl[pnl > 0]
        losers  = pnl[pnl <= 0]
        win_rate  = len(winners) / len(pnl)
        avg_win   = winners.mean() if len(winners) else 0.0
        avg_loss  = losers.mean()  if len(losers)  else 0.0
        gw = winners.sum(); gl = abs(losers.sum())
        pf = round(gw / gl, 4) if gl > 0 else None
        avg_r = round(df['r_multiple'].mean(), 4) if 'r_multiple' in df else None
        cum = pnl.sort_index().cumsum()
        max_dd = round((cum - cum.cummax()).min(), 2)
        rows.append({
            'policy':       policy_name,
            'n_trades':     len(pnl),
            'win_rate':     round(win_rate, 4),
            'total_pnl':    round(pnl.sum(), 2),
            'profit_factor': pf,
            'avg_r':        avg_r,
            'max_drawdown': max_dd,
            'expectancy':   round(win_rate * avg_win + (1 - win_rate) * avg_loss, 2),
        })

    comparison = pd.DataFrame(rows)
    if baseline_key in simulation_results:
        baseline_pnl = simulation_results[baseline_key]['net_pnl'].sum()
        comparison['vs_baseline_pnl'] = comparison['total_pnl'] - baseline_pnl

    return comparison.sort_values('total_pnl', ascending=False).reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# Per-session attribution
# ─────────────────────────────────────────────────────────────────────────────

def session_pnl_by_policy(
    simulation_results: Dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """Daily P&L for every policy in one DataFrame — useful for drawdown comparison."""
    all_dfs = []
    for policy_name, df in simulation_results.items():
        if df.empty:
            continue
        df['entry_time'] = pd.to_datetime(df['entry_time'])
        daily = (
            df.groupby(df['entry_time'].dt.date)['net_pnl']
            .sum()
            .reset_index()
            .rename(columns={'entry_time': 'date', 'net_pnl': policy_name})
        )
        all_dfs.append(daily)

    if not all_dfs:
        return pd.DataFrame()

    result = all_dfs[0]
    for other in all_dfs[1:]:
        result = result.merge(other, on='date', how='outer')
    return result.sort_values('date').fillna(0).reset_index(drop=True)
