"""Tests for SyntheticOptionChainFeed and WeeklyExpiryProvider."""
from datetime import datetime, date

import pytest

from src.feeds.option_chain_snapshot import (
    SyntheticOptionChainFeed,
    WeeklyExpiryProvider,
    _bs_price,
    _norm_cdf,
)


class TestWeeklyExpiryProvider:
    def test_picks_same_day_if_today_is_target_weekday(self):
        # 2024-01-04 is Thursday (weekday=3)
        provider = WeeklyExpiryProvider(weekday=3)
        assert provider(date(2024, 1, 4), 'NIFTY') == date(2024, 1, 4)

    def test_picks_upcoming_target_weekday(self):
        provider = WeeklyExpiryProvider(weekday=3)
        # Monday 2024-01-01 -> Thursday 2024-01-04
        assert provider(date(2024, 1, 1), 'NIFTY') == date(2024, 1, 4)

    def test_wraps_to_next_week(self):
        provider = WeeklyExpiryProvider(weekday=3)
        # Friday 2024-01-05 -> next Thursday 2024-01-11
        assert provider(date(2024, 1, 5), 'NIFTY') == date(2024, 1, 11)


class TestNormCdf:
    def test_known_values(self):
        assert abs(_norm_cdf(0.0) - 0.5) < 1e-9
        assert abs(_norm_cdf(1.96) - 0.975) < 1e-3
        assert abs(_norm_cdf(-1.96) - 0.025) < 1e-3


class TestBSPrice:
    def test_atm_call_positive(self):
        # ATM, 30 days, 20% vol -> call should be > 0
        price = _bs_price(100, 100, 30 / 365, 0.20, 0.05, 'CE')
        assert price > 0

    def test_deep_otm_call_nearly_zero(self):
        # Deep OTM, short time
        price = _bs_price(100, 200, 7 / 365, 0.15, 0.05, 'CE')
        assert price < 0.01

    def test_call_put_parity_approx(self):
        # C - P = S - K*e^-rT
        import math
        S, K, T, sigma, r = 100, 100, 30 / 365, 0.20, 0.05
        c = _bs_price(S, K, T, sigma, r, 'CE')
        p = _bs_price(S, K, T, sigma, r, 'PE')
        expected = S - K * math.exp(-r * T)
        assert abs((c - p) - expected) < 0.01

    def test_at_expiry_intrinsic(self):
        # T=0: intrinsic only
        assert _bs_price(110, 100, 0, 0.20, 0.05, 'CE') == 10
        assert _bs_price(100, 110, 0, 0.20, 0.05, 'CE') == 0
        assert _bs_price(90, 100, 0, 0.20, 0.05, 'PE') == 10


class TestSyntheticOptionChainFeed:
    def test_basic_snapshot_shape(self):
        feed = SyntheticOptionChainFeed(atm_iv=0.15, num_strikes_each_side=10)
        ts = datetime(2024, 1, 2, 10, 0)
        snap = feed.snapshot_at(ts, 'NIFTY', 22000)
        assert snap is not None
        assert snap.underlying == 'NIFTY'
        assert snap.spot == 22000
        # 21 strikes × 2 option_types = 42 quotes
        assert len(snap.quotes) == 42

    def test_atm_strike_present(self):
        feed = SyntheticOptionChainFeed(atm_iv=0.15)
        snap = feed.snapshot_at(datetime(2024, 1, 2, 10, 0), 'NIFTY', 22000)
        assert snap.quote(22000, 'CE') is not None
        assert snap.quote(22000, 'PE') is not None

    def test_call_monotonicity_in_strike(self):
        """Call price strictly decreases as strike increases."""
        feed = SyntheticOptionChainFeed(atm_iv=0.15)
        snap = feed.snapshot_at(datetime(2024, 1, 2, 10, 0), 'NIFTY', 22000)
        strikes = sorted({q.strike for q in snap.quotes})
        last_price = float('inf')
        for k in strikes:
            q = snap.quote(k, 'CE')
            assert q.last <= last_price
            last_price = q.last

    def test_put_monotonicity_in_strike(self):
        """Put price strictly increases as strike increases."""
        feed = SyntheticOptionChainFeed(atm_iv=0.15)
        snap = feed.snapshot_at(datetime(2024, 1, 2, 10, 0), 'NIFTY', 22000)
        strikes = sorted({q.strike for q in snap.quotes})
        last_price = -float('inf')
        for k in strikes:
            q = snap.quote(k, 'PE')
            assert q.last >= last_price
            last_price = q.last

    def test_bid_ask_ordered(self):
        feed = SyntheticOptionChainFeed(atm_iv=0.15)
        snap = feed.snapshot_at(datetime(2024, 1, 2, 10, 0), 'NIFTY', 22000)
        for q in snap.quotes:
            assert q.bid <= q.ask
            assert q.ask > 0

    def test_unknown_underlying_returns_none(self):
        feed = SyntheticOptionChainFeed()
        snap = feed.snapshot_at(datetime(2024, 1, 2, 10, 0), 'GOLD', 60000)
        assert snap is None

    def test_zero_spot_returns_none(self):
        feed = SyntheticOptionChainFeed()
        snap = feed.snapshot_at(datetime(2024, 1, 2, 10, 0), 'NIFTY', 0)
        assert snap is None

    def test_skew_makes_otm_puts_more_expensive_than_otm_calls(self):
        """With negative skew, equidistant OTM put IV > equidistant OTM call IV."""
        feed = SyntheticOptionChainFeed(atm_iv=0.15, skew=-0.05, smile=0.0)
        snap = feed.snapshot_at(datetime(2024, 1, 2, 10, 0), 'NIFTY', 22000)
        # 22500 CE is 500 OTM; 21500 PE is 500 OTM
        otm_call_iv = snap.quote(22500, 'CE').iv
        otm_put_iv = snap.quote(21500, 'PE').iv
        assert otm_put_iv > otm_call_iv

    def test_decay_over_time(self):
        """ATM options decay as time passes (same spot, later in day)."""
        feed = SyntheticOptionChainFeed(atm_iv=0.15)
        snap_early = feed.snapshot_at(datetime(2024, 1, 2, 10, 0), 'NIFTY', 22000)
        snap_late = feed.snapshot_at(datetime(2024, 1, 4, 10, 0), 'NIFTY', 22000)  # closer to Thu expiry
        # 0 DTE close to expiry should be cheaper than 2 DTE
        assert snap_late.quote(22000, 'CE').last < snap_early.quote(22000, 'CE').last
