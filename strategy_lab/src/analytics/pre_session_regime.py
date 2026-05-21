"""
Pre-session regime estimator.

Classifies today's regime using ONLY information available before 09:30:
  - Yesterday's OR width (known before today's open)
  - Yesterday's session range (known before today's open)
  - Today's opening gap (first bar open vs yesterday's close; known at 09:15)

This is the deployable version of the regime tagger. It introduces a 1-session
lag but can be applied in live trading.

Accuracy is compared against the oracle (in-session) regime to measure
how much predictive power the pre-session signals carry.

Thresholds are derived from the same empirical evidence as regime.py v1
(FROZEN — do not adjust to fit new data without creating v2).
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.analytics.regime import (
    BAD_ORB_GAP_MAX,
    BAD_ORB_OR_WIDTH_MIN,
    GOOD_ORB_GAP_MAX,
    GOOD_ORB_GAP_MIN,
    GOOD_ORB_OR_WIDTH_MAX,
    GOOD_ORB_OR_WIDTH_MIN,
    GOOD_ORB_VOL_MIN,
    REGIME_BAD,
    REGIME_GOOD,
    REGIME_NEUTRAL,
)


def build_pre_session_features(ohlcv_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute pre-session features for each session from the OHLCV data.
    Returns one row per session with:
      session_date, instrument, opening_gap_pct, prior_or_width_pct,
      prior_session_range_pct, prior_close
    """
    df = ohlcv_df.copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['date'] = df['timestamp'].dt.date

    # Per-session summaries
    session_summaries = []
    dates = sorted(df['date'].unique())
    for i, d in enumerate(dates):
        sess = df[df['date'] == d].sort_values('timestamp')
        if sess.empty:
            continue
        first_bar = sess.iloc[0]
        session_open  = float(first_bar['open'])
        session_close = float(sess.iloc[-1]['close'])
        session_high  = float(sess['high'].max())
        session_low   = float(sess['low'].min())
        # OR width from first 15 bars (09:15–09:29)
        or_bars = sess.head(15)
        or_high = float(or_bars['high'].max())
        or_low  = float(or_bars['low'].min())
        or_mid  = (or_high + or_low) / 2
        or_width = (or_high - or_low) / or_mid if or_mid > 0 else 0.0
        session_summaries.append({
            'date': d,
            'session_open':  session_open,
            'session_close': session_close,
            'session_high':  session_high,
            'session_low':   session_low,
            'or_width_pct':  or_width,
            'session_range_pct': (session_high - session_low) / session_open if session_open > 0 else 0.0,
        })

    summary_df = pd.DataFrame(session_summaries)
    if summary_df.empty:
        return summary_df

    # Pre-session features: shift by 1 to get PRIOR day's values
    result_rows = []
    for i in range(1, len(summary_df)):
        today   = summary_df.iloc[i]
        prior   = summary_df.iloc[i - 1]
        gap_pct = (today['session_open'] - prior['session_close']) / prior['session_close']
        result_rows.append({
            'session_date':          today['date'],
            'opening_gap_pct':       gap_pct,
            'gap_abs_pct':           abs(gap_pct),
            'prior_or_width_pct':    prior['or_width_pct'],
            'prior_session_range_pct': prior['session_range_pct'],
            'prior_close':           prior['session_close'],
        })

    return pd.DataFrame(result_rows)


def classify_pre_session(
    gap_abs_pct: float,
    prior_or_width_pct: float,
    prior_session_range_pct: float,
) -> Tuple[str, List[str]]:
    """
    Classify today's expected regime using only pre-session data.
    Returns (regime_label, [reasons]).

    Logic mirrors regime.py classify_session() but uses prior-day proxies.
    """
    bad_reasons = []
    good_reasons = []

    # BAD signals — any one makes it BAD_ORB
    if prior_or_width_pct > BAD_ORB_OR_WIDTH_MIN:
        bad_reasons.append(f'prior OR width {prior_or_width_pct:.3%} > 0.6% (exhaustion likely)')
    if gap_abs_pct < BAD_ORB_GAP_MAX:
        bad_reasons.append(f'gap {gap_abs_pct:.3%} < 0.2% (flat open)')

    if bad_reasons:
        return REGIME_BAD, bad_reasons

    # GOOD signals — all three proxies must be in range
    or_ok  = GOOD_ORB_OR_WIDTH_MIN <= prior_or_width_pct <= GOOD_ORB_OR_WIDTH_MAX
    gap_ok = GOOD_ORB_GAP_MIN <= gap_abs_pct <= GOOD_ORB_GAP_MAX
    vol_ok = prior_session_range_pct >= GOOD_ORB_VOL_MIN

    if or_ok:
        good_reasons.append(f'prior OR {prior_or_width_pct:.3%} in 0.2-0.4%')
    if gap_ok:
        good_reasons.append(f'gap {gap_abs_pct:.3%} in 0.5-1%')
    if vol_ok:
        good_reasons.append(f'prior range {prior_session_range_pct:.3%} ≥ 1%')

    if or_ok and gap_ok and vol_ok:
        return REGIME_GOOD, good_reasons

    return REGIME_NEUTRAL, good_reasons


def estimate_pre_session_regimes(
    ohlcv_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Compute pre-session regime estimates for all sessions.
    Returns DataFrame with columns: session_date, pre_session_regime, reasons, + feature cols.
    """
    features = build_pre_session_features(ohlcv_df)
    if features.empty:
        return features

    results = []
    for _, row in features.iterrows():
        regime, reasons = classify_pre_session(
            gap_abs_pct=float(row['gap_abs_pct']),
            prior_or_width_pct=float(row['prior_or_width_pct']),
            prior_session_range_pct=float(row['prior_session_range_pct']),
        )
        results.append({
            'session_date':         row['session_date'],
            'pre_session_regime':   regime,
            'gap_abs_pct':          round(row['gap_abs_pct'] * 100, 3),
            'prior_or_width_pct':   round(row['prior_or_width_pct'] * 100, 3),
            'prior_range_pct':      round(row['prior_session_range_pct'] * 100, 3),
            'reasons':              '; '.join(reasons),
        })
    return pd.DataFrame(results)


def accuracy_vs_oracle(
    pre_session_df: pd.DataFrame,
    oracle_df: pd.DataFrame,
) -> Dict:
    """
    Compare pre-session classifications against oracle (in-session) regime.
    Returns agreement rates and confusion counts.
    """
    oracle_map = {
        pd.to_datetime(r['session_date']).date(): r['regime']
        for _, r in oracle_df.iterrows()
    }

    matched = 0
    total = 0
    confusion: Dict[Tuple[str, str], int] = {}

    for _, row in pre_session_df.iterrows():
        d = pd.to_datetime(row['session_date']).date()
        if d not in oracle_map:
            continue
        pred   = row['pre_session_regime']
        actual = oracle_map[d]
        key    = (pred, actual)
        confusion[key] = confusion.get(key, 0) + 1
        if pred == actual:
            matched += 1
        total += 1

    return {
        'total_sessions': total,
        'exact_match_pct': round(matched / total, 4) if total else 0,
        'confusion': {f'{p}→{a}': n for (p, a), n in sorted(confusion.items())},
    }
