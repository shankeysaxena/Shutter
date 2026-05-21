"""
Zerodha historical candle loader.

Fetches 1-minute OHLCV candles from the Kite Connect historical API, normalises
them to the canonical format used throughout strategy_lab, and persists to disk.

Canonical DataFrame columns:
  timestamp  – naive IST datetime64[ns], e.g. 2024-01-02 09:15:00
  instrument – symbol string, e.g. 'NIFTY'
  open / high / low / close – float64
  volume     – int64

Kite rate limits:
  ~3 historical requests per second. Chunked requests include a 0.4-second sleep.

Kite minute-data window limit:
  60 calendar days per request. Longer ranges are auto-chunked.
"""
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd

from src.integrations.zerodha.config import KITE_MINUTE_CHUNK_DAYS, KiteConfig


def _get_kite_connect():
    try:
        from kiteconnect import KiteConnect
        return KiteConnect
    except ImportError as e:
        raise ImportError(
            "kiteconnect is required. Install with: pip install -r requirements.txt"
        ) from e

_CANONICAL_COLUMNS = ['timestamp', 'instrument', 'open', 'high', 'low', 'close', 'volume']
_MARKET_OPEN = (9, 15)
_MARKET_CLOSE = (15, 30)
_SLEEP_BETWEEN_CHUNKS = 0.4   # seconds


def build_kite_client(config: KiteConfig, access_token: str):
    kite = _get_kite_connect()(api_key=config.api_key)
    kite.set_access_token(access_token)
    return kite


def fetch_candles(
    kite: KiteConnect,
    instrument_token: int,
    symbol: str,
    from_date: date,
    to_date: date,
    interval: str = 'minute',
) -> pd.DataFrame:
    """
    Fetch candles for the given instrument and date range. Automatically
    chunks requests that span more than KITE_MINUTE_CHUNK_DAYS days.
    Returns a validated, deduplicated canonical DataFrame.
    """
    chunk_days = KITE_MINUTE_CHUNK_DAYS if interval == 'minute' else 200
    chunks = _date_chunks(from_date, to_date, chunk_days)
    all_rows: List[dict] = []

    for i, (chunk_start, chunk_end) in enumerate(chunks):
        if i > 0:
            time.sleep(_SLEEP_BETWEEN_CHUNKS)
        raw = kite.historical_data(
            instrument_token=instrument_token,
            from_date=chunk_start,
            to_date=chunk_end,
            interval=interval,
            continuous=False,
            oi=False,
        )
        all_rows.extend(raw)

    if not all_rows:
        raise ValueError(
            f"Zero rows returned for {symbol} {from_date}→{to_date} interval={interval}. "
            f"Verify the instrument_token, date range, and that your access_token is valid."
        )

    df = _normalise(all_rows, symbol)
    df = _validate(df, symbol)
    return df


def save_candles(df: pd.DataFrame, symbol: str, out_dir: Optional[Path] = None) -> Path:
    """
    Persist a canonical DataFrame to data/raw/<SYMBOL>.csv, appending if
    the file already exists and deduplicating on timestamp.
    Returns the path written.
    """
    from src.integrations.zerodha.config import _PROJECT_ROOT
    dest = (out_dir or (_PROJECT_ROOT / 'data' / 'raw')) / f'{symbol}.csv'
    dest.parent.mkdir(parents=True, exist_ok=True)   # ensure directory exists

    if dest.exists():
        existing = pd.read_csv(dest, parse_dates=['timestamp'])
        df = pd.concat([existing, df], ignore_index=True)

    df = (
        df.drop_duplicates(subset=['timestamp'])
          .sort_values('timestamp')
          .reset_index(drop=True)
    )
    df.to_csv(dest, index=False)
    return dest


# -----------------------------------------------------------------------
# Internal helpers
# -----------------------------------------------------------------------

def _date_chunks(from_date: date, to_date: date, chunk_days: int):
    """Split a date range into (start, end) pairs each at most chunk_days wide."""
    chunks = []
    cur = from_date
    while cur <= to_date:
        end = min(cur + timedelta(days=chunk_days - 1), to_date)
        chunks.append((cur, end))
        cur = end + timedelta(days=1)
    return chunks


def _normalise(raw: list, symbol: str) -> pd.DataFrame:
    """
    Convert Kite's raw candle list to the canonical DataFrame.
    Kite returns: {'date': datetime, 'open': float, 'high', 'low', 'close', 'volume'}
    Timestamps arrive as tz-aware IST datetimes; strip tz to match existing data convention.
    """
    rows = []
    for r in raw:
        ts = r['date']
        # Strip timezone (Kite returns IST-aware; strategy_lab stores naive IST)
        if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        rows.append({
            'timestamp': ts,
            'instrument': symbol,
            'open':   float(r['open']),
            'high':   float(r['high']),
            'low':    float(r['low']),
            'close':  float(r['close']),
            'volume': int(r['volume']),
        })
    df = pd.DataFrame(rows, columns=_CANONICAL_COLUMNS)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    return df


def _validate(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Post-fetch validation. Issues warnings for data-quality problems but only
    raises on fatal issues (zero rows after dedup).
    """
    import warnings

    # Required columns
    missing = [c for c in _CANONICAL_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns in fetched data for {symbol}: {missing}")

    # Dedup + sort
    before = len(df)
    df = df.drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)
    if len(df) < before:
        warnings.warn(f"{symbol}: removed {before - len(df)} duplicate timestamps")

    if df.empty:
        raise ValueError(f"No rows remaining for {symbol} after deduplication")

    # Market-hours filter (advisory — only warn, do not drop)
    t = df['timestamp'].dt
    out_of_hours = df[
        (t.hour < _MARKET_OPEN[0])
        | ((t.hour == _MARKET_OPEN[0]) & (t.minute < _MARKET_OPEN[1]))
        | (t.hour > _MARKET_CLOSE[0])
        | ((t.hour == _MARKET_CLOSE[0]) & (t.minute > _MARKET_CLOSE[1]))
    ]
    if not out_of_hours.empty:
        warnings.warn(
            f"{symbol}: {len(out_of_hours)} bars outside market hours "
            f"(09:15–15:30). Review and filter if needed."
        )

    # Session gap detection
    dates = sorted(df['timestamp'].dt.date.unique())
    if len(dates) > 1:
        _warn_missing_sessions(dates, symbol)

    return df


def _warn_missing_sessions(session_dates: list, symbol: str) -> None:
    import warnings
    from pandas.tseries.offsets import BDay
    first, last = session_dates[0], session_dates[-1]
    # Build expected business days; count gaps in fetched dates
    expected = {
        d.date() for d in pd.date_range(first, last, freq=BDay())
    }
    fetched = set(session_dates)
    missing = sorted(expected - fetched)
    if missing:
        warnings.warn(
            f"{symbol}: {len(missing)} expected trading session(s) not in fetched data. "
            f"First few: {missing[:5]}. Check for holidays or API gaps."
        )
