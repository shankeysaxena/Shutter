"""
Zerodha instrument discovery.

Downloads and caches the Kite instruments dump (CSV, refreshed daily).
Provides resolve_instrument_token() for mapping a human-readable symbol to
the integer token required by the historical API.

Segment codes used by Kite:
  NSE        — cash equities
  NFO-FUT    — NIFTY/BANKNIFTY futures
  NFO-OPT    — NIFTY/BANKNIFTY options
  NSE-INDICES — indices (NIFTY 50, NIFTY BANK)

Well-known index tokens (stable across Kite versions, used as fallback):
  NIFTY 50    → 256265
  NIFTY BANK  → 260105
"""
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from src.integrations.zerodha.config import INSTRUMENTS_DIR, KiteConfig


def _get_kite_connect():
    try:
        from kiteconnect import KiteConnect
        return KiteConnect
    except ImportError as e:
        raise ImportError(
            "kiteconnect is required. Install with: pip install -r requirements.txt"
        ) from e


_FALLBACK_TOKENS = {
    ('NSE', 'NIFTY 50'): 256265,
    ('NSE', 'NIFTY'):    256265,   # alias
    ('NSE', 'NIFTY BANK'): 260105,
    ('NSE', 'BANKNIFTY'): 260105,  # alias
}

_EXCHANGE_MAP = {
    'NSE': 'NSE',
    'NFO-FUT': 'NFO',
    'NFO-OPT': 'NFO',
    'NSE-INDICES': 'NSE',
}


def instruments_file_path() -> Path:
    return INSTRUMENTS_DIR / f"instruments_{date.today().isoformat()}.csv"


def refresh_instruments(config: KiteConfig, access_token: str) -> Path:
    """
    Download the full instruments dump from Kite and save it as a dated CSV.
    Returns the path to the saved file.
    """
    kite = _get_kite_connect()(api_key=config.api_key)
    kite.set_access_token(access_token)
    instruments = kite.instruments()
    df = pd.DataFrame(instruments)
    INSTRUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = instruments_file_path()
    df.to_csv(path, index=False)
    return path


def load_instruments(path: Optional[Path] = None) -> pd.DataFrame:
    """Load instruments from the dated CSV, or return empty frame if not present."""
    p = path or instruments_file_path()
    if not p.exists():
        return pd.DataFrame()
    return pd.read_csv(p, low_memory=False)


def resolve_instrument_token(
    symbol: str,
    segment: str,
    instruments_df: Optional[pd.DataFrame] = None,
) -> int:
    """
    Resolve symbol + segment to an integer instrument_token.

    Parameters
    ----------
    symbol  : 'NIFTY', 'BANKNIFTY', 'RELIANCE', etc.
    segment : 'NSE', 'NFO-FUT', 'NFO-OPT', 'NSE-INDICES'

    Lookup priority:
      1. Fallback table for well-known index tokens (always reliable)
      2. Loaded instruments CSV filtered by tradingsymbol + segment
    """
    # Normalize
    symbol = symbol.upper().strip()
    segment = segment.upper().strip()

    key = (segment.split('-')[0], symbol)
    if key in _FALLBACK_TOKENS:
        return _FALLBACK_TOKENS[key]

    df = instruments_df if instruments_df is not None else load_instruments()
    if df.empty:
        raise LookupError(
            f"Instruments CSV not found. Run: python -m src.cli.main zerodha instruments refresh"
        )

    exchange = _EXCHANGE_MAP.get(segment, segment.split('-')[0])
    mask = (
        (df['tradingsymbol'].str.upper() == symbol)
        & (df['exchange'].str.upper() == exchange)
    )
    if 'segment' in df.columns:
        mask &= df['segment'].str.upper() == segment

    matches = df[mask]
    if matches.empty:
        raise LookupError(f"No instrument found for symbol={symbol!r}, segment={segment!r}")
    return int(matches.iloc[0]['instrument_token'])
