"""
Phase 4.8B.2 — Market regime tagger.

Classifies each trading session into one of three regimes based on the
empirical evidence from segmentation analysis (Phase 4.8B.1):

  GOOD_ORB  — structured, continuation-friendly conditions
  BAD_ORB   — exhaustion / trap / reversal-prone conditions
  NEUTRAL   — neither clearly good nor bad

Classification is intentionally simple and derived from data, not heuristics.
Do NOT hardcode these thresholds inside strategy logic — keep the regime layer
separate so it can evolve independently.

Evidence base (50 real sessions, 2026-03-01 → 2026-05-18):
  OR width 0.2-0.4% + medium gap + high vol → PF 2.45, +₹11,539
  Very wide OR >0.6%                        → PF 0.46, -₹29,989
  Flat open <0.2% gap                       → PF 0.18, -₹26,634
  Medium vol (0.5-1% range)                 → PF 0.19, -₹31,536
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from src.analytics.segmentation import build_session_features


# ─────────────────────────────────────────────────────────────────────────────
# Thresholds — FROZEN v1 (2026-05-19)
#
# Derived from Phase 4.8B.1 segmentation on 50 real sessions (2026-03-01 →
# 2026-05-18). Do NOT adjust these to fit new data — that is curve fitting.
# To test a new definition, create a v2 alongside this one, compare explicitly.
# ─────────────────────────────────────────────────────────────────────────────

# OR width as % of price midpoint
GOOD_ORB_OR_WIDTH_MIN = 0.002   # 0.2%
GOOD_ORB_OR_WIDTH_MAX = 0.004   # 0.4%
BAD_ORB_OR_WIDTH_MIN  = 0.006   # >0.6% = very wide (exhaustion)

# Gap: abs(open - prior_close) / prior_close
GOOD_ORB_GAP_MIN = 0.005        # 0.5%
GOOD_ORB_GAP_MAX = 0.010        # 1.0%
BAD_ORB_GAP_MAX  = 0.002        # <0.2% = flat open

# Session volatility: (high - low) / open
GOOD_ORB_VOL_MIN = 0.010        # ≥1.0% range (high or very_high bucket)
BAD_ORB_VOL_MIN  = 0.005        # 0.5-1.0% range = medium bucket (worst performer)
BAD_ORB_VOL_MAX  = 0.010

REGIME_GOOD    = 'GOOD_ORB'
REGIME_BAD     = 'BAD_ORB'
REGIME_NEUTRAL = 'NEUTRAL'


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionRegime:
    session_date: date
    regime: str                         # GOOD_ORB | BAD_ORB | NEUTRAL
    or_width_pct: Optional[float]       # None when OR data unavailable
    gap_abs_pct: float
    session_range_pct: float
    reasons: List[str] = field(default_factory=list)   # which condition(s) fired

    def is_good(self) -> bool:
        return self.regime == REGIME_GOOD

    def is_bad(self) -> bool:
        return self.regime == REGIME_BAD


# ─────────────────────────────────────────────────────────────────────────────
# Classifier
# ─────────────────────────────────────────────────────────────────────────────

def classify_session(
    session_range_pct: float,
    gap_abs_pct: float,
    or_width_pct: Optional[float] = None,
) -> tuple[str, list[str]]:
    """
    Return (regime_label, [reasons]).
    Any BAD condition overrides GOOD — pessimistic by default.
    """
    bad_reasons = []
    good_reasons = []

    # BAD conditions — any one makes the session BAD_ORB
    if or_width_pct is not None and or_width_pct > BAD_ORB_OR_WIDTH_MIN:
        bad_reasons.append(f'or_width {or_width_pct:.3%} > 0.6% (exhaustion)')
    if gap_abs_pct < BAD_ORB_GAP_MAX:
        bad_reasons.append(f'gap {gap_abs_pct:.3%} < 0.2% (flat open)')
    if BAD_ORB_VOL_MIN <= session_range_pct < BAD_ORB_VOL_MAX:
        bad_reasons.append(f'range {session_range_pct:.3%} in 0.5-1% (medium vol)')

    if bad_reasons:
        return REGIME_BAD, bad_reasons

    # GOOD conditions — ALL three must hold
    or_ok = (or_width_pct is not None and
             GOOD_ORB_OR_WIDTH_MIN <= or_width_pct <= GOOD_ORB_OR_WIDTH_MAX)
    gap_ok = GOOD_ORB_GAP_MIN <= gap_abs_pct <= GOOD_ORB_GAP_MAX
    vol_ok = session_range_pct >= GOOD_ORB_VOL_MIN

    if or_ok:
        good_reasons.append(f'or_width {or_width_pct:.3%} in 0.2-0.4%')
    if gap_ok:
        good_reasons.append(f'gap {gap_abs_pct:.3%} in 0.5-1%')
    if vol_ok:
        good_reasons.append(f'range {session_range_pct:.3%} ≥ 1% (high vol)')

    if or_ok and gap_ok and vol_ok:
        return REGIME_GOOD, good_reasons

    return REGIME_NEUTRAL, good_reasons   # partial match → neutral


# ─────────────────────────────────────────────────────────────────────────────
# Session-level tagging
# ─────────────────────────────────────────────────────────────────────────────

def tag_sessions(
    ohlcv_df: pd.DataFrame,
    or_width_by_date: Optional[Dict[date, float]] = None,
) -> List[SessionRegime]:
    """
    Tag every session in ohlcv_df with a regime.

    or_width_by_date: optional dict {date → or_width_pct} derived from the
    backtest trades metadata (more accurate than re-computing here). When not
    provided, OR width is computed from the first 15 bars of the session.
    """
    session_df = build_session_features(ohlcv_df)
    ohlcv_df = ohlcv_df.copy()
    ohlcv_df['timestamp'] = pd.to_datetime(ohlcv_df['timestamp'])
    ohlcv_df['date'] = ohlcv_df['timestamp'].dt.date

    regimes = []
    for _, row in session_df.iterrows():
        d = row['session_date']
        if isinstance(d, str):
            import datetime
            d = datetime.date.fromisoformat(d)

        # Use trade-derived OR width if provided; otherwise estimate from first 15 bars
        if or_width_by_date and d in or_width_by_date:
            or_width_pct = or_width_by_date[d]
        else:
            or_width_pct = _estimate_or_width(ohlcv_df, d, row['session_open'])

        regime, reasons = classify_session(
            session_range_pct=float(row['session_range_pct']),
            gap_abs_pct=float(row['gap_abs_pct']),
            or_width_pct=or_width_pct,
        )
        regimes.append(SessionRegime(
            session_date=d,
            regime=regime,
            or_width_pct=or_width_pct,
            gap_abs_pct=float(row['gap_abs_pct']),
            session_range_pct=float(row['session_range_pct']),
            reasons=reasons,
        ))

    return regimes


def _estimate_or_width(ohlcv_df: pd.DataFrame, day: date, session_open: float) -> Optional[float]:
    """Approximate OR width from the OR window (09:15–09:30 = first 15 bars)."""
    sess = ohlcv_df[ohlcv_df['date'] == day].sort_values('timestamp')
    or_bars = sess.head(15)
    if or_bars.empty or session_open <= 0:
        return None
    or_high = float(or_bars['high'].max())
    or_low = float(or_bars['low'].min())
    midpoint = (or_high + or_low) / 2
    return (or_high - or_low) / midpoint if midpoint > 0 else None


# ─────────────────────────────────────────────────────────────────────────────
# P&L breakdown by regime
# ─────────────────────────────────────────────────────────────────────────────

def pnl_by_regime(
    trades_df: pd.DataFrame,
    regimes: List[SessionRegime],
) -> pd.DataFrame:
    """
    Join trades with regime tags and compute per-regime P&L metrics.
    Returns a DataFrame with one row per (strategy, regime).
    """
    regime_map = {r.session_date: r.regime for r in regimes}

    trades = trades_df.copy()
    trades['entry_time'] = pd.to_datetime(trades['entry_time'])
    trades['trade_date'] = trades['entry_time'].dt.date
    trades['regime'] = trades['trade_date'].map(regime_map).fillna(REGIME_NEUTRAL)

    rows = []
    for (strategy, regime), group in trades.groupby(['strategy', 'regime']):
        pnl = group['net_pnl'].dropna()
        if pnl.empty:
            continue
        winners = pnl[pnl > 0]
        losers  = pnl[pnl <= 0]
        win_rate = len(winners) / len(pnl)
        avg_win  = winners.mean() if len(winners) else 0.0
        avg_loss = losers.mean()  if len(losers)  else 0.0
        gross_wins   = winners.sum()
        gross_losses = abs(losers.sum())
        pf = gross_wins / gross_losses if gross_losses > 0 else None
        avg_r = group['r_multiple'].mean() if 'r_multiple' in group else None
        cum = pnl.cumsum()
        max_dd = (cum - cum.cummax()).min()

        rows.append({
            'strategy':      strategy,
            'regime':        regime,
            'n_trades':      len(pnl),
            'win_rate':      round(win_rate, 4),
            'avg_win':       round(avg_win, 2),
            'avg_loss':      round(avg_loss, 2),
            'expectancy':    round(win_rate * avg_win + (1 - win_rate) * avg_loss, 2),
            'total_pnl':     round(pnl.sum(), 2),
            'profit_factor': round(pf, 4) if pf is not None else None,
            'avg_r':         round(avg_r, 4) if avg_r is not None else None,
            'max_drawdown':  round(max_dd, 2),
        })

    return pd.DataFrame(rows).sort_values(['strategy', 'regime']).reset_index(drop=True)


def regimes_to_df(regimes: List[SessionRegime]) -> pd.DataFrame:
    return pd.DataFrame([{
        'session_date':       r.session_date,
        'regime':             r.regime,
        'or_width_pct':       round(r.or_width_pct * 100, 3) if r.or_width_pct is not None else None,
        'gap_abs_pct':        round(r.gap_abs_pct * 100, 3),
        'session_range_pct':  round(r.session_range_pct * 100, 3),
        'reasons':            '; '.join(r.reasons),
    } for r in regimes])
