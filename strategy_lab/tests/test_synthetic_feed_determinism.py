"""v2 §16 reproducibility: synthetic chain feed must be deterministic.

Same inputs (timestamp, underlying, spot) must produce the exact same
ChainSnapshot. Without this, sweeps and replay-parity tests are meaningless.
"""
from datetime import datetime

from src.feeds.option_chain_snapshot import SyntheticOptionChainFeed


def _feed():
    # Use the same defaults every test uses
    return SyntheticOptionChainFeed(
        atm_iv=0.15,
        skew=-0.02,
        smile=0.30,
        risk_free_rate=0.07,
        num_strikes_each_side=20,
        spread_pct=0.01,
        min_spread=0.5,
    )


class TestSyntheticFeedDeterminism:
    def test_same_inputs_produce_same_snapshot(self):
        feed_a = _feed()
        feed_b = _feed()
        ts = datetime(2024, 1, 2, 10, 0)
        snap_a = feed_a.snapshot_at(ts, 'NIFTY', 22000.0)
        snap_b = feed_b.snapshot_at(ts, 'NIFTY', 22000.0)
        assert snap_a.spot == snap_b.spot
        assert snap_a.expiry == snap_b.expiry
        assert snap_a.atm_iv == snap_b.atm_iv
        assert len(snap_a.quotes) == len(snap_b.quotes)
        for qa, qb in zip(snap_a.quotes, snap_b.quotes):
            assert qa.strike == qb.strike
            assert qa.option_type == qb.option_type
            assert qa.bid == qb.bid
            assert qa.ask == qb.ask
            assert qa.iv == qb.iv

    def test_consecutive_calls_on_same_feed_are_identical(self):
        feed = _feed()
        ts = datetime(2024, 1, 2, 10, 0)
        snap_1 = feed.snapshot_at(ts, 'NIFTY', 22000.0)
        snap_2 = feed.snapshot_at(ts, 'NIFTY', 22000.0)
        assert [(q.strike, q.option_type, q.bid, q.ask) for q in snap_1.quotes] == \
               [(q.strike, q.option_type, q.bid, q.ask) for q in snap_2.quotes]

    def test_different_spot_produces_different_atm(self):
        feed = _feed()
        ts = datetime(2024, 1, 2, 10, 0)
        snap_low = feed.snapshot_at(ts, 'NIFTY', 21500.0)
        snap_high = feed.snapshot_at(ts, 'NIFTY', 22500.0)
        # ATM should follow spot (rounded to nearest 50)
        atm_low = round(21500 / 50) * 50
        atm_high = round(22500 / 50) * 50
        assert snap_low.quote(atm_low, 'CE') is not None
        assert snap_high.quote(atm_high, 'CE') is not None

    def test_no_hidden_state_between_calls(self):
        """Calling for one underlying must not corrupt a later call for another."""
        feed = _feed()
        ts = datetime(2024, 1, 2, 10, 0)
        snap_nifty = feed.snapshot_at(ts, 'NIFTY', 22000.0)
        snap_bn = feed.snapshot_at(ts, 'BANKNIFTY', 48000.0)
        snap_nifty_again = feed.snapshot_at(ts, 'NIFTY', 22000.0)
        for q1, q2 in zip(snap_nifty.quotes, snap_nifty_again.quotes):
            assert q1.last == q2.last
            assert q1.bid == q2.bid
            assert q1.ask == q2.ask
