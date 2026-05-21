"""
Option instrument token resolution for Zerodha Kite.

Resolves (underlying, expiry, strike, option_type) → instrument_token
from the Kite instruments CSV. Caches the filtered NFO-OPT slice in memory
so the same CSV isn't re-read for every lookup in a session.
"""
from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# Strike key: (underlying, expiry_iso, strike, option_type)
_TokenKey = Tuple[str, str, float, str]


class OptionInstrumentResolver:
    """
    Resolves Kite instrument tokens for option contracts.

    Usage:
        resolver = OptionInstrumentResolver()
        token = resolver.resolve('NIFTY', date(2025, 6, 12), 22500, 'CE')
        tokens = resolver.tokens_for_session('NIFTY', date(2025, 6, 12), [22400, 22450, 22500], ['CE','PE'])
    """

    def __init__(self, instruments_df: Optional[pd.DataFrame] = None):
        self._df: Optional[pd.DataFrame] = None
        if instruments_df is not None:
            self._df = self._filter_options(instruments_df)

    def _load(self) -> pd.DataFrame:
        if self._df is not None:
            return self._df
        from src.integrations.zerodha.instruments import load_instruments
        df = load_instruments()
        if df.empty:
            raise RuntimeError(
                "Instruments CSV not found. Run: "
                "python -m src.cli.main zerodha instruments refresh"
            )
        self._df = self._filter_options(df)
        return self._df

    @staticmethod
    def _filter_options(df: pd.DataFrame) -> pd.DataFrame:
        """Keep only NFO option rows and normalise key columns."""
        mask = (
            (df['exchange'].str.upper() == 'NFO')
            & (df['instrument_type'].str.upper().isin(['CE', 'PE']))
        )
        opt = df[mask].copy()
        if opt.empty:
            return opt
        opt['strike']      = opt['strike'].astype(float)
        opt['expiry']      = pd.to_datetime(opt['expiry']).dt.date
        opt['name']        = opt['name'].str.upper()
        opt['instrument_type'] = opt['instrument_type'].str.upper()
        return opt

    def resolve(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
    ) -> Optional[int]:
        """
        Return instrument_token for the given contract, or None if not found.
        Does not raise — callers should handle None as "skip this strike".
        """
        try:
            df = self._load()
        except RuntimeError as e:
            logger.warning(str(e))
            return None

        mask = (
            (df['name'] == underlying.upper())
            & (df['expiry'] == expiry)
            & (df['strike'] == float(strike))
            & (df['instrument_type'] == option_type.upper())
        )
        matches = df[mask]
        if matches.empty:
            return None
        return int(matches.iloc[0]['instrument_token'])

    def tokens_for_session(
        self,
        underlying: str,
        expiry: date,
        strikes: List[float],
        option_types: Optional[List[str]] = None,
    ) -> Dict[int, dict]:
        """
        Bulk resolve tokens for multiple strikes/types.
        Returns {token: {'underlying': ..., 'expiry': ..., 'strike': ..., 'option_type': ...}}.
        Skips strikes not found in the instrument master.
        """
        if option_types is None:
            option_types = ['CE', 'PE']

        result: Dict[int, dict] = {}
        for strike in strikes:
            for opt_type in option_types:
                token = self.resolve(underlying, expiry, strike, opt_type)
                if token is not None:
                    result[token] = {
                        'underlying':  underlying,
                        'expiry':      expiry,
                        'strike':      strike,
                        'option_type': opt_type,
                    }
        logger.info(
            f"Resolved {len(result)}/{len(strikes)*len(option_types)} tokens "
            f"for {underlying} expiry={expiry}"
        )
        return result

    def nearest_weekly_expiry(self, session_date: date, underlying: str = 'NIFTY') -> Optional[date]:
        """
        Return the nearest weekly expiry on or after session_date from the
        instrument master. Falls back to Thursday-based calculation if data
        is unavailable.
        """
        try:
            df = self._load()
        except RuntimeError:
            return _thursday_expiry(session_date)

        expiries = df[df['name'] == underlying.upper()]['expiry'].dropna().unique()
        future = sorted(e for e in expiries if e >= session_date)
        return future[0] if future else _thursday_expiry(session_date)

    def clear_cache(self) -> None:
        """Force reload on next access (call when instruments CSV is refreshed)."""
        self._df = None


def _thursday_expiry(from_date: date) -> date:
    """Fallback: next Thursday at or after from_date."""
    from datetime import timedelta
    days = (3 - from_date.weekday()) % 7
    return from_date + timedelta(days=days)
