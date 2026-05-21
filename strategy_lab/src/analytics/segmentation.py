"""
Phase 4.8B — Segmentation analysis.

Takes a trades ledger and the underlying OHLCV data, tags each trade with
session-level context, then buckets trades across four dimensions to reveal
under what market conditions strategies work:

  1. OR width      — breakout space available
  2. Gap size      — overnight imbalance
  3. Entry time    — time-of-day decay
  4. Volatility    — session expansion

Outputs one DataFrame per dimension, each row = one bucket.

Usage:
    from src.analytics.segmentation import build_session_features, segment_trades
    session_df = build_session_features(ohlcv_df)
    results    = segment_trades(trades_df, session_df)
    # results is a dict: {dimension_name: pd.DataFrame}
"""
import json
import warnings
from typing import Dict, Optional

import pandas as pd
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Bucket definitions
# ─────────────────────────────────────────────────────────────────────────────

OR_WIDTH_BINS = [0, 0.002, 0.004, 0.006, np.inf]
OR_WIDTH_LABELS = ['tight <0.2%', 'normal 0.2-0.4%', 'wide 0.4-0.6%', 'very_wide >0.6%']

GAP_BINS = [0, 0.002, 0.005, 0.010, np.inf]
GAP_LABELS = ['flat <0.2%', 'small 0.2-0.5%', 'medium 0.5-1%', 'large >1%']

# Session volatility: (high - low) / open
VOL_BINS = [0, 0.005, 0.010, 0.015, np.inf]
VOL_LABELS = ['low <0.5%', 'medium 0.5-1%', 'high 1-1.5%', 'very_high >1.5%']

# Entry time buckets (hour only for simplicity)
TIME_BINS = [0, 630, 660, 720, 900]   # minutes since midnight
TIME_LABELS = ['9:30-10:00', '10:00-11:00', '11:00-12:00', '12:00+']


# ─────────────────────────────────────────────────────────────────────────────
# Session-level feature extraction
# ─────────────────────────────────────────────────────────────────────────────

def build_session_features(ohlcv_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute one-row-per-session summary features from a canonical OHLCV DataFrame.

    Returns a DataFrame with columns:
        session_date, session_open, session_high, session_low, session_close,
        prior_close, gap_pct, gap_abs, session_range_pct, or_open (first bar open)
    """
    df = ohlcv_df.copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df['date'] = df['timestamp'].dt.date

    rows = []
    dates = sorted(df['date'].unique())
    for i, d in enumerate(dates):
        sess = df[df['date'] == d].sort_values('timestamp')
        if sess.empty:
            continue
        s_open = float(sess['open'].iloc[0])
        s_high = float(sess['high'].max())
        s_low = float(sess['low'].min())
        s_close = float(sess['close'].iloc[-1])

        prior_close = None
        gap_pct = 0.0
        if i > 0:
            prev = df[df['date'] == dates[i - 1]]
            if not prev.empty:
                prior_close = float(prev['close'].iloc[-1])
                gap_pct = (s_open - prior_close) / prior_close

        session_range_pct = (s_high - s_low) / s_open if s_open > 0 else 0.0

        rows.append({
            'session_date':      d,
            'session_open':      s_open,
            'session_high':      s_high,
            'session_low':       s_low,
            'session_close':     s_close,
            'prior_close':       prior_close,
            'gap_pct':           gap_pct,
            'gap_abs_pct':       abs(gap_pct),
            'session_range_pct': session_range_pct,
        })

    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Trade enrichment
# ─────────────────────────────────────────────────────────────────────────────

def enrich_trades(
    trades_df: pd.DataFrame,
    session_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Join trades with session features and add derived bucketing columns.
    Mutates a copy of trades_df.
    """
    trades = trades_df.copy()
    trades['entry_time'] = pd.to_datetime(trades['entry_time'])
    trades['trade_date'] = trades['entry_time'].dt.date

    # Parse or_high / or_low from metadata to compute OR width
    trades['or_width_pct'] = trades['metadata_json'].apply(_extract_or_width_pct)

    # Join session features
    session_df = session_df.copy()
    session_df['session_date'] = pd.to_datetime(session_df['session_date']).dt.date
    trades = trades.merge(
        session_df[['session_date', 'gap_abs_pct', 'session_range_pct', 'session_open']],
        left_on='trade_date', right_on='session_date', how='left',
    )

    # Entry time in minutes since midnight for bucketing
    trades['entry_minutes'] = (
        trades['entry_time'].dt.hour * 60 + trades['entry_time'].dt.minute
    )

    # Apply buckets
    trades['or_width_bucket'] = pd.cut(
        trades['or_width_pct'].clip(lower=0),
        bins=OR_WIDTH_BINS, labels=OR_WIDTH_LABELS, right=False,
    ).astype(str)

    trades['gap_bucket'] = pd.cut(
        trades['gap_abs_pct'].clip(lower=0),
        bins=GAP_BINS, labels=GAP_LABELS, right=False,
    ).astype(str)

    trades['volatility_bucket'] = pd.cut(
        trades['session_range_pct'].clip(lower=0),
        bins=VOL_BINS, labels=VOL_LABELS, right=False,
    ).astype(str)

    trades['time_bucket'] = pd.cut(
        trades['entry_minutes'].clip(lower=TIME_BINS[0]),
        bins=TIME_BINS, labels=TIME_LABELS, right=False,
    ).astype(str)

    return trades


def _extract_or_width_pct(metadata_json: str) -> float:
    """Parse or_high / or_low from metadata and return width as % of midpoint."""
    try:
        meta = json.loads(metadata_json) if isinstance(metadata_json, str) else {}
        or_high = meta.get('or_high')
        or_low = meta.get('or_low')
        if or_high and or_low and or_low > 0:
            return (or_high - or_low) / ((or_high + or_low) / 2)
    except (json.JSONDecodeError, TypeError):
        pass
    return float('nan')


# ─────────────────────────────────────────────────────────────────────────────
# Metrics per bucket
# ─────────────────────────────────────────────────────────────────────────────

def _bucket_metrics(group: pd.DataFrame) -> pd.Series:
    """Compute trade metrics for one bucket group."""
    pnl = group['net_pnl'].dropna()
    if pnl.empty:
        return pd.Series({'n_trades': 0})

    winners = pnl[pnl > 0]
    losers = pnl[pnl <= 0]
    win_rate = len(winners) / len(pnl)
    avg_win = winners.mean() if len(winners) else 0.0
    avg_loss = losers.mean() if len(losers) else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    gross_wins = winners.sum()
    gross_losses = abs(losers.sum())
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else None
    avg_r = group['r_multiple'].mean() if 'r_multiple' in group else None

    # Drawdown within bucket (chronological)
    cum = pnl.cumsum()
    max_dd = (cum - cum.cummax()).min()

    return pd.Series({
        'n_trades':      len(pnl),
        'win_rate':      round(win_rate, 4),
        'avg_win':       round(avg_win, 2),
        'avg_loss':      round(avg_loss, 2),
        'expectancy':    round(expectancy, 2),
        'total_pnl':     round(pnl.sum(), 2),
        'profit_factor': round(profit_factor, 4) if profit_factor is not None else None,
        'avg_r':         round(avg_r, 4) if avg_r is not None else None,
        'max_drawdown':  round(max_dd, 2),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def segment_trades(
    trades_df: pd.DataFrame,
    session_df: pd.DataFrame,
    strategies: Optional[list] = None,
) -> Dict[str, pd.DataFrame]:
    """
    Produce segmentation tables for the four dimensions.

    Parameters
    ----------
    trades_df   : unified ledger DataFrame (from trades.csv)
    session_df  : output of build_session_features()
    strategies  : filter to specific strategies; None = all

    Returns
    -------
    dict with keys: by_or_width, by_gap, by_time, by_volatility
    Each value is a DataFrame with one row per bucket, metrics as columns.
    """
    if strategies:
        trades_df = trades_df[trades_df['strategy'].isin(strategies)]

    # Only single-leg trades have R-multiples and direction context
    sl = trades_df[trades_df['trade_type'] == 'single_leg'].copy()
    if sl.empty:
        warnings.warn("No single-leg trades found — segmentation requires single-leg trades.")
        return {}

    enriched = enrich_trades(sl, session_df)

    results = {}
    for bucket_col, name in [
        ('or_width_bucket',   'by_or_width'),
        ('gap_bucket',        'by_gap'),
        ('time_bucket',       'by_time'),
        ('volatility_bucket', 'by_volatility'),
    ]:
        valid = enriched[enriched[bucket_col] != 'nan']
        if valid.empty:
            continue
        tbl = valid.groupby(bucket_col, observed=True).apply(
            _bucket_metrics, include_groups=False
        ).reset_index()
        tbl = tbl.rename(columns={bucket_col: 'bucket'})
        results[name] = tbl

    return results


def save_segments(
    segments: Dict[str, pd.DataFrame],
    output_dir,
) -> None:
    """Write each segment table to a CSV under output_dir/segments/."""
    from pathlib import Path
    out = Path(output_dir) / 'segments'
    out.mkdir(parents=True, exist_ok=True)
    for name, df in segments.items():
        df.to_csv(out / f'{name}.csv', index=False)
