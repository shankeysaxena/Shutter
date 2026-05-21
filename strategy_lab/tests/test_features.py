"""Tests for VWAP, Opening Range, and Gap feature calculations."""
import pandas as pd
import numpy as np
import pytest
from datetime import datetime, time, date

from src.features.vwap import VWAPFeature
from src.features.opening_range import OpeningRangeFeature
from src.features.gap import GapFeature


def _make_session(timestamps, opens, highs, lows, closes, volumes=None):
    if volumes is None:
        volumes = [1000] * len(timestamps)
    return pd.DataFrame({
        'timestamp': pd.to_datetime(timestamps),
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes,
    })


# --- VWAP ---

class TestVWAP:
    def test_vwap_single_bar(self):
        df = _make_session(
            ['2024-01-01 09:15'], [100], [110], [90], [105], [1000]
        )
        result = VWAPFeature().calculate(df)
        # typical = (110+90+105)/3 = 101.666...
        expected_vwap = (110 + 90 + 105) / 3
        assert abs(result['vwap'].iloc[0] - expected_vwap) < 0.01

    def test_vwap_cumulative_resets(self):
        # Two bars with different volumes — vwap should be volume-weighted
        df = _make_session(
            ['2024-01-01 09:15', '2024-01-01 09:16'],
            [100, 110], [105, 115], [95, 105], [100, 110],
            [1000, 2000]
        )
        result = VWAPFeature().calculate(df)
        tp1 = (105 + 95 + 100) / 3   # 100
        tp2 = (115 + 105 + 110) / 3   # 110
        expected_vwap2 = (tp1 * 1000 + tp2 * 2000) / 3000
        assert abs(result['vwap'].iloc[1] - expected_vwap2) < 0.01

    def test_vwap_above_below_flags(self):
        df = _make_session(
            ['2024-01-01 09:15', '2024-01-01 09:16'],
            [100, 100], [110, 100], [90, 100], [120, 80],
            [1000, 1000]
        )
        result = VWAPFeature().calculate(df)
        assert result['above_vwap'].iloc[0]   # close 120 > vwap
        assert result['below_vwap'].iloc[1]   # close 80 < vwap

    def test_vwap_zero_volume_uses_time_weighted_fallback(self):
        # Zero-volume instruments (indices from Kite) fall back to time-weighted VWAP.
        # typical_price = (110 + 90 + 105) / 3 = 101.67
        df = _make_session(
            ['2024-01-01 09:15'], [100], [110], [90], [105], [0]
        )
        result = VWAPFeature().calculate(df)
        assert result['vwap'].iloc[0] == pytest.approx((110 + 90 + 105) / 3, rel=1e-4)

    def test_vwap_empty_df(self):
        df = pd.DataFrame()
        result = VWAPFeature().calculate(df)
        assert result.empty


# --- Opening Range ---

class TestOpeningRange:
    def _session_with_or(self):
        timestamps = [
            '2024-01-01 09:15', '2024-01-01 09:20', '2024-01-01 09:25',
            '2024-01-01 09:30', '2024-01-01 09:35',
        ]
        return _make_session(
            timestamps,
            [100, 102, 98, 104, 106],
            [105, 106, 103, 108, 110],
            [98,  100,  96, 102, 104],
            [103, 104, 100, 106, 108],
        )

    def test_or_ready_only_after_end_time(self):
        df = self._session_with_or()
        result = OpeningRangeFeature(time(9, 15), time(9, 30)).calculate(df)
        # Bars at 09:15, 09:20, 09:25 are inside OR window — not ready
        assert not result.loc[result['timestamp'].dt.time < time(9, 30), 'or_ready'].any()
        # Bar at 09:30 and after should have or_ready = True
        assert result.loc[result['timestamp'].dt.time >= time(9, 30), 'or_ready'].all()

    def test_or_high_is_max_of_or_window(self):
        df = self._session_with_or()
        result = OpeningRangeFeature(time(9, 15), time(9, 30)).calculate(df)
        expected_or_high = max(105, 106, 103)  # highs of 09:15, 09:20, 09:25
        after_or = result[result['timestamp'].dt.time >= time(9, 30)]
        assert abs(after_or['or_high'].iloc[0] - expected_or_high) < 0.001

    def test_or_low_is_min_of_or_window(self):
        df = self._session_with_or()
        result = OpeningRangeFeature(time(9, 15), time(9, 30)).calculate(df)
        expected_or_low = min(98, 100, 96)  # lows of 09:15, 09:20, 09:25
        after_or = result[result['timestamp'].dt.time >= time(9, 30)]
        assert abs(after_or['or_low'].iloc[0] - expected_or_low) < 0.001

    def test_or_values_nan_inside_window(self):
        df = self._session_with_or()
        result = OpeningRangeFeature(time(9, 15), time(9, 30)).calculate(df)
        inside = result[result['timestamp'].dt.time < time(9, 30)]
        assert inside['or_high'].isna().all()
        assert inside['or_low'].isna().all()

    def test_or_empty_df(self):
        result = OpeningRangeFeature().calculate(pd.DataFrame())
        assert result.empty


# --- Gap ---

class TestGap:
    def test_gap_up(self):
        df = _make_session(['2024-01-01 09:15'], [110], [115], [108], [112])
        df['prior_close'] = 100.0
        result = GapFeature().calculate(df)
        assert abs(result['gap_pct'].iloc[0] - 0.10) < 0.001
        assert result['gap_direction'].iloc[0] == 'UP'

    def test_gap_down(self):
        df = _make_session(['2024-01-01 09:15'], [90], [95], [88], [92])
        df['prior_close'] = 100.0
        result = GapFeature().calculate(df)
        assert abs(result['gap_pct'].iloc[0] - (-0.10)) < 0.001
        assert result['gap_direction'].iloc[0] == 'DOWN'

    def test_gap_flat(self):
        df = _make_session(['2024-01-01 09:15'], [100], [105], [98], [102])
        df['prior_close'] = 100.0
        result = GapFeature().calculate(df)
        assert result['gap_direction'].iloc[0] == 'FLAT'

    def test_gap_no_prior_close(self):
        df = _make_session(['2024-01-01 09:15'], [100], [105], [98], [102])
        df['prior_close'] = None
        result = GapFeature().calculate(df)
        assert pd.isna(result['gap_pct'].iloc[0])

    def test_gap_missing_prior_close_column(self):
        df = _make_session(['2024-01-01 09:15'], [100], [105], [98], [102])
        result = GapFeature().calculate(df)
        assert result['gap_pct'].isna().all()
