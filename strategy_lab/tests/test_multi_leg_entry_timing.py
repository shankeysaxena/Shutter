"""v2.1 #7 — lock the multi-leg entry-fill timing semantics.

A multi-leg signal generated on bar T's close must fill on bar T+1 using the
chain snapshot keyed by bar T+1's (timestamp, close). This test asserts that
behavior with a stub chain feed that returns identifiably-different snapshots
at different timestamps.
"""
from datetime import date, datetime
from typing import List, Optional

import pytest

from src.core.engine import BarEngine
from src.core.models import BarEvent, Candle, EngineState, FeatureSnapshot
from src.core.option_models import (
    ChainQuote,
    ChainSnapshot,
    MultiLegSignal,
    OptionLeg,
)
from src.execution.multi_leg_simulator import MultiLegSimulator, MODE_IDEAL
from src.execution.simulator import BacktestSimulator
from src.feeds.option_chain_snapshot import OptionChainFeed
from src.strategies.base import BaseStrategy


class _StubChainFeed(OptionChainFeed):
    """Returns a chain whose ATM IV varies by minute so we can identify which
    snapshot the simulator used to fill the trade."""

    def __init__(self):
        # Keyed by .minute attribute of the bar timestamp
        self.iv_by_minute = {
            0: 0.10,    # bar T   = 10:00
            1: 0.20,    # bar T+1 = 10:01  (distinguishable)
            2: 0.30,    # bar T+2 = 10:02
        }
        self.queries: List[datetime] = []

    def snapshot_at(self, timestamp, underlying, spot):
        self.queries.append(timestamp)
        atm_iv = self.iv_by_minute.get(timestamp.minute, 0.15)
        expiry = date(2024, 1, 4)
        quotes = []
        for strike in (21800, 22000, 22200):
            for opt in ('CE', 'PE'):
                # Prices encoded as 100 * atm_iv + strike-offset hash so we can
                # trace exactly which fill came from which snapshot.
                base = 100.0 * atm_iv
                quotes.append(ChainQuote(
                    strike=strike, option_type=opt,
                    bid=round(base, 2), ask=round(base + 0.5, 2),
                    last=round(base + 0.25, 2), iv=atm_iv,
                ))
        return ChainSnapshot(timestamp, underlying, spot, expiry, quotes, atm_iv)


class _OneShotStrategy(BaseStrategy):
    """Emits one preconstructed multi-leg signal on the first call, then nothing."""
    name = 'STUB_IRON_FLY'

    def __init__(self, signal: MultiLegSignal):
        self._signal = signal
        self._emitted = False

    def generate_signal(self, ctx):
        if self._emitted:
            return None
        self._emitted = True
        return self._signal

    def evaluate_multi_leg_exits(self, ctx):
        return []


def _bar(ts: datetime, close: float = 22000.0) -> BarEvent:
    candle = Candle(ts, 'NIFTY', close - 1, close + 1, close - 1, close, 1000)
    features = FeatureSnapshot(
        session_date=ts.date(), minute_index=ts.minute,
        prior_close=close, vwap=close, vwap_distance=0.0,
        above_vwap=False, below_vwap=False,
        or_high=close + 10, or_low=close - 10, or_width=20, or_ready=True,
        gap_pct=0, gap_direction=None,
        session_high_so_far=close + 5, session_low_so_far=close - 5,
    )
    return BarEvent(candle, features, True, 'backtest')


class TestMultiLegEntryTiming:
    def test_signal_emitted_on_bar_T_fills_using_bar_T_plus_1_chain(self):
        feed = _StubChainFeed()
        ml_sim = MultiLegSimulator(mode=MODE_IDEAL, brokerage_per_leg=0)
        sl_sim = BacktestSimulator()

        # Signal references one short call leg at strike 22000
        signal = MultiLegSignal(
            strategy_name='STUB_IRON_FLY',
            instrument='NIFTY',
            timestamp=datetime(2024, 1, 2, 10, 0),
            structure_type='IRON_FLY',
            legs=[
                OptionLeg('NIFTY', date(2024, 1, 4), 22000, 'CE', 'SELL', qty=1),
            ],
            metadata={'max_loss_per_lot_rupees': 100, 'lot_size': 25},
        )

        strategy = _OneShotStrategy(signal)
        engine = BarEngine(
            strategies=[strategy], simulator=sl_sim, config={
                'risk': {'lot_size': {'NIFTY': 25}, 'max_total_trades_per_day': 10},
                'strategies': {'iron_fly': {'capital': 1_000_000,
                                              'risk_per_trade_pct': 0.005,
                                              'max_lots_per_trade': 5}},
            },
            multi_leg_simulator=ml_sim, chain_feed=feed,
        )

        state = EngineState(
            instrument='NIFTY', session_date=date(2024, 1, 2),
            open_trades=[], closed_trades=[], queued_signals=[],
            per_strategy_day_trade_count={'STUB_IRON_FLY': 0},
        )

        # Bar T (10:00): strategy emits signal, no trade yet
        engine.process_bar(_bar(datetime(2024, 1, 2, 10, 0)), state, 'NIFTY')
        assert len(state.queued_multi_leg_signals) == 1
        assert len(state.open_multi_leg_trades) == 0

        # Bar T+1 (10:01): signal fills using THIS bar's chain (atm_iv = 0.20)
        engine.process_bar(_bar(datetime(2024, 1, 2, 10, 1)), state, 'NIFTY')
        assert len(state.queued_multi_leg_signals) == 0
        assert len(state.open_multi_leg_trades) == 1

        trade = state.open_multi_leg_trades[0]
        fill = trade.entry_fills[0]
        # Bar T+1 chain has atm_iv=0.20 -> bid 20.0, ask 20.5, ideal-mode mid 20.25.
        # Bar T   chain has atm_iv=0.10 -> bid 10.0, ask 10.5, mid 10.25.
        # Must be the T+1 value, NOT the T value.
        assert abs(fill.fill_price - 20.25) < 0.01, (
            f"fill_price {fill.fill_price} should use bar T+1's chain (mid 20.25), "
            f"not bar T's (mid 10.25)"
        )

        # v2.2: trade.entry_time and fill_time must reflect the FILL bar (T+1),
        # not the signal-emission bar (T). Earlier versions used signal.timestamp
        # for both, which silently mis-stamped trades in the ledger.
        expected_fill_ts = datetime(2024, 1, 2, 10, 1)
        assert trade.entry_time == expected_fill_ts, (
            f"trade.entry_time {trade.entry_time} should be bar T+1 ({expected_fill_ts})"
        )
        assert fill.fill_time == expected_fill_ts, (
            f"LegFill.fill_time {fill.fill_time} should be bar T+1 ({expected_fill_ts})"
        )
        assert trade.entry_time != signal.timestamp, (
            "trade.entry_time must differ from signal emission time (regression guard)"
        )

    def test_chain_feed_called_once_per_bar(self):
        """Engine consults the chain feed once per bar, regardless of how many strategies."""
        feed = _StubChainFeed()
        ml_sim = MultiLegSimulator(mode=MODE_IDEAL)
        sl_sim = BacktestSimulator()
        engine = BarEngine(
            strategies=[], simulator=sl_sim, config={'risk': {}},
            multi_leg_simulator=ml_sim, chain_feed=feed,
        )
        state = EngineState(
            instrument='NIFTY', session_date=date(2024, 1, 2),
            open_trades=[], closed_trades=[], queued_signals=[],
            per_strategy_day_trade_count={},
        )
        engine.process_bar(_bar(datetime(2024, 1, 2, 10, 0)), state, 'NIFTY')
        engine.process_bar(_bar(datetime(2024, 1, 2, 10, 1)), state, 'NIFTY')
        assert len(feed.queries) == 2
        assert feed.queries[0].minute == 0
        assert feed.queries[1].minute == 1


class _NullChainFeed(OptionChainFeed):
    """Returns None for every query — simulates missing historical chain data."""
    def snapshot_at(self, timestamp, underlying, spot):
        return None


class TestEodMissingChain:
    """v2.2 #2 — EOD must not silently lose multi-leg trades when chain is missing.

    Construct an open multi-leg trade, then call force_eod_exits with a chain
    feed that returns None. Trade must move to closed_multi_leg_trades with
    exit_reason='EOD_NO_CHAIN' and the event log must contain a warning entry.
    """

    def _make_engine_with_open_trade(self, chain_feed_at_eod):
        # Build the engine with a working stub feed so the trade opens normally
        feed_for_entry = _StubChainFeed()
        ml_sim = MultiLegSimulator(mode=MODE_IDEAL, brokerage_per_leg=0)
        sl_sim = BacktestSimulator()

        signal = MultiLegSignal(
            strategy_name='STUB_IRON_FLY', instrument='NIFTY',
            timestamp=datetime(2024, 1, 2, 10, 0), structure_type='IRON_FLY',
            legs=[OptionLeg('NIFTY', date(2024, 1, 4), 22000, 'CE', 'SELL', qty=1)],
            metadata={'max_loss_per_lot_rupees': 100, 'lot_size': 25},
        )

        engine = BarEngine(
            strategies=[_OneShotStrategy(signal)], simulator=sl_sim,
            config={'risk': {'lot_size': {'NIFTY': 25}, 'max_total_trades_per_day': 10},
                     'strategies': {'iron_fly': {'capital': 1_000_000,
                                                   'risk_per_trade_pct': 0.005,
                                                   'max_lots_per_trade': 5}}},
            multi_leg_simulator=ml_sim, chain_feed=feed_for_entry,
        )
        state = EngineState(
            instrument='NIFTY', session_date=date(2024, 1, 2),
            open_trades=[], closed_trades=[], queued_signals=[],
            per_strategy_day_trade_count={'STUB_IRON_FLY': 0},
        )
        # Drive two bars to open the trade
        engine.process_bar(_bar(datetime(2024, 1, 2, 10, 0)), state, 'NIFTY')
        engine.process_bar(_bar(datetime(2024, 1, 2, 10, 1)), state, 'NIFTY')
        assert len(state.open_multi_leg_trades) == 1

        # Swap to the no-chain feed for EOD
        engine.chain_feed = chain_feed_at_eod
        return engine, state

    def test_trade_marked_eod_no_chain_when_feed_returns_none(self):
        engine, state = self._make_engine_with_open_trade(_NullChainFeed())
        last_bar = _bar(datetime(2024, 1, 2, 15, 30))
        log = engine.force_eod_exits(last_bar, state, 'NIFTY')

        # Trade migrated to closed list
        assert len(state.open_multi_leg_trades) == 0
        assert len(state.closed_multi_leg_trades) == 1

        t = state.closed_multi_leg_trades[0]
        assert t.exit_reason == 'EOD_NO_CHAIN'
        assert t.exit_time == last_bar.candle.timestamp
        assert t.net_pnl is None       # P&L cannot be computed; honestly nil
        assert t.gross_pnl is None

        # Warning event surfaced in the log
        warnings = [e for e in log if e.get('event_type') == 'multi_leg_eod_no_chain']
        assert len(warnings) == 1
        assert warnings[0].get('severity') == 'warning'
        assert warnings[0].get('trade_id') == t.trade_id

    def test_normal_eod_close_still_works_when_chain_present(self):
        """Regression: if chain IS available at EOD, the normal close path still runs."""
        feed = _StubChainFeed()
        engine, state = self._make_engine_with_open_trade(feed)
        last_bar = _bar(datetime(2024, 1, 2, 15, 30))
        log = engine.force_eod_exits(last_bar, state, 'NIFTY')

        assert len(state.closed_multi_leg_trades) == 1
        t = state.closed_multi_leg_trades[0]
        assert t.exit_reason == 'EOD'
        assert t.net_pnl is not None   # normal close computes P&L
        # No EOD_NO_CHAIN warning
        warnings = [e for e in log if e.get('event_type') == 'multi_leg_eod_no_chain']
        assert len(warnings) == 0
