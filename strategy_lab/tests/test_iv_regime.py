"""Tests for IVRegimeFeature rolling buffer and percentile lookup."""
from datetime import datetime, timedelta

from src.features.iv_regime import IVRegimeFeature


class TestIVRegimeFeature:
    def test_buffer_grows_with_updates(self):
        f = IVRegimeFeature(min_observations=1)
        ts = datetime(2024, 1, 1)
        f.update('NIFTY', ts, 0.15)
        f.update('NIFTY', ts + timedelta(minutes=1), 0.16)
        assert f.buffer_size('NIFTY') == 2

    def test_percentile_below_threshold_returns_none(self):
        f = IVRegimeFeature(min_observations=100)
        f.update('NIFTY', datetime(2024, 1, 1), 0.15)
        assert f.percentile('NIFTY', 0.15) is None

    def test_percentile_correctness(self):
        f = IVRegimeFeature(min_observations=10)
        ts = datetime(2024, 1, 1)
        for i in range(20):
            f.update('NIFTY', ts + timedelta(minutes=i), 0.10 + 0.001 * i)
        # value at the low end -> 0
        assert f.percentile('NIFTY', 0.10) == 0.0
        # value at midpoint
        assert 0.45 <= f.percentile('NIFTY', 0.110) <= 0.55
        # value above all
        assert f.percentile('NIFTY', 0.130) == 1.0

    def test_trims_outside_window(self):
        f = IVRegimeFeature(lookback_days=10, min_observations=1)
        ts = datetime(2024, 1, 1)
        f.update('NIFTY', ts, 0.15)
        f.update('NIFTY', ts + timedelta(days=20), 0.18)
        assert f.buffer_size('NIFTY') == 1

    def test_reset_clears_all(self):
        f = IVRegimeFeature(min_observations=1)
        f.update('NIFTY', datetime(2024, 1, 1), 0.15)
        f.reset()
        assert f.buffer_size('NIFTY') == 0

    def test_invalid_iv_ignored(self):
        f = IVRegimeFeature(min_observations=1)
        f.update('NIFTY', datetime(2024, 1, 1), 0)
        f.update('NIFTY', datetime(2024, 1, 1), -0.1)
        f.update('NIFTY', datetime(2024, 1, 1), None)
        assert f.buffer_size('NIFTY') == 0

    def test_per_instrument_isolation(self):
        f = IVRegimeFeature(min_observations=1)
        f.update('NIFTY', datetime(2024, 1, 1), 0.15)
        f.update('BANKNIFTY', datetime(2024, 1, 1), 0.20)
        assert f.buffer_size('NIFTY') == 1
        assert f.buffer_size('BANKNIFTY') == 1
