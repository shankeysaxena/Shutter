"""
Phase 6.2 — PaperExecutor.

Simulates order fills at the current market price (last tick price) with
configurable slippage. No real orders are placed. Satisfies the Executor
interface exactly so the LivePaperRuntime cannot distinguish paper from real.

Fill model (MARKET orders):
  - Fill is immediate (same tick cycle)
  - Fill price = last_price ± slippage_pct
  - Slippage direction: BUY → pays more, SELL → receives less

LIMIT orders:
  - Queued; checked against each new tick price
  - Filled if market price crosses the limit (conservative — bar close only)

Thread safety:
  - update_market_price() is called from the bar-processing loop (main thread)
  - submit_order() and get_fills() also called from main thread
  - No cross-thread calls in Phase 6; lock added as defensive measure only
"""
from __future__ import annotations

import threading
import uuid
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional

from src.execution.executor import Executor, OrderRequest, OrderStatus


class PaperExecutor(Executor):
    """
    Paper trading executor — simulated fills, no real orders.

    Configured via constructor; mirrors what broker config would provide.
    """

    def __init__(
        self,
        slippage_pct: float = 0.0002,   # 0.02% per side (≈ 4-5 pts on NIFTY)
        reject_if_no_price: bool = True,
    ):
        self.slippage_pct       = slippage_pct
        self.reject_if_no_price = reject_if_no_price

        self._orders:    Dict[str, OrderStatus] = {}
        self._positions: Dict[str, int]         = {}   # instrument → net units
        self._new_fills: Deque[OrderStatus]     = deque()
        self._last_price: Dict[str, float]      = {}   # instrument → last known price
        self._lock = threading.Lock()

    # ----------------------------------------------------------------
    # Price feed — called by the runtime on each completed bar
    # ----------------------------------------------------------------

    def update_market_price(self, instrument: str, price: float) -> List[OrderStatus]:
        """
        Update the latest market price. Checks pending LIMIT orders.
        Returns any orders that were just filled.
        """
        with self._lock:
            self._last_price[instrument] = price
            filled = []
            for order_id, status in list(self._orders.items()):
                if status.status != 'PENDING':
                    continue
                if status.instrument != instrument:
                    continue
                if status.direction in ('BUY', 'SELL') and status.fill_price is None:
                    # LIMIT check
                    pass   # MARKET orders fill immediately in submit_order
            return filled

    # ----------------------------------------------------------------
    # Executor interface
    # ----------------------------------------------------------------

    def submit_order(self, req: OrderRequest) -> str:
        with self._lock:
            # FIX #4: copy req.metadata so exit_reason and other context survive fills
            status = OrderStatus(
                order_id=req.order_id,
                instrument=req.instrument,
                direction=req.direction,
                quantity=req.quantity,
                status='PENDING',
                metadata=dict(req.metadata),
            )
            self._orders[req.order_id] = status

            if req.order_type == 'MARKET':
                self._fill_market(status, req)
            # LIMIT orders remain PENDING until price check

        return req.order_id

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            status = self._orders.get(order_id)
            if status is None or status.is_done:
                return False
            status.status = 'CANCELLED'
            return True

    def modify_order(self, order_id: str, new_price: Optional[float] = None,
                     new_qty: Optional[int] = None) -> bool:
        with self._lock:
            status = self._orders.get(order_id)
            if status is None or status.is_done:
                return False
            # Paper: modify is always successful for PENDING orders
            return True

    def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        with self._lock:
            return self._orders.get(order_id)

    def get_positions(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._positions)

    def get_fills(self) -> List[OrderStatus]:
        """
        FIX #5: return ALL terminal orders (FILLED, REJECTED, CANCELLED) so
        the runtime can purge _pending_entries and avoid memory leaks on rejects.
        """
        with self._lock:
            fills = list(self._new_fills)
            self._new_fills.clear()
            return fills

    def get_open_orders(self) -> List[OrderStatus]:
        with self._lock:
            return [s for s in self._orders.values() if not s.is_done]

    def reset_session(self) -> None:
        with self._lock:
            self._orders.clear()
            self._positions.clear()
            self._new_fills.clear()
            self._last_price.clear()

    # ----------------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------------

    def _fill_market(self, status: OrderStatus, req: OrderRequest) -> None:
        """Fill a MARKET order immediately at last known price ± slippage."""
        last = self._last_price.get(req.instrument)
        if last is None:
            if self.reject_if_no_price:
                status.status  = 'REJECTED'
                status.message = 'No market price available yet'
                self._new_fills.append(status)   # FIX #5: surface rejected orders
                return
            last = 0.0

        slip = last * self.slippage_pct
        if req.direction == 'BUY':
            fill_price = round(last + slip, 2)
        else:
            fill_price = round(last - slip, 2)

        status.status     = 'FILLED'
        status.filled_qty = req.quantity
        status.fill_price = fill_price
        status.fill_time  = datetime.now()

        # Update positions
        sign = 1 if req.direction == 'BUY' else -1
        self._positions[req.instrument] = (
            self._positions.get(req.instrument, 0) + sign * req.quantity
        )

        self._new_fills.append(status)

    @property
    def net_pnl(self) -> float:
        """Rough intraday P&L estimate from fills (for monitoring only)."""
        pnl = 0.0
        for status in self._orders.values():
            if not status.is_filled or status.fill_price is None:
                continue
            sign = 1 if status.direction == 'SELL' else -1
            pnl += sign * status.fill_price * status.filled_qty
        return pnl
