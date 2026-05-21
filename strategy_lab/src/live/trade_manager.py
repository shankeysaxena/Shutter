"""
Phase 6.2A — LiveTradeManager.

Mirrors BacktestSimulator's role in the live runtime:

  BacktestSimulator (backtest)           LiveTradeManager (live paper)
  ─────────────────────────────────      ──────────────────────────────
  process_signal_for_entry()             on_entry_fill()
  check_exits() — bar-level              check_exits() — bar-level
  EOD force-close                        flush_session()

On each completed bar, check_exits() is called for every open paper trade.
If the bar's high or low crosses a stop or target, an exit order is submitted
to the Executor. When the exit fill arrives (polled via get_fills), the trade
is closed, net P&L computed, and RiskEngine.record_trade_close() is called.

This keeps EngineState.open_trades / closed_trades accurate so strategy-level
duplicate-prevention and daily-cap logic behave identically to backtest.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from datetime import time as _time
from src.core.models import EngineState, Trade
from src.core.option_models import MultiLegSignal, MultiLegTrade, ChainSnapshot
from src.execution.executor import Executor, OrderRequest, OrderStatus
from src.live.bar_builder import LiveBar
from src.live.risk_engine import RiskEngine, RiskState

logger = logging.getLogger(__name__)


class LiveTradeManager:
    """
    Manages the full lifecycle of open paper trades:
      on_entry_fill → create Trade, push to engine_state.open_trades
      check_exits   → detect stop/target hit on each bar, submit exit order
      on_exit_fill  → close Trade, compute P&L, update risk state

    One instance shared across all instruments; disambiguated by trade_id.
    """

    def __init__(self, executor: Executor, risk_engine: RiskEngine,
                 lot_sizes: Dict[str, int]):
        self.executor    = executor
        self.risk_engine = risk_engine
        self.lot_sizes   = lot_sizes    # {instrument: units-per-lot}

        # Pending exit orders waiting for fill: {exit_order_id → trade}
        self._pending_exits: Dict[str, Trade] = {}

        # Phase 3: option paper executor (set after init via set_option_executor)
        self._option_executor = None

    # ----------------------------------------------------------------
    # Entry fill → Trade creation
    # ----------------------------------------------------------------

    def on_entry_fill(
        self,
        fill: OrderStatus,
        signal_stop:   float,
        signal_target: float,
        strategy_name: str,
        state:         EngineState,
        risk_state:    RiskState,
    ) -> Optional[Trade]:
        """
        Convert an entry fill into a Trade and add it to engine_state.open_trades.
        Returns the created Trade.
        """
        if not fill.is_filled or fill.fill_price is None:
            return None

        direction = fill.direction  # 'BUY' → 'LONG', 'SELL' → 'SHORT'
        trade_direction = 'LONG' if fill.direction == 'BUY' else 'SHORT'

        trade = Trade(
            trade_id      = fill.order_id,
            strategy_name = strategy_name,
            instrument    = fill.instrument,
            direction     = trade_direction,
            entry_time    = fill.fill_time or datetime.now(),
            entry_price   = fill.fill_price,
            stop_price    = signal_stop,
            target_price  = signal_target,
            exit_time     = None,
            exit_price    = None,
            exit_reason   = None,
            qty           = fill.filled_qty,
            gross_pnl     = 0.0,
            net_pnl       = 0.0,
            r_multiple    = None,
            runtime_mode  = 'live_paper',
        )
        state.open_trades.append(trade)
        state.per_strategy_day_trade_count[strategy_name] = (
            state.per_strategy_day_trade_count.get(strategy_name, 0) + 1
        )
        self.risk_engine.record_trade_open(risk_state)
        logger.info(
            f"Trade opened: {strategy_name} {trade_direction} {fill.instrument} "
            f"qty={fill.filled_qty} @ {fill.fill_price:.2f} "
            f"stop={signal_stop:.2f} target={signal_target:.2f}"
        )
        return trade

    # ----------------------------------------------------------------
    # Exit checking on each bar
    # ----------------------------------------------------------------

    def check_exits(
        self,
        bar:        LiveBar,
        state:      EngineState,
        risk_state: RiskState,
        brokerage:  float = 20.0,
    ) -> None:
        """
        Check every open trade for the given instrument against bar.high/bar.low.
        Mirrors BacktestSimulator.check_exits — conservative: stop before target.
        Submits exit orders for any trade that was hit.
        """
        to_close: List[Tuple[Trade, str]] = []

        for trade in state.open_trades:
            if trade.instrument != bar.instrument:
                continue

            reason, exit_price = self._check_trade_exit(trade, bar)
            if reason:
                to_close.append((trade, reason))
                # FIX 1: pass the specific reason (STOP / TARGET / EOD)
                self._submit_exit_order(trade, exit_price, bar.timestamp, reason)

        # Move hit trades to "awaiting exit fill" — remove from open_trades
        hit_ids = {t.trade_id for t, _ in to_close}
        state.open_trades = [t for t in state.open_trades if t.trade_id not in hit_ids]

    def _check_trade_exit(
        self, trade: Trade, bar: LiveBar
    ) -> Tuple[Optional[str], Optional[float]]:
        """
        Returns (exit_reason, exit_price) if the bar hits a stop or target,
        else (None, None). Stops take priority (conservative assumption).
        """
        if trade.direction == 'LONG':
            hit_stop   = bar.low  <= trade.stop_price
            hit_target = bar.high >= trade.target_price
        else:
            hit_stop   = bar.high >= trade.stop_price
            hit_target = bar.low  <= trade.target_price

        if hit_stop and hit_target:
            return 'STOP', trade.stop_price   # stop first (conservative)
        if hit_stop:
            return 'STOP', trade.stop_price
        if hit_target:
            return 'TARGET', trade.target_price
        return None, None

    def _submit_exit_order(
        self, trade: Trade, approx_exit_price: float, timestamp: datetime,
        exit_reason: str = 'stop_or_target',
    ) -> None:
        """Submit a market exit order and register it as a pending exit.

        FIX 1: store exit_detection_bar_ts in metadata so the closed trade
        carries both the bar that triggered the exit (backtest-equivalent timing)
        and the actual fill price/time (live-paper timing). The difference
        represents realistic fill slippage vs backtest stop-price assumption.
        """
        exit_direction = 'SELL' if trade.direction == 'LONG' else 'BUY'
        order_id = str(uuid.uuid4())
        req = OrderRequest(
            order_id         = order_id,
            instrument       = trade.instrument,
            direction        = exit_direction,
            order_type       = 'MARKET',
            quantity         = trade.qty,
            price            = None,
            strategy_name    = trade.strategy_name,
            signal_timestamp = timestamp,
            metadata         = {
                'trade_id':               trade.trade_id,
                'exit_reason':            exit_reason,
                'exit_detection_bar_ts':  timestamp.isoformat(),
                'exit_expected_price':    round(approx_exit_price, 2),
                # live fill price recorded at close time — compare against
                # exit_expected_price to measure bar-close-vs-fill slippage
            },
        )
        self.executor.submit_order(req)
        self._pending_exits[order_id] = trade

    # ----------------------------------------------------------------
    # Exit fill processing
    # ----------------------------------------------------------------

    def on_fills(
        self,
        fills:      List[OrderStatus],
        state:      EngineState,
        risk_state: RiskState,
        brokerage:  float = 20.0,
    ) -> None:
        """
        Process new fills from the executor. For exit fills, close the
        corresponding trade and update risk state.

        FIX 2: if an exit order is REJECTED or CANCELLED, the trade was already
        removed from open_trades. We cannot know the true position state, so we
        halt the session immediately rather than risk an orphaned open position.
        """
        for fill in fills:
            if fill.order_id in self._pending_exits:
                trade = self._pending_exits.pop(fill.order_id)
                if fill.status in ('REJECTED', 'CANCELLED'):
                    # Halt — we cannot safely manage this position anymore
                    risk_state.halted      = True
                    risk_state.halt_reason = (
                        f'EXIT_ORDER_{fill.status}:trade={trade.trade_id[:8]}'
                        f' strategy={trade.strategy_name} msg={fill.message}'
                    )
                    logger.critical(
                        f"EXIT ORDER {fill.status} — session halted. "
                        f"Trade {trade.trade_id[:8]} ({trade.strategy_name} "
                        f"{trade.direction} {trade.instrument}) has no exit. "
                        f"Reason: {fill.message}"
                    )
                    # Return the trade to open_trades as a record
                    # (cannot transact on it — session is halted)
                    state.open_trades.append(trade)
                    continue

                # Normal exit fill — close the trade
                self._close_trade(trade, fill, state, risk_state, brokerage)
            # Entry fills are handled separately in on_entry_fill

    def _close_trade(
        self,
        trade:      Trade,
        fill:       OrderStatus,
        state:      EngineState,
        risk_state: RiskState,
        brokerage:  float,
    ) -> None:
        """Finalise the trade P&L and record it."""
        if not fill.is_filled or fill.fill_price is None:
            logger.warning(f"Exit fill not filled for {trade.trade_id}: {fill.status}")
            return

        exit_price = fill.fill_price
        exit_time  = fill.fill_time or datetime.now()

        if trade.direction == 'LONG':
            gross_pnl = (exit_price - trade.entry_price) * trade.qty
        else:
            gross_pnl = (trade.entry_price - exit_price) * trade.qty

        net_pnl = gross_pnl - brokerage

        risk_amt = abs(trade.entry_price - trade.stop_price) * trade.qty
        r_multiple = net_pnl / risk_amt if risk_amt > 0 else None

        # FIX 1: record both detection bar and actual fill for slippage analysis
        detection_bar_ts  = fill.metadata.get('exit_detection_bar_ts')
        expected_price    = fill.metadata.get('exit_expected_price')
        bar_vs_fill_slip  = round(abs(exit_price - expected_price), 2) if expected_price else None

        trade.exit_price  = exit_price
        trade.exit_time   = exit_time
        trade.exit_reason = fill.metadata.get('exit_reason', 'FILL')
        trade.gross_pnl   = gross_pnl
        trade.net_pnl     = net_pnl
        trade.r_multiple  = r_multiple
        trade.metadata.update({
            'exit_detection_bar_ts': detection_bar_ts,
            'exit_expected_price':   expected_price,
            'bar_vs_fill_slippage':  bar_vs_fill_slip,
            # bar_vs_fill_slippage > 0 means live fill was worse than backtest
            # stop-price assumption; aggregate this in session reports
        })

        state.closed_trades.append(trade)
        self.risk_engine.record_trade_close(risk_state, trade.strategy_name, net_pnl)

        r_text = f"{r_multiple:.2f}" if r_multiple is not None else "N/A"
        logger.info(
            f"Trade closed: {trade.strategy_name} {trade.direction} {trade.instrument} "
            f"@ {exit_price:.2f}  net_pnl=₹{net_pnl:.2f}  R={r_text}"
        )
        # Notification is sent by the runtime (which holds the notifier reference)
        # Trade object has full context for the runtime to format the message.

    # ----------------------------------------------------------------
    # Session-end flush
    # ----------------------------------------------------------------

    def flush_session(
        self,
        last_bar:   LiveBar,
        state:      EngineState,
        risk_state: RiskState,
        brokerage:  float = 20.0,
    ) -> None:
        """Force-close all open trades at 15:29 using the last bar's close price."""
        for trade in list(state.open_trades):
            if trade.instrument != last_bar.instrument:
                continue
            order_id = str(uuid.uuid4())
            req = OrderRequest(
                order_id   = order_id,
                instrument = trade.instrument,
                direction  = 'SELL' if trade.direction == 'LONG' else 'BUY',
                order_type = 'MARKET',
                quantity   = trade.qty,
                price      = None,
                strategy_name    = trade.strategy_name,
                signal_timestamp = last_bar.timestamp,
                metadata   = {'exit_reason': 'EOD'},
            )
            self.executor.submit_order(req)
            self._pending_exits[order_id] = trade

        state.open_trades = [
            t for t in state.open_trades if t.instrument != last_bar.instrument
        ]

    def reset_session(self) -> None:
        self._pending_exits.clear()

    def set_option_executor(self, option_executor) -> None:
        self._option_executor = option_executor

    # ----------------------------------------------------------------
    # Phase 3 — Multi-leg (options) lifecycle
    # ----------------------------------------------------------------

    def on_multi_leg_entry(
        self,
        signal:      MultiLegSignal,
        chain:       'ChainSnapshot',
        state:       EngineState,
        risk_state:  RiskState,
        lots:        int = 1,
        fill_time:   Optional[datetime] = None,
    ) -> Optional[MultiLegTrade]:
        """
        Immediately fill a multi-leg signal and open the trade.
        Returns None if the fill fails (unquotable leg or executor not set).
        """
        if self._option_executor is None:
            logger.warning("on_multi_leg_entry: option executor not set — skipping")
            return None

        trade = self._option_executor.fill_signal(signal, chain, lots=lots, fill_time=fill_time)
        if trade is None:
            return None

        state.open_multi_leg_trades.append(trade)
        state.per_strategy_day_trade_count[signal.strategy_name] = (
            state.per_strategy_day_trade_count.get(signal.strategy_name, 0) + 1
        )
        self.risk_engine.record_multi_leg_open(risk_state)

        entry_prem = self._option_executor.entry_premium_per_lot(trade)
        logger.info(
            f"Option trade opened: {signal.strategy_name} {signal.structure_type} "
            f"{signal.instrument}  entry_debit=₹{entry_prem:.2f}/lot  "
            f"legs={[f'{l.option_type}{l.strike}' for l in signal.legs]}"
        )
        return trade

    def check_premium_exits(
        self,
        bar:         LiveBar,
        chain:       Optional['ChainSnapshot'],
        state:       EngineState,
        risk_state:  RiskState,
        options_cfg: dict,
    ) -> List[MultiLegTrade]:
        """
        Check every open multi-leg trade for premium stop / target / time exits.
        Returns list of trades that were closed this bar.

        Exit triggers (in priority order):
          1. Force-flat time (force_flat_after) — always exits
          2. Max hold time (max_hold_minutes) — exits regardless of P&L
          3. Premium stop (premium_stop_pct) — exit if P&L < -entry × stop_pct
          4. Premium target (premium_target_pct) — exit if P&L > +entry × target_pct
        """
        if self._option_executor is None or not state.open_multi_leg_trades:
            return []

        long_cfg    = options_cfg.get('long_option', {})
        stop_pct    = long_cfg.get('premium_stop_pct', 0.30)
        target_pct  = long_cfg.get('premium_target_pct', 0.50)
        max_hold    = long_cfg.get('max_hold_minutes', 60)
        force_after = long_cfg.get('force_flat_after', '15:10')
        fh, fm      = map(int, force_after.split(':'))
        force_time  = _time(fh, fm)

        closed = []
        still_open = []

        for trade in state.open_multi_leg_trades:
            if trade.instrument != bar.instrument:
                still_open.append(trade)
                continue

            # Force flat
            if bar.timestamp.time() >= force_time:
                reason = 'FORCE_FLAT'
            else:
                # Max hold time
                hold_mins = (bar.timestamp - trade.entry_time).total_seconds() / 60
                if hold_mins >= max_hold:
                    reason = 'MAX_HOLD_TIME'
                elif chain is None:
                    still_open.append(trade)
                    continue
                else:
                    mtm = self._option_executor.mark_to_market(trade, chain)
                    if mtm is None:
                        still_open.append(trade)   # chain too stale — skip
                        continue

                    entry_prem = self._option_executor.entry_premium_per_lot(trade)
                    if mtm <= -entry_prem * stop_pct:
                        reason = 'PREMIUM_STOP'
                    elif mtm >= entry_prem * target_pct:
                        reason = 'PREMIUM_TARGET'
                    else:
                        still_open.append(trade)
                        continue

            # Close the trade
            close_chain = chain if chain is not None else self._last_chain
            if close_chain is None:
                logger.warning(f"Cannot close {trade.trade_id[:8]}: no chain available")
                still_open.append(trade)
                continue

            ok = self._option_executor.close_trade(trade, close_chain, reason, bar.timestamp)
            if ok:
                state.closed_multi_leg_trades.append(trade)
                self.risk_engine.record_multi_leg_close(risk_state, trade.net_pnl or 0.0)
                closed.append(trade)
                logger.info(
                    f"Option trade closed: {trade.strategy_name} {reason} "
                    f"net_pnl=₹{trade.net_pnl or 0:.2f}"
                )
            else:
                still_open.append(trade)   # close failed; try again next bar

        state.open_multi_leg_trades = still_open
        return closed

    def flush_multi_leg_session(
        self,
        last_bar:    LiveBar,
        chain:       Optional['ChainSnapshot'],
        state:       EngineState,
        risk_state:  RiskState,
    ) -> None:
        """Force-close all open multi-leg trades at EOD."""
        if self._option_executor is None:
            return
        for trade in list(state.open_multi_leg_trades):
            if trade.instrument != last_bar.instrument:
                continue
            if chain is not None:
                ok = self._option_executor.close_trade(trade, chain, 'EOD', last_bar.timestamp)
                if ok:
                    state.closed_multi_leg_trades.append(trade)
                    self.risk_engine.record_multi_leg_close(risk_state, trade.net_pnl or 0.0)
        state.open_multi_leg_trades = [
            t for t in state.open_multi_leg_trades if t.instrument != last_bar.instrument
        ]
