"""Tests for instrument resolution (no real API calls)."""
import pandas as pd
import pytest

pytest.importorskip("kiteconnect")

from src.integrations.zerodha.config import KiteConfig
from src.integrations.zerodha.instruments import resolve_instrument_token


def _make_instruments_df():
    return pd.DataFrame([
        {'instrument_token': 12345, 'tradingsymbol': 'RELIANCE', 'exchange': 'NSE',
          'segment': 'NSE', 'name': 'RELIANCE INDUSTRIES'},
        {'instrument_token': 67890, 'tradingsymbol': 'NIFTY24JANFUT', 'exchange': 'NFO',
          'segment': 'NFO-FUT', 'name': 'NIFTY FUT'},
        {'instrument_token': 99999, 'tradingsymbol': 'BANKNIFTY', 'exchange': 'NSE',
          'segment': 'NSE', 'name': 'NIFTY BANK'},
    ])


class TestFallbackTokens:
    def test_nifty_index_returns_known_token(self):
        assert resolve_instrument_token('NIFTY', 'NSE') == 256265

    def test_nifty50_alias_works(self):
        assert resolve_instrument_token('NIFTY 50', 'NSE') == 256265

    def test_banknifty_alias_works(self):
        assert resolve_instrument_token('BANKNIFTY', 'NSE') == 260105

    def test_nifty_bank_alias_works(self):
        assert resolve_instrument_token('NIFTY BANK', 'NSE') == 260105


class TestCsvLookup:
    def test_resolves_from_instruments_df(self):
        df = _make_instruments_df()
        token = resolve_instrument_token('RELIANCE', 'NSE', instruments_df=df)
        assert token == 12345

    def test_raises_lookup_error_for_unknown_symbol(self):
        df = _make_instruments_df()
        with pytest.raises(LookupError, match='No instrument found'):
            resolve_instrument_token('DOESNOTEXIST', 'NSE', instruments_df=df)

    def test_empty_df_raises_lookup_error(self):
        with pytest.raises(LookupError, match='Instruments CSV'):
            resolve_instrument_token('RELIANCE', 'NSE', instruments_df=pd.DataFrame())

    def test_segment_filtering_nfo_fut(self):
        df = _make_instruments_df()
        token = resolve_instrument_token('NIFTY24JANFUT', 'NFO-FUT', instruments_df=df)
        assert token == 67890
