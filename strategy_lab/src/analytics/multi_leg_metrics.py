"""
Multi-leg trade analytics.

Per spec v2 §12: distinct from the single-leg `MetricsEngine.calculate_summary`
because multi-leg structures have different P&L drivers (entry credit, net debit
on close, exit reasons) that don't map to the directional/R-multiple framing.

Exposes:
- compute_multi_leg_summary(trades) -> Dict
- multi_leg_leg_ledger(trades) -> DataFrame (one row per leg fill)
- exit_reason_breakdown(trades) -> DataFrame
"""
import json
import statistics
from collections import defaultdict
from typing import Dict, List

import pandas as pd

from src.core.option_models import MultiLegTrade


def compute_multi_leg_summary(trades: List[MultiLegTrade]) -> Dict:
    """High-level metrics for a flat list of MultiLegTrade. Empty input → minimal dict."""
    if not trades:
        return {'total_trades': 0}

    pnls = [t.net_pnl for t in trades if t.net_pnl is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    credits = [t.net_entry_credit for t in trades]
    max_losses = [t.metadata.get('max_loss_per_lot_rupees', 0) for t in trades]

    by_reason: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        by_reason[t.exit_reason or 'NONE'].append(t.net_pnl or 0)

    return {
        'total_trades':         len(trades),
        'win_rate':             round(len(wins) / len(trades), 4),
        'avg_win':              round(statistics.mean(wins), 2) if wins else 0.0,
        'avg_loss':             round(statistics.mean(losses), 2) if losses else 0.0,
        'total_net_pnl':        round(sum(pnls), 2),
        'total_gross_pnl':      round(sum(t.gross_pnl or 0 for t in trades), 2),
        'avg_net_credit':       round(statistics.mean(credits), 2) if credits else 0.0,
        'avg_max_loss_per_lot': round(statistics.mean(max_losses), 2) if max_losses else 0.0,
        'max_drawdown':         round(_running_drawdown(pnls), 2),
        'exits_by_reason': {
            reason: {'n': len(vals), 'total_pnl': round(sum(vals), 2)}
            for reason, vals in by_reason.items()
        },
    }


def multi_leg_leg_ledger(trades: List[MultiLegTrade]) -> pd.DataFrame:
    """One row per leg fill (both entry and exit). Useful for fill-level diagnostics."""
    rows = []
    for t in trades:
        for fill in t.entry_fills:
            rows.append(_leg_row(t, fill, side_event='ENTRY'))
        for fill in t.exit_fills:
            rows.append(_leg_row(t, fill, side_event='EXIT'))
    return pd.DataFrame(rows)


def exit_reason_breakdown(trades: List[MultiLegTrade]) -> pd.DataFrame:
    """exit_reason × {count, total_pnl, avg_pnl}. Empty input → empty frame."""
    if not trades:
        return pd.DataFrame(columns=['exit_reason', 'count', 'total_pnl', 'avg_pnl'])
    by_reason: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        by_reason[t.exit_reason or 'NONE'].append(t.net_pnl or 0)
    rows = [
        {
            'exit_reason': reason,
            'count': len(vals),
            'total_pnl': round(sum(vals), 2),
            'avg_pnl': round(sum(vals) / len(vals), 2),
        }
        for reason, vals in sorted(by_reason.items(), key=lambda kv: -len(kv[1]))
    ]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------

def _leg_row(trade: MultiLegTrade, fill, side_event: str) -> Dict:
    leg = fill.leg
    return {
        'trade_id': trade.trade_id,
        'strategy': trade.strategy_name,
        'instrument': trade.instrument,
        'structure': trade.structure_type,
        'side_event': side_event,       # ENTRY or EXIT
        'leg_side': leg.side,           # BUY or SELL
        'option_type': leg.option_type,
        'strike': leg.strike,
        'expiry': leg.expiry,
        'qty': leg.qty,
        'lot_size': trade.lot_size,
        'fill_price': fill.fill_price,
        'fill_time': fill.fill_time,
    }


def _running_drawdown(pnls: List[float]) -> float:
    """Min cumulative drawdown from a chronological P&L series. Returns 0 if list empty."""
    if not pnls:
        return 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    return max_dd
