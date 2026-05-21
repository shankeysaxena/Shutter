"""
BarEngine — the single shared per-bar processing core.

Both BacktestRuntime and ReplayRuntime delegate all engine logic here.
This ensures:
- bug fixes apply to both runtimes simultaneously
- event logging is identical across runtimes
- no behavioral drift between backtest and replay
"""
from typing import List, Dict, Optional
from src.core.models import Candle, BarEvent, EngineState, StrategyContext, Trade
from src.core.option_models import MultiLegSignal, MultiLegTrade
from src.strategies.base import BaseStrategy
from src.execution.simulator import BacktestSimulator
from src.execution.multi_leg_simulator import MultiLegSimulator
from src.feeds.option_chain_snapshot import OptionChainFeed


class BarEngine:
    """
    Processes one BarEvent at a time through the full engine loop:
      1.  Fill queued single-leg signals at bar open
      1b. Fill queued multi-leg signals using this bar's chain snapshot
      2.  Check stop/target exits for single-leg trades (uses bar high/low)
      2b. Evaluate multi-leg exits using this bar's chain
      3.  Generate new signals from bar close (single-leg or multi-leg)

    Returns structured event log entries for every bar so both
    backtest and replay have full traceability.

    --- v2.1 #7: Fill-timing semantics ---

    Single-leg signal generated on bar T's close fills at bar T+1's OPEN price
    (via `BacktestSimulator.process_signal_for_entry` reading `candle.open`).
    No look-ahead.

    Multi-leg signal generated on bar T's close fills using the ChainSnapshot
    queried at bar T+1's timestamp+close. The chain feed is consulted exactly
    once per bar at the top of `process_bar`, using `bar_event.candle.close`
    as the spot input. So multi-leg fills carry a small look-ahead bias
    (T+1 close vs. T+1 open for single-leg) — accepted because bar-resolution
    backtests have no intra-bar chain snapshot.

    Practical impact: multi-leg fill prices reflect bar-close option quotes,
    not bar-open. Anyone reading the ledger should know this when comparing
    single-leg and multi-leg entry timing. Documented in spec v2.1.
    """

    def __init__(
        self,
        strategies: List[BaseStrategy],
        simulator: BacktestSimulator,
        config: Dict,
        multi_leg_simulator: Optional[MultiLegSimulator] = None,
        chain_feed: Optional[OptionChainFeed] = None,
    ):
        self.strategies = strategies
        self.simulator = simulator
        self.config = config
        self.multi_leg_simulator = multi_leg_simulator
        self.chain_feed = chain_feed
        self._strategy_by_name = {s.name: s for s in strategies}

    def reset_strategies(self) -> None:
        """Reset all stateful strategies. Call once per instrument before processing begins."""
        for strategy in self.strategies:
            strategy.reset()

    def process_bar(
        self,
        bar_event: BarEvent,
        state: EngineState,
        instrument: str,
    ) -> List[Dict]:
        """
        Process one finalized bar. Mutates state in place.
        Returns a list of structured log entries for this bar.
        """
        event_log: List[Dict] = []
        ts = bar_event.candle.timestamp

        # Look up the chain snapshot for this bar (if configured)
        chain = None
        if self.chain_feed is not None:
            chain = self.chain_feed.snapshot_at(ts, instrument, bar_event.candle.close)

        # Step 1: Fill queued single-leg signals at this bar's open
        for signal in state.queued_signals:
            qty = self.config.get('risk', {}).get('lot_size', {}).get(instrument, 1)
            trade = self.simulator.process_signal_for_entry(signal, bar_event, qty=qty)
            if trade:
                state.open_trades.append(trade)
                state.per_strategy_day_trade_count[signal.strategy_name] += 1
                event_log.append(_log('entry_filled', ts, instrument,
                                      strategy=signal.strategy_name,
                                      direction=signal.direction,
                                      entry_price=trade.entry_price,
                                      stop_price=trade.stop_price,
                                      target_price=trade.target_price))
        state.queued_signals = []

        # Step 1b: Fill queued multi-leg signals at this bar's chain snapshot
        if self.multi_leg_simulator is not None and state.queued_multi_leg_signals:
            self._fill_queued_multi_leg(state, chain, ts, instrument,
                                         bar_event.runtime_mode, event_log)

        # Step 2: Check single-leg exits for all open trades
        still_open: List[Trade] = []
        for t in state.open_trades:
            exited = self.simulator.check_exits(t, bar_event, is_eod=False)
            if exited:
                state.closed_trades.append(t)
                event_log.append(_log(f'exit_{t.exit_reason.lower()}', ts, instrument,
                                      strategy=t.strategy_name,
                                      direction=t.direction,
                                      exit_price=t.exit_price,
                                      net_pnl=t.net_pnl,
                                      r_multiple=t.r_multiple))
            else:
                still_open.append(t)
        state.open_trades = still_open

        # Step 2b: Evaluate multi-leg exits
        if self.multi_leg_simulator is not None and state.open_multi_leg_trades and chain is not None:
            self._evaluate_multi_leg_exits(state, bar_event, chain, ts, instrument, event_log)

        # Step 3: Generate new signals — cap checked per signal before each strategy.
        # v2.1 #5: multi-leg trades (open + queued + closed today) count toward
        # the daily cap so a multi-leg strategy can't sidestep the global limit.
        max_daily = self.config.get('risk', {}).get('max_total_trades_per_day', 4)
        ctx = StrategyContext(
            bar_event=bar_event,
            engine_state=state,
            strategy_config=self.config.get('strategies', {}),
            chain_snapshot=chain,
        )
        for strategy in self.strategies:
            single_leg_committed = (
                sum(state.per_strategy_day_trade_count.values())
                + len(state.queued_signals)
            )
            multi_leg_committed = (
                len(state.open_multi_leg_trades)
                + len(state.closed_multi_leg_trades)
                + len(state.queued_multi_leg_signals)
            )
            total_committed = single_leg_committed + multi_leg_committed
            if total_committed >= max_daily:
                event_log.append(_log('signal_rejected', ts, instrument,
                                      strategy=strategy.name,
                                      reason='daily_cap_reached',
                                      cap=max_daily,
                                      committed=total_committed))
                break

            signal = strategy.generate_signal(ctx)
            if signal is None:
                reason = strategy.explain_no_signal(ctx)
                event_log.append(_log('no_signal', ts, instrument,
                                      strategy=strategy.name,
                                      reason=reason))
            elif isinstance(signal, MultiLegSignal):
                state.queued_multi_leg_signals.append(signal)
                event_log.append(_log('multi_leg_signal_queued', ts, instrument,
                                      strategy=signal.strategy_name,
                                      structure_type=signal.structure_type,
                                      metadata=signal.metadata))
            else:
                state.queued_signals.append(signal)
                event_log.append(_log('signal_queued', ts, instrument,
                                      strategy=signal.strategy_name,
                                      direction=signal.direction,
                                      stop_price=signal.stop_price,
                                      metadata=signal.metadata))

        return event_log

    # ----------------------------------------------------------------
    # Multi-leg helpers
    # ----------------------------------------------------------------

    def _fill_queued_multi_leg(
        self,
        state: EngineState,
        chain,
        ts,
        instrument: str,
        runtime_mode: str,
        event_log: List[Dict],
    ) -> None:
        carried_forward: List[MultiLegSignal] = []
        for sig in state.queued_multi_leg_signals:
            if chain is None:
                # No chain this bar — cannot fill; notify strategy and drop
                self._notify_rejected(sig)
                event_log.append(_log('multi_leg_signal_rejected', ts, instrument,
                                      strategy=sig.strategy_name,
                                      reason='chain_not_available_at_fill'))
                continue

            lots = self._size_multi_leg(sig)
            if lots <= 0:
                self._notify_rejected(sig)
                event_log.append(_log('multi_leg_signal_rejected', ts, instrument,
                                      strategy=sig.strategy_name,
                                      reason='zero_lots'))
                continue

            lot_size = sig.metadata.get('lot_size', 1)
            # v2.2: pass actual bar timestamp as fill_time so entry_time on the
            # trade reflects the fill bar (T+1), not the signal-emission bar (T).
            trade = self.multi_leg_simulator.open_trade(
                sig, chain, lots=lots, lot_size=lot_size, runtime_mode=runtime_mode,
                fill_time=ts,
            )
            if trade is None:
                self._notify_rejected(sig)
                event_log.append(_log('multi_leg_signal_rejected', ts, instrument,
                                      strategy=sig.strategy_name,
                                      reason='unquotable_leg'))
                continue

            state.open_multi_leg_trades.append(trade)
            self._notify_filled(trade)
            event_log.append(_log('multi_leg_entry_filled', ts, instrument,
                                  strategy=trade.strategy_name,
                                  structure_type=trade.structure_type,
                                  net_credit=trade.net_entry_credit,
                                  lots=lots,
                                  trade_id=trade.trade_id))
        state.queued_multi_leg_signals = carried_forward

    def _evaluate_multi_leg_exits(
        self, state: EngineState, bar_event: BarEvent, chain, ts, instrument: str,
        event_log: List[Dict],
    ) -> None:
        ctx_exits = StrategyContext(
            bar_event=bar_event,
            engine_state=state,
            strategy_config=self.config.get('strategies', {}),
            chain_snapshot=chain,
        )
        exits_by_id: Dict[str, str] = {}
        for strategy in self.strategies:
            for trade, reason in strategy.evaluate_multi_leg_exits(ctx_exits):
                exits_by_id[trade.trade_id] = reason

        if not exits_by_id:
            return

        still_open: List[MultiLegTrade] = []
        for t in state.open_multi_leg_trades:
            reason = exits_by_id.get(t.trade_id)
            if reason is None:
                still_open.append(t)
                continue
            closed = self.multi_leg_simulator.close_trade(t, chain, ts, reason)
            if not closed:
                still_open.append(t)
                continue
            state.closed_multi_leg_trades.append(t)
            self._notify_closed(t)
            event_log.append(_log(f'multi_leg_exit_{reason.lower()}', ts, instrument,
                                  strategy=t.strategy_name,
                                  structure_type=t.structure_type,
                                  gross_pnl=t.gross_pnl,
                                  net_pnl=t.net_pnl,
                                  trade_id=t.trade_id))
        state.open_multi_leg_trades = still_open

    def _size_multi_leg(self, signal: MultiLegSignal) -> int:
        from src.core.option_intent import STRUCTURE_LONG_OPTION, STRUCTURE_DEBIT_SPREAD
        # Long-option / debit-spread: Phase 1 always uses 1 lot.
        # Risk-budget sizing comes in Phase 2 once premium stop-loss is wired.
        if signal.structure_type in (STRUCTURE_LONG_OPTION, STRUCTURE_DEBIT_SPREAD):
            max_lots = self.config.get('risk_engine', {}).get('max_lots_per_trade', 1)
            return max(1, min(max_lots, 1))

        # Iron Fly and other credit structures: size by max-loss budget
        cfg = self.config.get('strategies', {}).get('iron_fly', {})
        capital = cfg.get('capital', 1000000)
        risk_pct = cfg.get('risk_per_trade_pct', 0.005)
        max_lots = cfg.get('max_lots_per_trade', 10)
        max_loss_per_lot = signal.metadata.get('max_loss_per_lot_rupees', 0)
        if max_loss_per_lot <= 0:
            return 0
        risk_budget = capital * risk_pct
        lots = int(risk_budget // max_loss_per_lot)
        return max(0, min(lots, max_lots))

    def _notify_filled(self, trade: MultiLegTrade) -> None:
        s = self._strategy_by_name.get(trade.strategy_name)
        if s is not None and hasattr(s, 'on_multi_leg_filled'):
            s.on_multi_leg_filled(trade)

    def _notify_closed(self, trade: MultiLegTrade) -> None:
        s = self._strategy_by_name.get(trade.strategy_name)
        if s is not None and hasattr(s, 'on_multi_leg_closed'):
            s.on_multi_leg_closed(trade)

    def _notify_rejected(self, signal: MultiLegSignal) -> None:
        s = self._strategy_by_name.get(signal.strategy_name)
        if s is not None and hasattr(s, 'on_multi_leg_rejected'):
            s.on_multi_leg_rejected(signal)

    # ----------------------------------------------------------------
    # End of session
    # ----------------------------------------------------------------

    def force_eod_exits(
        self,
        last_bar_event: BarEvent,
        state: EngineState,
        instrument: str,
    ) -> List[Dict]:
        """
        Force-close all remaining open trades at end of session.
        Single-leg trades use the last bar's close. Multi-leg trades use the
        last available chain snapshot.
        """
        event_log: List[Dict] = []

        # Single-leg path (unchanged)
        if state.open_trades:
            last_close = last_bar_event.candle.close
            eod_candle = Candle(
                last_bar_event.candle.timestamp, instrument,
                last_close, last_close, last_close, last_close, 0,
            )
            eod_bar = BarEvent(
                candle=eod_candle,
                features=last_bar_event.features,
                is_bar_closed=True,
                runtime_mode=last_bar_event.runtime_mode,
            )
            for t in state.open_trades:
                self.simulator.check_exits(t, eod_bar, is_eod=True)
                state.closed_trades.append(t)
                event_log.append(_log('exit_eod', eod_candle.timestamp, instrument,
                                      strategy=t.strategy_name,
                                      direction=t.direction,
                                      exit_price=t.exit_price,
                                      net_pnl=t.net_pnl,
                                      r_multiple=t.r_multiple))
            state.open_trades = []

        # Multi-leg path: force-close remaining at last chain snapshot.
        # v2.2: if the chain is unavailable at EOD (missing historical data,
        # feed outage), trades MUST NOT silently linger on open_multi_leg_trades
        # and disappear from reports. Emit a loud warning and mark them with
        # exit_reason='EOD_NO_CHAIN' so they surface in the ledger with a clear
        # signal that P&L could not be computed.
        if state.open_multi_leg_trades and self.multi_leg_simulator is not None:
            ts = last_bar_event.candle.timestamp
            chain = None
            if self.chain_feed is not None:
                chain = self.chain_feed.snapshot_at(ts, instrument, last_bar_event.candle.close)

            if chain is not None:
                for t in list(state.open_multi_leg_trades):
                    if self.multi_leg_simulator.close_trade(t, chain, ts, 'EOD'):
                        state.closed_multi_leg_trades.append(t)
                        self._notify_closed(t)
                        event_log.append(_log('multi_leg_exit_eod', ts, instrument,
                                              strategy=t.strategy_name,
                                              structure_type=t.structure_type,
                                              gross_pnl=t.gross_pnl,
                                              net_pnl=t.net_pnl,
                                              trade_id=t.trade_id))
            else:
                # Chain missing — mark trades as orphaned, surface loudly
                for t in list(state.open_multi_leg_trades):
                    t.exit_time = ts
                    t.exit_reason = 'EOD_NO_CHAIN'
                    t.gross_pnl = None
                    t.net_pnl = None
                    state.closed_multi_leg_trades.append(t)
                    self._notify_closed(t)
                    event_log.append(_log('multi_leg_eod_no_chain', ts, instrument,
                                          strategy=t.strategy_name,
                                          structure_type=t.structure_type,
                                          trade_id=t.trade_id,
                                          severity='warning',
                                          note='Chain snapshot unavailable at EOD; trade closed with no P&L. '
                                                 'Verify chain-feed coverage at session-end timestamp.'))

            state.open_multi_leg_trades = []

        return event_log


def _log(event_type: str, timestamp, instrument: str, **kwargs) -> Dict:
    """Builds a structured log entry. Shared by all runtimes."""
    return {'event_type': event_type, 'timestamp': timestamp, 'instrument': instrument, **kwargs}
