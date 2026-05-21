"""
OptionPaperExecutor — live-paper execution for option multi-leg trades.

Thin wrapper around MultiLegSimulator that fits the live-paper lifecycle:
  fill_signal()     — immediate synthetic fill at session open bar
  mark_to_market()  — unrealized P&L using current chain mid-prices
  close_trade()     — synthetic exit fill; returns closed MultiLegTrade

Why a wrapper:
  MultiLegSimulator was designed for backtest (full session data available
  upfront). In live-paper, we call it one bar at a time, so we wrap it with
  the current bar's chain snapshot and lot-size config.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional

from src.core.option_models import ChainSnapshot, MultiLegSignal, MultiLegTrade
from src.execution.multi_leg_simulator import MultiLegSimulator

logger = logging.getLogger(__name__)


class OptionPaperExecutor:
    """
    Executes option multi-leg trades in paper mode using synthetic fills.

    Usage:
        executor = OptionPaperExecutor(simulator, lot_sizes={'NIFTY': 25})
        trade = executor.fill_signal(signal, chain, lots=1)
        pnl   = executor.mark_to_market(trade, chain)
        executor.close_trade(trade, chain, 'PREMIUM_STOP')
    """

    def __init__(
        self,
        simulator: MultiLegSimulator,
        lot_sizes: Dict[str, int],
    ):
        self._simulator = simulator
        self._lot_sizes = lot_sizes   # {underlying: rupee-multiplier per lot unit}

    def fill_signal(
        self,
        signal: MultiLegSignal,
        chain:  ChainSnapshot,
        lots:   int = 1,
        fill_time: Optional[datetime] = None,
    ) -> Optional[MultiLegTrade]:
        """
        Immediately fill a MultiLegSignal at current chain prices.
        Returns None if any leg is unquotable.
        """
        lot_size = self._lot_sizes.get(signal.instrument, 1)
        trade = self._simulator.open_trade(
            signal,
            chain,
            lots=lots,
            lot_size=lot_size,
            runtime_mode='live_paper',
            fill_time=fill_time or chain.timestamp,
        )
        if trade is None:
            logger.warning(
                f"OptionPaperExecutor: could not fill {signal.strategy_name} "
                f"{signal.structure_type} — unquotable leg"
            )
        return trade

    def mark_to_market(
        self,
        trade: MultiLegTrade,
        chain: ChainSnapshot,
    ) -> Optional[float]:
        """
        Unrealized P&L for an open trade at current chain mid-prices.
        Returns None if any leg is unquotable (stale chain).
        """
        return self._simulator.mark_to_market(trade, chain)

    def close_trade(
        self,
        trade:       MultiLegTrade,
        chain:       ChainSnapshot,
        exit_reason: str,
        exit_time:   Optional[datetime] = None,
    ) -> bool:
        """
        Close a trade at current chain prices.
        Returns True on success, False if any leg is unquotable.
        Mutates trade in-place (exit_fills, net_exit_debit, gross_pnl, net_pnl, exit_reason).
        """
        ok = self._simulator.close_trade(
            trade,
            chain,
            exit_time or chain.timestamp,
            exit_reason,
        )
        if not ok:
            logger.warning(
                f"OptionPaperExecutor: could not close {trade.trade_id[:8]} "
                f"({trade.strategy_name}) — unquotable leg. Chain ts={chain.timestamp}"
            )
        return ok

    def entry_premium_per_lot(self, trade: MultiLegTrade) -> float:
        """
        Absolute premium paid (debit) or received (credit) per lot.
        For a long option (debit): net_entry_credit is negative;
        this returns the magnitude of the debit.
        """
        return abs(trade.net_entry_credit)
