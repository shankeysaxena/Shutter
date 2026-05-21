"""Tests for historical candle fetching, normalisation, chunking (all mocked)."""
import warnings
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pandas as pd
import pytest

pytest.importorskip("kiteconnect")

from src.integrations.zerodha.historical_loader import (
    _date_chunks,
    _normalise,
    _validate,
    fetch_candles,
    save_candles,
)


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def _make_raw_candle(dt: datetime, o=100.0, h=105.0, lo=98.0, c=102.0, vol=1000):
    """Kite-style raw candle dict with naive datetime."""
    return {'date': dt, 'open': o, 'high': h, 'low': lo, 'close': c, 'volume': vol}


def _candle_range(start: datetime, n: int):
    """n consecutive 1-min candles starting at start."""
    from datetime import timedelta
    return [_make_raw_candle(start + timedelta(minutes=i)) for i in range(n)]


# -----------------------------------------------------------------------
# Chunking
# -----------------------------------------------------------------------

class TestDateChunks:
    def test_single_chunk_fits_within_limit(self):
        chunks = _date_chunks(date(2024, 1, 1), date(2024, 1, 30), 60)
        assert len(chunks) == 1
        assert chunks[0] == (date(2024, 1, 1), date(2024, 1, 30))

    def test_full_year_splits_into_multiple_chunks(self):
        chunks = _date_chunks(date(2024, 1, 1), date(2024, 12, 31), 60)
        assert len(chunks) == 7          # ceil(366/60)
        # no gaps or overlaps
        for i in range(1, len(chunks)):
            from datetime import timedelta
            assert chunks[i][0] == chunks[i-1][1] + timedelta(days=1)

    def test_single_day_range(self):
        chunks = _date_chunks(date(2024, 1, 15), date(2024, 1, 15), 60)
        assert chunks == [(date(2024, 1, 15), date(2024, 1, 15))]


# -----------------------------------------------------------------------
# Normalisation
# -----------------------------------------------------------------------

class TestNormalise:
    def test_columns_present(self):
        raw = [_make_raw_candle(datetime(2024, 1, 2, 9, 15))]
        df = _normalise(raw, 'NIFTY')
        for col in ['timestamp', 'instrument', 'open', 'high', 'low', 'close', 'volume']:
            assert col in df.columns

    def test_instrument_column_set(self):
        raw = [_make_raw_candle(datetime(2024, 1, 2, 9, 15))]
        df = _normalise(raw, 'BANKNIFTY')
        assert df['instrument'].iloc[0] == 'BANKNIFTY'

    def test_tz_aware_timestamps_stripped(self):
        from datetime import timezone, timedelta
        # IST is UTC+5:30
        ist = timezone(timedelta(hours=5, minutes=30))
        ts_aware = datetime(2024, 1, 2, 9, 15, tzinfo=ist)
        raw = [_make_raw_candle(ts_aware)]
        df = _normalise(raw, 'NIFTY')
        ts = df['timestamp'].iloc[0]
        assert ts.tzinfo is None     # naive after strip

    def test_naive_timestamps_unchanged(self):
        raw = [_make_raw_candle(datetime(2024, 1, 2, 9, 15))]
        df = _normalise(raw, 'NIFTY')
        assert df['timestamp'].iloc[0] == datetime(2024, 1, 2, 9, 15)

    def test_numeric_types(self):
        raw = [_make_raw_candle(datetime(2024, 1, 2, 9, 15), vol=12345)]
        df = _normalise(raw, 'NIFTY')
        assert df['close'].dtype.kind == 'f'
        assert df['volume'].dtype.kind in ('i', 'u')


# -----------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------

class TestValidate:
    def _df(self, rows):
        ts = [datetime(2024, 1, 2, 9, 15 + i) for i in range(len(rows))]
        return pd.DataFrame({
            'timestamp': ts, 'instrument': 'NIFTY',
            'open': [r[0] for r in rows], 'high': [r[1] for r in rows],
            'low': [r[2] for r in rows], 'close': [r[3] for r in rows],
            'volume': [1000] * len(rows),
        })

    def test_passes_on_clean_data(self):
        df = self._df([(100, 110, 90, 105)] * 5)
        result = _validate(df, 'NIFTY')
        assert len(result) == 5

    def test_deduplicates_timestamps(self):
        df = self._df([(100, 110, 90, 105)] * 3)
        df.loc[2, 'timestamp'] = df.loc[1, 'timestamp']   # duplicate
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            result = _validate(df, 'NIFTY')
        assert len(result) == 2
        assert any('duplicate' in str(x.message).lower() for x in w)

    def test_raises_on_zero_rows_after_dedup(self):
        df = self._df([(100, 110, 90, 105)] * 2)
        df.loc[1, 'timestamp'] = df.loc[0, 'timestamp']
        with warnings.catch_warnings(record=True):
            warnings.simplefilter('always')
            # Both rows same timestamp → 1 row remains → should NOT raise
            result = _validate(df, 'NIFTY')
            assert len(result) == 1

    def test_warns_on_out_of_hours_rows(self):
        df = self._df([(100, 110, 90, 105)] * 2)
        df.loc[0, 'timestamp'] = datetime(2024, 1, 2, 7, 0)   # pre-market
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            _validate(df, 'NIFTY')
        assert any('outside market hours' in str(x.message) for x in w)


# -----------------------------------------------------------------------
# fetch_candles with mocked Kite client
# -----------------------------------------------------------------------

class TestFetchCandles:
    def _mock_kite(self, rows_per_chunk=5):
        kite = MagicMock()
        kite.historical_data.return_value = _candle_range(
            datetime(2024, 1, 2, 9, 15), rows_per_chunk
        )
        return kite

    def test_calls_historical_data_once_for_short_range(self):
        kite = self._mock_kite(10)
        df = fetch_candles(kite, 256265, 'NIFTY', date(2024, 1, 2), date(2024, 1, 3))
        kite.historical_data.assert_called_once()
        assert len(df) == 10

    def test_chunked_for_long_range(self):
        kite = self._mock_kite(5)
        # 6-month span → at least 3 chunks
        with patch('src.integrations.zerodha.historical_loader.time') as t:
            t.sleep = MagicMock()
            df = fetch_candles(kite, 256265, 'NIFTY', date(2024, 1, 1), date(2024, 6, 30))
        assert kite.historical_data.call_count >= 3
        t.sleep.assert_called()

    def test_raises_on_zero_rows(self):
        kite = MagicMock()
        kite.historical_data.return_value = []
        with pytest.raises(ValueError, match='Zero rows'):
            fetch_candles(kite, 256265, 'NIFTY', date(2024, 1, 2), date(2024, 1, 2))

    def test_deduplicates_across_chunks(self):
        # Two chunks return overlapping last/first row
        first_chunk = _candle_range(datetime(2024, 1, 2, 9, 15), 3)
        second_chunk = _candle_range(datetime(2024, 1, 2, 9, 17), 3)  # starts with overlap
        kite = MagicMock()
        kite.historical_data.side_effect = [first_chunk, second_chunk]
        with patch('src.integrations.zerodha.historical_loader._date_chunks') as mc:
            mc.return_value = [(date(2024, 1, 2), date(2024, 1, 30)),
                                (date(2024, 1, 31), date(2024, 3, 31))]
            with patch('src.integrations.zerodha.historical_loader.time'):
                df = fetch_candles(kite, 256265, 'NIFTY', date(2024, 1, 2), date(2024, 3, 31))
        # 3 + 3 = 6 raw rows but 2 are duplicate (09:17 and 09:18 appear in both)
        assert df['timestamp'].is_unique


# -----------------------------------------------------------------------
# save_candles
# -----------------------------------------------------------------------

class TestSaveCandles:
    def _df(self):
        return pd.DataFrame({
            'timestamp': [datetime(2024, 1, 2, 9, 15), datetime(2024, 1, 2, 9, 16)],
            'instrument': ['NIFTY', 'NIFTY'],
            'open': [100.0, 101.0], 'high': [105.0, 106.0],
            'low': [99.0, 100.0], 'close': [102.0, 103.0],
            'volume': [1000, 1100],
        })

    def test_creates_csv(self, tmp_path):
        df = self._df()
        dest = save_candles(df, 'NIFTY', out_dir=tmp_path)
        assert dest.exists()
        loaded = pd.read_csv(dest, parse_dates=['timestamp'])
        assert len(loaded) == 2

    def test_appends_and_deduplicates(self, tmp_path):
        df = self._df()
        save_candles(df, 'NIFTY', out_dir=tmp_path)
        # Append same data again — should still be 2 rows
        save_candles(df, 'NIFTY', out_dir=tmp_path)
        loaded = pd.read_csv(tmp_path / 'NIFTY.csv', parse_dates=['timestamp'])
        assert len(loaded) == 2

    def test_output_sorted_by_timestamp(self, tmp_path):
        df = self._df().iloc[::-1].reset_index(drop=True)   # reverse order
        save_candles(df, 'NIFTY', out_dir=tmp_path)
        loaded = pd.read_csv(tmp_path / 'NIFTY.csv', parse_dates=['timestamp'])
        assert loaded['timestamp'].is_monotonic_increasing
