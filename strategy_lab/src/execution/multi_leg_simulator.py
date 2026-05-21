"""
Multi-leg execution simulator.

Routes MultiLegSignal -> MultiLegTrade with three fidelity modes:
  - ideal:        all legs fill at mid, no slippage
  - realistic:    shorts at bid, wings at ask + extra ticks (models naked-window)
  - pessimistic:  shorts at bid, wings at ask + larger extra ticks (stress test)

Provides mark_to_market(trade, chain) for unrealized P&L queries used by
the Iron Fly exit layers.

Single-leg path (Trade / BacktestSimulator) is untouched.
"""
import uuid
from datetime import datetime
from typing import List, Optional

from src.core.option_models import (
    MultiLegSignal,
    MultiLegTrade,
    OptionLeg,
    LegFill,
    ChainSnapshot,
    ChainQuote,
)


# Fidelity modes — keyed by name in config
MODE_IDEAL = 'ideal'
MODE_REALISTIC = 'realistic'
MODE_PESSIMISTIC = 'pessimistic'

# Wing extra-tick slippage per mode (over and above quoted side).
# Models the naked-window risk: shorts fill first, wings fill 1+ ticks worse.
_WING_EXTRA_TICKS = {
    MODE_IDEAL: 0,
    MODE_REALISTIC: 2,
    MODE_PESSIMISTIC: 5,
}


class MultiLegSimulator:
    """Fill simulation for multi-leg structures. Stateless across calls."""

    def __init__(
        self,
        mode: str = MODE_REALISTIC,
        tick_size: float = 0.05,
        brokerage_per_leg: float = 20.0,
    ):
        if mode not in _WING_EXTRA_TICKS:
            raise ValueError(f"Unknown mode: {mode}")
        self.mode = mode
        self.tick_size = tick_size
        self.brokerage_per_leg = brokerage_per_leg

    # ---------------------------------------------------------------
    # Entry
    # ---------------------------------------------------------------

    def open_trade(
        self,
        signal: MultiLegSignal,
        chain: ChainSnapshot,
        lots: int,
        lot_size: int,
        runtime_mode: str = 'backtest',
        fill_time: Optional[datetime] = None,
    ) -> Optional[MultiLegTrade]:
        """
        Fill every leg of `signal` against `chain`. Returns a MultiLegTrade
        with entry_fills and net_entry_credit populated, or None if any leg
        is unquotable in this snapshot.

        `fill_time` is the actual bar timestamp at which the fill happens —
        normally bar T+1 when the signal was emitted at bar T's close. If not
        provided, falls back to signal.timestamp for backward compatibility
        (caller is expected to pass it explicitly in production paths).
        """
        if lots <= 0:
            return None

        actual_entry_time = fill_time if fill_time is not None else signal.timestamp

        scaled_legs = [_scale_leg(l, lots) for l in signal.legs]
        fills: List[LegFill] = []
        net_credit = 0.0

        for leg in scaled_legs:
            quote = chain.quote(leg.strike, leg.option_type)
            if quote is None:
                return None  # cannot construct trade if any leg unquotable
            fill_price = self._entry_fill_price(leg, quote)
            fills.append(LegFill(leg=leg, fill_price=fill_price, fill_time=actual_entry_time))

            units = leg.qty * lot_size
            if leg.side == 'SELL':
                net_credit += fill_price * units
            else:
                net_credit -= fill_price * units

        return MultiLegTrade(
            trade_id=str(uuid.uuid4()),
            strategy_name=signal.strategy_name,
            instrument=signal.instrument,
            structure_type=signal.structure_type,
            entry_time=actual_entry_time,
            entry_fills=fills,
            net_entry_credit=round(net_credit, 2),
            runtime_mode=runtime_mode,
            metadata=dict(signal.metadata),
            lot_size=lot_size,
        )

    def _entry_fill_price(self, leg: OptionLeg, quote: ChainQuote) -> float:
        """Per-leg entry fill price under current mode."""
        extra = _WING_EXTRA_TICKS[self.mode] * self.tick_size

        if self.mode == MODE_IDEAL:
            return round((quote.bid + quote.ask) / 2.0, 2)

        if leg.side == 'SELL':
            # Sell at bid — no extra slippage on shorts (they fill first)
            return round(max(quote.bid, 0.05), 2)
        # BUY: wings — pay ask + extra slippage modeling naked-window delay
        return round(quote.ask + extra, 2)

    # ---------------------------------------------------------------
    # Exit
    # ---------------------------------------------------------------

    def close_trade(
        self,
        trade: MultiLegTrade,
        chain: ChainSnapshot,
        exit_time: datetime,
        exit_reason: str,
    ) -> bool:
        """
        Close all open legs of `trade` against `chain`. Mutates trade.
        Returns True if the trade was closed, False if any leg unquotable.
        """
        exit_fills: List[LegFill] = []
        net_debit = 0.0

        for entry_fill in trade.entry_fills:
            leg = entry_fill.leg
            quote = chain.quote(leg.strike, leg.option_type)
            if quote is None:
                return False
            fill_price = self._exit_fill_price(leg, quote)
            exit_fills.append(LegFill(leg=leg, fill_price=fill_price, fill_time=exit_time))

            units = leg.qty * trade.lot_size
            if leg.side == 'SELL':
                # We sold at entry; closing = buying back at ask -> debit
                net_debit += fill_price * units
            else:
                # We bought at entry; closing = selling at bid -> negative debit (credit on close)
                net_debit -= fill_price * units

        gross_pnl = trade.net_entry_credit - net_debit
        total_brokerage = self.brokerage_per_leg * len(trade.entry_fills) * 2  # entry + exit legs

        trade.exit_time = exit_time
        trade.exit_fills = exit_fills
        trade.net_exit_debit = round(net_debit, 2)
        trade.exit_reason = exit_reason
        trade.gross_pnl = round(gross_pnl, 2)
        trade.net_pnl = round(gross_pnl - total_brokerage, 2)
        return True

    def _exit_fill_price(self, leg: OptionLeg, quote: ChainQuote) -> float:
        """Per-leg exit fill price. Exits assumed market orders; no extra wing penalty."""
        if self.mode == MODE_IDEAL:
            return round((quote.bid + quote.ask) / 2.0, 2)

        # Closing a SELL = BUY at ask; closing a BUY = SELL at bid
        if leg.side == 'SELL':
            return round(quote.ask, 2)
        return round(max(quote.bid, 0.05), 2)

    # ---------------------------------------------------------------
    # Mark-to-market
    # ---------------------------------------------------------------

    def mark_to_market(self, trade: MultiLegTrade, chain: ChainSnapshot) -> Optional[float]:
        """
        Unrealized P&L for `trade` using mid prices from `chain`.
        Returns None if any leg cannot be quoted in the snapshot.
        Excludes brokerage (used for exit-decision math, not realized P&L).
        """
        unrealized_close_value = 0.0
        for entry_fill in trade.entry_fills:
            leg = entry_fill.leg
            quote = chain.quote(leg.strike, leg.option_type)
            if quote is None:
                return None
            mid = (quote.bid + quote.ask) / 2.0
            units = leg.qty * trade.lot_size
            if leg.side == 'SELL':
                unrealized_close_value += mid * units
            else:
                unrealized_close_value -= mid * units
        return round(trade.net_entry_credit - unrealized_close_value, 2)


def _scale_leg(leg: OptionLeg, lots: int) -> OptionLeg:
    """Return a copy of `leg` with qty multiplied by lots."""
    return OptionLeg(
        instrument=leg.instrument,
        expiry=leg.expiry,
        strike=leg.strike,
        option_type=leg.option_type,
        side=leg.side,
        qty=leg.qty * lots,
    )
