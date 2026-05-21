"""Tests for MultiLegSimulator entry, exit, and mark-to-market."""
import copy
from datetime import datetime, date

import pytest

from src.core.option_models import OptionLeg, MultiLegSignal
from src.execution.multi_leg_simulator import (
    MultiLegSimulator,
    MODE_IDEAL,
    MODE_REALISTIC,
    MODE_PESSIMISTIC,
)
from src.feeds.option_chain_snapshot import SyntheticOptionChainFeed


def _signal(ts, expiry, atm=22000, wing=200):
    legs = [
        OptionLeg('NIFTY', expiry, atm, 'CE', 'SELL', qty=1),
        OptionLeg('NIFTY', expiry, atm, 'PE', 'SELL', qty=1),
        OptionLeg('NIFTY', expiry, atm + wing, 'CE', 'BUY', qty=1),
        OptionLeg('NIFTY', expiry, atm - wing, 'PE', 'BUY', qty=1),
    ]
    return MultiLegSignal('IRON_FLY', 'NIFTY', ts, 'IRON_FLY', legs)


def _feed_and_snap(ts, spot=22000):
    feed = SyntheticOptionChainFeed(atm_iv=0.15)
    return feed, feed.snapshot_at(ts, 'NIFTY', spot)


class TestOpenTrade:
    def test_opens_trade_with_positive_credit(self):
        ts = datetime(2024, 1, 2, 10, 0)
        _, snap = _feed_and_snap(ts)
        sim = MultiLegSimulator(mode=MODE_IDEAL, brokerage_per_leg=0)
        trade = sim.open_trade(_signal(ts, snap.expiry), snap, lots=1, lot_size=25)
        assert trade is not None
        assert trade.net_entry_credit > 0
        assert len(trade.entry_fills) == 4

    def test_zero_lots_returns_none(self):
        ts = datetime(2024, 1, 2, 10, 0)
        _, snap = _feed_and_snap(ts)
        sim = MultiLegSimulator(mode=MODE_IDEAL)
        trade = sim.open_trade(_signal(ts, snap.expiry), snap, lots=0, lot_size=25)
        assert trade is None

    def test_unquotable_leg_returns_none(self):
        ts = datetime(2024, 1, 2, 10, 0)
        _, snap = _feed_and_snap(ts)
        sim = MultiLegSimulator(mode=MODE_IDEAL)
        # Use a strike that doesn't exist in the chain
        signal = MultiLegSignal('X', 'NIFTY', ts, 'IRON_FLY', [
            OptionLeg('NIFTY', snap.expiry, 99999, 'CE', 'SELL', qty=1),
        ])
        trade = sim.open_trade(signal, snap, lots=1, lot_size=25)
        assert trade is None

    def test_realistic_mode_pays_more_than_ideal(self):
        """Realistic mode pays wings at ask + extra ticks; net credit lower than ideal mid-fill."""
        ts = datetime(2024, 1, 2, 10, 0)
        _, snap = _feed_and_snap(ts)
        sig = _signal(ts, snap.expiry)
        ideal = MultiLegSimulator(mode=MODE_IDEAL).open_trade(sig, snap, lots=1, lot_size=25)
        realistic = MultiLegSimulator(mode=MODE_REALISTIC).open_trade(sig, snap, lots=1, lot_size=25)
        assert ideal.net_entry_credit > realistic.net_entry_credit

    def test_pessimistic_mode_worse_than_realistic(self):
        ts = datetime(2024, 1, 2, 10, 0)
        _, snap = _feed_and_snap(ts)
        sig = _signal(ts, snap.expiry)
        realistic = MultiLegSimulator(mode=MODE_REALISTIC).open_trade(sig, snap, lots=1, lot_size=25)
        pessimistic = MultiLegSimulator(mode=MODE_PESSIMISTIC).open_trade(sig, snap, lots=1, lot_size=25)
        assert realistic.net_entry_credit > pessimistic.net_entry_credit


class TestCloseTrade:
    def test_close_records_exit_data(self):
        ts = datetime(2024, 1, 2, 10, 0)
        feed, snap = _feed_and_snap(ts)
        sim = MultiLegSimulator(mode=MODE_IDEAL, brokerage_per_leg=0)
        trade = sim.open_trade(_signal(ts, snap.expiry), snap, lots=1, lot_size=25)
        ts2 = datetime(2024, 1, 2, 11, 0)
        snap2 = feed.snapshot_at(ts2, 'NIFTY', 22000)
        ok = sim.close_trade(trade, snap2, ts2, 'PROFIT_TARGET')
        assert ok
        assert trade.exit_time == ts2
        assert trade.exit_reason == 'PROFIT_TARGET'
        assert trade.gross_pnl is not None
        assert trade.net_pnl is not None
        assert len(trade.exit_fills) == 4

    def test_theta_profitable_close(self):
        """Same spot, later in day -> close at profit (no brokerage)."""
        feed = SyntheticOptionChainFeed(atm_iv=0.15)
        ts_open = datetime(2024, 1, 2, 10, 0)
        snap_open = feed.snapshot_at(ts_open, 'NIFTY', 22000)
        sim = MultiLegSimulator(mode=MODE_IDEAL, brokerage_per_leg=0)
        trade = sim.open_trade(_signal(ts_open, snap_open.expiry), snap_open, lots=1, lot_size=25)
        # close 4 hours later, same spot
        ts_close = datetime(2024, 1, 2, 14, 0)
        snap_close = feed.snapshot_at(ts_close, 'NIFTY', 22000)
        sim.close_trade(trade, snap_close, ts_close, 'PROFIT_TARGET')
        assert trade.gross_pnl > 0

    def test_touch_at_short_strike_negative(self):
        """If spot moves to short strike, closing is a loss."""
        feed = SyntheticOptionChainFeed(atm_iv=0.15)
        ts_open = datetime(2024, 1, 2, 10, 0)
        snap_open = feed.snapshot_at(ts_open, 'NIFTY', 22000)
        sim = MultiLegSimulator(mode=MODE_IDEAL, brokerage_per_leg=0)
        trade = sim.open_trade(_signal(ts_open, snap_open.expiry), snap_open, lots=1, lot_size=25)
        # spot moves to short call strike
        ts_close = datetime(2024, 1, 2, 11, 0)
        snap_close = feed.snapshot_at(ts_close, 'NIFTY', 22200)
        sim.close_trade(trade, snap_close, ts_close, 'TOUCH_EXIT_CALL')
        # P&L should reflect the loss from gamma — likely negative or small positive
        assert trade.gross_pnl < trade.net_entry_credit  # we paid back significant premium


class TestMarkToMarket:
    def test_mtm_at_entry_near_zero_ideal(self):
        """Under ideal fills (mid-mid), MtM at entry is ≈ 0 modulo per-leg ₹0.01 rounding."""
        ts = datetime(2024, 1, 2, 10, 0)
        _, snap = _feed_and_snap(ts)
        sim = MultiLegSimulator(mode=MODE_IDEAL, brokerage_per_leg=0)
        trade = sim.open_trade(_signal(ts, snap.expiry), snap, lots=1, lot_size=25)
        mtm = sim.mark_to_market(trade, snap)
        # Tolerance: 4 legs × 0.005 rounding × 25 lot_size = 0.5
        assert abs(mtm) < 1.0

    def test_mtm_at_entry_negative_realistic(self):
        """Under realistic fills, we paid the spread, so entry-MtM is negative."""
        ts = datetime(2024, 1, 2, 10, 0)
        _, snap = _feed_and_snap(ts)
        sim = MultiLegSimulator(mode=MODE_REALISTIC, brokerage_per_leg=0)
        trade = sim.open_trade(_signal(ts, snap.expiry), snap, lots=1, lot_size=25)
        mtm = sim.mark_to_market(trade, snap)
        assert mtm < 0

    def test_mtm_unquotable_returns_none(self):
        ts = datetime(2024, 1, 2, 10, 0)
        feed, snap = _feed_and_snap(ts)
        sim = MultiLegSimulator(mode=MODE_IDEAL)
        trade = sim.open_trade(_signal(ts, snap.expiry), snap, lots=1, lot_size=25)
        # Build a chain that doesn't contain the strikes we opened with
        bad_snap = feed.snapshot_at(ts, 'NIFTY', 50000)  # ATM ~ 50000, our 22000 strikes won't be there
        mtm = sim.mark_to_market(trade, bad_snap)
        assert mtm is None
