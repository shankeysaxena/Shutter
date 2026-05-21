"""
Phase 6.5 (patched 6.2A) — LivePaperRuntime.

Orchestrates the full live trading loop:

  Feed (WebSocket ticks)
    ↓ on_tick()
  LiveBarBuilder (tick → 1-min bar)
    ↓ completed bar
  Feature pipeline (VWAPFeature, ORFeature, etc.)
    ↓
  AllocationGatedStrategies (via BarEngine.process_bar)
    ↓ Signal emitted
  RiskEngine.approve_signal + approve_order  ← both gates now used
    ↓ approved
  Executor.submit_order (PaperExecutor or future BrokerExecutor)
    ↓ entry fill received via get_fills()
  LiveTradeManager.on_entry_fill → Trade created in engine_state
    ↓ each subsequent bar
  LiveTradeManager.check_exits → stop/target hit detection
    ↓ exit order submitted
  exit fill received
  LiveTradeManager._close_trade → P&L computed
    ↓
  RiskEngine.record_trade_close → daily cap / cooldown logic

Phase 6.2A fixes applied:
  [1] LiveTradeManager replaces _NoOpSimulator — exit lifecycle complete
  [2] _running set to True before feed.start() (race condition fix)
  [3] approve_order() called before each order submission
  [4] Risk state split: entries_accepted / fills_closed tracked separately
  [5] Late/out-of-order tick handling in _on_tick guarded
  [6] Session flush via LiveTradeManager at 15:29
  [7] Pending entry fills tracked by order_id for fill matching
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import date, datetime, time
from typing import Dict, List, Optional

import pandas as pd

from src.core.engine import BarEngine
from src.core.models import EngineState, Signal
from src.core.utils import row_to_bar_event
from src.execution.executor import Executor, OrderRequest
from src.execution.paper_executor import PaperExecutor
from src.features.atr import IntradayATRFeature
from src.features.gap import GapFeature
from src.features.intraday import IntradaySessionFeature
from src.features.opening_range import OpeningRangeFeature
from src.features.vwap import VWAPFeature
from src.live.bar_builder import LiveBar, LiveBarBuilder, Tick
from src.live.feeds import MarketDataFeed
from src.live.risk_engine import RiskEngine, RiskState
from src.live.notifications import NotificationService, INFO, WARNING, CRITICAL
from src.live.session_monitor import SessionHealthMonitor
from src.live.shadow_evaluator import ShadowEvaluator
from src.live.trade_manager import LiveTradeManager
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

_SESSION_OPEN  = time(9, 15)
_SESSION_CLOSE = time(15, 30)
_EOD_FLUSH_AT  = time(15, 29)


class LivePaperRuntime:
    """
    Live (or paper) intraday runtime. Executor-agnostic — does not know if
    it is paper or real. PaperExecutor vs BrokerExecutor is purely a
    constructor choice.
    """

    def __init__(
        self,
        strategies:  List[BaseStrategy],
        executor:    Executor,
        risk_engine: RiskEngine,
        feed:        MarketDataFeed,
        bar_builder: LiveBarBuilder,
        config:      dict,
        instruments: Optional[List[str]] = None,
        monitor:           Optional[SessionHealthMonitor] = None,
        shadow_evaluators: Optional[List[ShadowEvaluator]] = None,
        notifier:          Optional[NotificationService]   = None,
    ):
        self.strategies  = strategies
        self.executor    = executor
        self.risk_engine = risk_engine
        self.feed        = feed
        self.bar_builder = bar_builder
        self.config      = config
        self.instruments = instruments or config.get('instruments', ['NIFTY'])
        self.monitor           = monitor
        self.shadow_evaluators = shadow_evaluators or []
        self.notifier          = notifier or NotificationService()
        self._auto_shutdown    = False
        self._last_bar_time:   Optional[datetime] = None
        self._last_heartbeat:  Optional[datetime] = None
        self._heartbeat_mins   = 30   # Telegram heartbeat interval
        # Force-close open trades and stop if no bars arrive for this many minutes
        self._max_stale_minutes = config.get('live', {}).get('max_stale_minutes', 10)

        lot_sizes = config.get('risk', {}).get('lot_size', {})
        brokerage = config.get('costs', {}).get('brokerage_per_trade', 20.0)
        self._brokerage   = brokerage
        self._trade_manager = LiveTradeManager(executor, risk_engine, lot_sizes)

        # Pending entry orders: {order_id → (Signal, strategy_name)}
        self._pending_entries: Dict[str, tuple] = {}

        self._engine_states: Dict[str, EngineState] = {}
        self._risk_state:    RiskState = risk_engine.reset_session()
        self._current_date:  Optional[date] = None
        self._session_rows:  Dict[str, list] = {}
        self._eod_flushed:   bool = False

        # Feature pipeline
        or_cfg = config.get('strategies', {}).get('orb', {})
        h1, m1 = map(int, or_cfg.get('opening_range_start', '09:15').split(':'))
        h2, m2 = map(int, or_cfg.get('opening_range_end',   '09:30').split(':'))
        self._features = [
            VWAPFeature(),
            OpeningRangeFeature(start_time=time(h1, m1), end_time=time(h2, m2)),
            GapFeature(),
            IntradaySessionFeature(),
            IntradayATRFeature(period=14),
        ]

        # BarEngine uses SignalOnlySimulator — it NEVER creates trades or closes
        # exits. Signal generation (step 3) is all we need from BarEngine.
        # Trade lifecycle (entry fill, exit detection, P&L) is owned exclusively
        # by LiveTradeManager + Executor so risk accounting stays consistent.
        self._engine = BarEngine(
            strategies=strategies,
            simulator=_SignalOnlySimulator(),
            config=config,
        )

        self._running = False
        self._lock    = threading.Lock()

    # ----------------------------------------------------------------
    # Start / stop
    # ----------------------------------------------------------------

    def start(self) -> None:
        """FIX #2: _running = True BEFORE feed.start() to avoid dropped first ticks."""
        self._running = True
        self.feed.set_tick_callback(self._on_tick)
        self.feed.subscribe(self.instruments)
        self.feed.start()
        logger.info(f"LivePaperRuntime started. Instruments: {self.instruments}")
        self.notifier.send(
            f"Session started\nPolicy: `{self.config.get('experiment_name','live-paper')}`\n"
            f"Instruments: {self.instruments}",
            INFO,
        )

    def stop(self) -> None:
        self.feed.stop()
        self._running = False
        logger.info("LivePaperRuntime stopped.")

    # ----------------------------------------------------------------
    # Tick ingestion (feed thread → bar builder)
    # ----------------------------------------------------------------

    def _on_tick(self, tick: Tick) -> None:
        if not self._running:
            return

        # FIX #5: guard late/dropped ticks outside market hours
        t = tick.timestamp.time()
        if t < _SESSION_OPEN or t > _SESSION_CLOSE:
            return

        tick_date = tick.timestamp.date()
        if tick_date != self._current_date:
            self._reset_session(tick_date)

        # FIX 3: notify monitor on every tick
        if self.monitor:
            self.monitor.on_tick(tick.instrument, tick.timestamp)

        self.bar_builder.on_tick(tick)

        if isinstance(self.executor, PaperExecutor):
            self.executor.update_market_price(tick.instrument, tick.last_price)

        completed = self.bar_builder.get_completed_bars(tick.instrument)
        for bar in completed:
            self._process_bar(bar)

    # ----------------------------------------------------------------
    # Bar processing (per completed bar)
    # ----------------------------------------------------------------

    def _process_bar(self, bar: LiveBar) -> None:
        if bar.timestamp.time() > _SESSION_CLOSE:
            return

        inst  = bar.instrument
        if inst not in self._engine_states:
            return
        state = self._engine_states[inst]

        # EOD flush — force-close all open positions once at 15:29
        if bar.timestamp.time() >= _EOD_FLUSH_AT and not self._eod_flushed:
            self._eod_flushed = True
            self._trade_manager.flush_session(bar, state, self._risk_state, self._brokerage)


        # Accumulate bar row and recompute features
        self._session_rows[inst].append({
            'timestamp': bar.timestamp,
            'instrument': inst,
            'open': bar.open, 'high': bar.high, 'low': bar.low,
            'close': bar.close, 'volume': bar.volume,
        })
        sess_df = pd.DataFrame(self._session_rows[inst])
        for feat in self._features:
            sess_df = feat.calculate(sess_df)
        sess_df['session_date'] = bar.timestamp.date()

        bar_event = row_to_bar_event(sess_df.iloc[-1], inst, 'live_paper')

        if self.monitor:
            self.monitor.on_bar_completed(bar.instrument, bar.timestamp)

        self._last_bar_time = bar.timestamp

        # 30-minute heartbeat
        now = bar.timestamp
        if (self._last_heartbeat is None or
                (now - self._last_heartbeat).total_seconds() >= self._heartbeat_mins * 60):
            self._last_heartbeat = now
            self._send_heartbeat(bar_event)

        # Heartbeat: log every bar so you can see the system is alive
        logger.info(
            f"Bar  {bar.instrument} {bar.timestamp.strftime('%H:%M')} "
            f"O={bar.open:.0f} H={bar.high:.0f} L={bar.low:.0f} C={bar.close:.0f}"
        )

        # Feed the same bar to every shadow evaluator (isolated state each)
        for shadow in self.shadow_evaluators:
            shadow.process_bar(bar_event, bar.instrument)

        # Check exits for open trades BEFORE generating new signals
        self._trade_manager.check_exits(bar, state, self._risk_state, self._brokerage)

        # Generate signals via engine (allocator + strategy logic)
        self._engine.process_bar(bar_event, state, inst)

        # Check if session was halted mid-bar and notify once
        if self._risk_state.halted and not getattr(self, '_halt_notified', False):
            self._halt_notified = True
            self.notifier.send(
                f"🛑 *SESSION HALTED*\n{self._risk_state.halt_reason}", CRITICAL
            )

        # Submit any new signals that cleared the risk engine
        self._submit_pending_signals(bar_event)

        # Poll executor fills → close trades (FIX #3: no state arg)
        self._process_fills()

    # ----------------------------------------------------------------
    # Signal → order pipeline
    # ----------------------------------------------------------------

    def _submit_pending_signals(self, bar_event) -> None:
        inst  = bar_event.candle.instrument
        state = self._engine_states.get(inst)
        if state is None:
            return

        to_submit = list(state.queued_signals)
        state.queued_signals = []

        for signal in to_submit:
            # FIX #3: approve_signal counts accepted entries
            approved, reason = self.risk_engine.approve_signal(
                signal, self._risk_state
            )
            if not approved:
                logger.info(f"Signal rejected ({reason}): {signal.strategy_name}")
                continue

            qty = self.config.get('risk', {}).get('lot_size', {}).get(inst, 1)
            order_id = str(uuid.uuid4())
            # Capture regime at signal time for Stage B daily report
            regime_at_entry = 'unknown'
            for strat in self.strategies:
                if hasattr(strat, '_detector'):
                    s = strat._detector._session_cache.get((inst, bar_event.candle.timestamp.date()))
                    if s:
                        regime_at_entry = s.regime + ('+compression' if s.compression_detected else '')
                    break

            req = OrderRequest(
                order_id         = order_id,
                instrument       = inst,
                direction        = 'BUY' if signal.direction == 'LONG' else 'SELL',
                order_type       = 'MARKET',
                quantity         = qty,
                price            = None,
                strategy_name    = signal.strategy_name,
                signal_timestamp = signal.timestamp,
                metadata         = {
                    'stop_price':      signal.stop_price,
                    'target_price':    signal.target_price,
                    'regime_at_entry': regime_at_entry,
                    **signal.metadata,
                },
            )

            # FIX #4: approve_order is the second gate (e.g. exposure check)
            order_approved, order_reason = self.risk_engine.approve_order(
                inst, self._risk_state
            )
            if not order_approved:
                logger.info(f"Order rejected ({order_reason}): {signal.strategy_name}")
                continue

            self.executor.submit_order(req)
            self._pending_entries[order_id] = (signal, signal.strategy_name)
            logger.info(
                f"Order submitted: {signal.strategy_name} {signal.direction} "
                f"{inst} qty={qty} order_id={order_id[:8]}"
            )
            target_text = f"{signal.target_price:.0f}" if signal.target_price else "VWAP"
            self.notifier.send(
                f"📤 *Signal* `{signal.strategy_name}` {signal.direction} `{inst}`\n"
                f"Stop: {signal.stop_price:.0f}  Target: {target_text}",
                INFO,
            )

    # ----------------------------------------------------------------
    # Fill processing
    # ----------------------------------------------------------------

    def _process_fills(self) -> None:
        """
        FIX #3: route each fill to the correct instrument's EngineState.
        get_fills() returns global fills; do NOT pass a single state.
        FIX #5: terminal (REJECTED/CANCELLED) orders are purged from pending.
        """
        fills = self.executor.get_fills()
        entry_fills = []
        exit_fills  = []

        for fill in fills:
            if fill.order_id in self._pending_entries:
                # FIX #5: if rejected/cancelled, remove from pending silently
                if fill.status in ('REJECTED', 'CANCELLED'):
                    self._pending_entries.pop(fill.order_id, None)
                    logger.warning(
                        f"Entry order {fill.status}: "
                        f"{fill.order_id[:8]} — {fill.message}"
                    )
                    continue
                if fill.is_filled:
                    entry_fills.append(fill)
            else:
                exit_fills.append(fill)

        for fill in entry_fills:
            state = self._engine_states.get(fill.instrument)
            if state is None:
                logger.warning(f"Fill for unknown instrument: {fill.instrument}")
                self._pending_entries.pop(fill.order_id, None)
                continue
            signal, strategy_name = self._pending_entries.pop(fill.order_id)
            self._trade_manager.on_entry_fill(
                fill           = fill,
                signal_stop    = signal.stop_price,
                signal_target  = signal.target_price if signal.target_price != 0.0
                                 else fill.fill_price * 1.02,
                strategy_name  = strategy_name,
                state          = state,
                risk_state     = self._risk_state,
            )
            self.notifier.send(
                f"✅ *Entry filled* `{strategy_name}` {fill.direction} `{fill.instrument}`\n"
                f"Fill: ₹{fill.fill_price:.0f}  Qty: {fill.filled_qty}",
                INFO,
            )

        for fill in exit_fills:
            state = self._engine_states.get(fill.instrument)
            if state is None:
                continue
            closed_before = len(state.closed_trades)
            self._trade_manager.on_fills([fill], state, self._risk_state, self._brokerage)
            # Notify on newly closed trades
            new_trades = state.closed_trades[closed_before:]
            for t in new_trades:
                pnl_str = f"₹{t.net_pnl:+,.0f}" if t.net_pnl is not None else '—'
                level   = WARNING if (t.net_pnl or 0) < 0 else INFO
                if t.exit_reason in ('STOP',):
                    level = WARNING
                self.notifier.send(
                    f"{'🔴' if level==WARNING else '🟢'} *Exit* `{t.strategy_name}` "
                    f"`{t.exit_reason}` `{t.instrument}`\n"
                    f"P&L: {pnl_str}  R: {f'{t.r_multiple:.2f}' if t.r_multiple else '—'}",
                    level,
                )

    # ----------------------------------------------------------------
    # Session reset
    # ----------------------------------------------------------------

    def _reset_session(self, new_date: date) -> None:
        logger.info(f"Session reset for {new_date}")
        self._current_date = new_date
        self._eod_flushed  = False
        self._risk_state   = self.risk_engine.reset_session()
        self._engine.reset_strategies()
        self.executor.reset_session()
        self._trade_manager.reset_session()
        self.bar_builder.reset()
        if self.monitor:
            self.monitor.reset_session()
        for shadow in self.shadow_evaluators:
            shadow.reset_session(new_date)
        self._pending_entries.clear()
        self._session_rows = {inst: [] for inst in self.instruments}
        self._engine_states = {
            inst: EngineState(
                instrument=inst,
                session_date=new_date,
                open_trades=[], closed_trades=[], queued_signals=[],
                per_strategy_day_trade_count={s.name: 0 for s in self.strategies},
            )
            for inst in self.instruments
        }

    # ----------------------------------------------------------------
    # Status / introspection
    # ----------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    def get_risk_state(self) -> RiskState:
        return self._risk_state

    def get_positions(self) -> dict:
        return self.executor.get_positions()

    def _send_heartbeat(self, bar_event) -> None:
        """Send 30-min Telegram heartbeat with market state + strategy near-miss."""
        from src.strategies.allocator import AllocationGatedStrategy
        inst = bar_event.candle.instrument
        ts   = bar_event.candle.timestamp.strftime('%H:%M')

        # Market state
        detector = None
        for s in self.strategies:
            if isinstance(s, AllocationGatedStrategy) and hasattr(s, '_detector'):
                detector = s._detector
                break

        state_str = ''
        for instrument in self.instruments:
            key = (instrument, bar_event.features.session_date)
            sess_state = detector._session_cache.get(key) if detector else None
            if sess_state:
                comp = '+compression' if sess_state.compression_detected else ''
                feats = self._session_rows.get(instrument)
                vwap_d = bar_event.features.vwap_distance or 0
                atr    = bar_event.features.intraday_atr or 0
                vwap_atr = abs(vwap_d * bar_event.candle.close / atr) if atr > 0 else 0
                or_w   = round((sess_state.or_width_pct or 0) * 100, 2)
                state_str += (f"\n`{instrument}`: {sess_state.regime}{comp}"
                              f" | OR {or_w}% | VWAP {vwap_atr:.1f} ATR")

        # Strategy evaluations with near-miss
        strategy_str = ''
        ctx_temp = None
        try:
            from src.core.models import StrategyContext
            state = self._engine_states.get(inst)
            if state:
                ctx_temp = type('C', (), {
                    'bar_event': bar_event,
                    'engine_state': state,
                    'strategy_config': self.config.get('strategies', {}),
                    'chain_snapshot': None,
                })()
        except Exception:
            pass

        for s in self.strategies:
            inner = getattr(s, '_strategy', s)
            name  = inner.name if hasattr(inner, 'name') else s.name
            if ctx_temp:
                try:
                    reason  = inner.explain_no_signal(ctx_temp)
                    metrics = inner.near_miss_metrics(ctx_temp) if hasattr(inner, 'near_miss_metrics') else {}
                    pct     = metrics.get('pct_to_trigger')
                    bar_str = ''
                    if 'stretch_atr' in metrics:
                        bar_str = f" {metrics['stretch_atr']}/{metrics['threshold_atr']} ATR"
                    elif 'trend_bars' in metrics:
                        bar_str = f" {metrics['trend_bars']}/{metrics['trend_bars_needed']} bars"
                    elif 'gap_abs_pct' in metrics:
                        bar_str = f" gap {metrics['gap_abs_pct']}%/{metrics['min_gap_pct']}%"
                    pct_str = f" ({pct}%)" if pct is not None else ''
                    strategy_str += f"\n`{name}`: {reason}{bar_str}{pct_str}"
                except Exception:
                    strategy_str += f"\n`{name}`: —"

        # System health
        rs  = self._risk_state
        pos = sum(abs(v) for v in self.executor.get_positions().values())
        health_icon = '✅' if (self.monitor and self.monitor.is_healthy()) else '⚠️'

        msg = (
            f"🕐 *{ts} HEARTBEAT*"
            f"{state_str}"
            f"\n\n*STRATEGIES*{strategy_str}"
            f"\n\n*SYSTEM*"
            f"\n{health_icon} Feed  |  Pos: {pos}  |  P&L: ₹{rs.session_net_pnl:,.0f}"
            f"  |  Trades: {rs.fills_closed}/{self.config.get('risk_engine',{}).get('max_trades_per_session',3)}"
        )
        self.notifier.send(msg, INFO)

    def trigger_eod_shutdown(self) -> None:
        """Called by CLI wall-clock loop at 15:35. Not bar-dependent."""
        if self._auto_shutdown:
            return
        self._auto_shutdown = True
        logger.info("EOD auto-shutdown triggered at 15:35")
        self._send_session_summary()
        self.stop()

    # ----------------------------------------------------------------
    # Session summary + daily report
    # ----------------------------------------------------------------

    def _send_session_summary(self) -> None:
        """Send EOD summary via Telegram and save to disk."""
        rs   = self._risk_state
        date = self._current_date or 'unknown'

        all_trades = []
        for st in self._engine_states.values():
            all_trades.extend(st.closed_trades)

        n       = len(all_trades)
        pnl     = sum(t.net_pnl for t in all_trades if t.net_pnl)
        wins    = sum(1 for t in all_trades if (t.net_pnl or 0) > 0)
        wr      = f"{wins/n:.0%}" if n else '—'
        halted  = '🛑 HALTED' if rs.halted else '✅ Clean'

        msg = (
            f"📊 *Session Summary* {date}\n"
            f"Trades: {n}  WR: {wr}  P&L: ₹{pnl:,.0f}\n"
            f"Status: {halted}\n"
            f"Entries accepted: {rs.entries_accepted}"
        )
        if rs.halted:
            msg += f"\nHalt reason: `{rs.halt_reason}`"

        self.notifier.send(msg, CRITICAL if rs.halted else INFO)
        self._save_daily_report(date, all_trades, rs)

    def _save_daily_report(self, session_date, trades, rs) -> None:
        """Save Stage B daily JSON report to runs/live_paper/<date>.json"""
        import json
        from collections import defaultdict
        from pathlib import Path

        out_dir = Path('runs') / 'live_paper'
        out_dir.mkdir(parents=True, exist_ok=True)

        # Per-strategy breakdown
        by_strat: dict = defaultdict(lambda: {'n': 0, 'pnl': 0.0, 'wins': 0})
        for t in trades:
            s = by_strat[t.strategy_name]
            s['n']   += 1
            s['pnl'] += t.net_pnl or 0
            if (t.net_pnl or 0) > 0:
                s['wins'] += 1
        by_strat_out = {
            k: {'trades': v['n'], 'net_pnl': round(v['pnl'], 2),
                'win_rate': round(v['wins'] / v['n'], 3) if v['n'] else None}
            for k, v in by_strat.items()
        }

        # Cap usage
        cap_usage = {
            'entries_accepted':    rs.entries_accepted,
            'fills_closed':        rs.fills_closed,
            'open_positions':      rs.open_positions,
            'max_trades_session':  self.config.get('risk_engine', {}).get('max_trades_per_session', 20),
            'daily_loss_cap':      self.config.get('risk_engine', {}).get('daily_loss_cap', -99999),
            'session_net_pnl':     round(rs.session_net_pnl, 2),
            'cap_used_pct':        round(abs(rs.session_net_pnl) /
                                          abs(self.config.get('risk_engine', {}).get('daily_loss_cap', -1) or -1)
                                          * 100, 1),
        }

        # Cooldown + kill events
        risk_events = []
        for strat, until in (rs.cooldown_until or {}).items():
            risk_events.append({'type': 'cooldown', 'strategy': strat, 'until': str(until)})
        for strat in (rs.killed_strategies or set()):
            risk_events.append({'type': 'killed', 'strategy': strat})

        report = {
            'session_date':    str(session_date),
            'experiment_name': self.config.get('experiment_name', 'live-paper'),
            'instruments':     self.instruments,
            'policy':          self.config.get('policy', 'unknown'),
            'total_trades':    len(trades),
            'total_net_pnl':   round(sum(t.net_pnl or 0 for t in trades), 2),
            'win_rate':        round(sum(1 for t in trades if (t.net_pnl or 0) > 0) / len(trades), 4) if trades else None,
            'halted':          rs.halted,
            'halt_reason':     rs.halt_reason,
            'by_strategy':     by_strat_out,
            'cap_usage':       cap_usage,
            'risk_events':     risk_events,
            'trades': [
                {
                    'strategy':        t.strategy_name,
                    'instrument':      t.instrument,
                    'direction':       t.direction,
                    'entry_time':      str(t.entry_time),
                    'entry_price':     t.entry_price,
                    'exit_time':       str(t.exit_time),
                    'exit_price':      t.exit_price,
                    'exit_reason':     t.exit_reason,
                    'net_pnl':         t.net_pnl,
                    'r_multiple':      t.r_multiple,
                    'regime_at_entry': t.metadata.get('regime_at_entry', 'unknown'),
                    'bar_vs_fill_slippage': t.metadata.get('bar_vs_fill_slippage'),
                }
                for t in trades
            ],
        }
        path = out_dir / f"{session_date}.json"
        path.write_text(json.dumps(report, indent=2, default=str))
        logger.info(f"Daily report saved → {path}")


class _SignalOnlySimulator:
    """
    Simulator that intentionally does nothing except satisfy BarEngine's interface.

    BarEngine calls this in two places:
      - process_signal_for_entry() during step 1 (fill queued signals)
      - check_exits() during step 2 (stop/target/EOD checks)

    Both return inert values so the engine never touches open_trades.
    All trade lifecycle — entry fill → stop/target exit → P&L → risk accounting
    — is exclusively owned by LiveTradeManager + Executor. This prevents silent
    P&L drift and double-counting that would occur if BacktestSimulator ran
    concurrently with LiveTradeManager.
    """
    def process_signal_for_entry(self, *a, **kw) -> None:
        return None   # signal consumed, no Trade added to engine_state

    def check_exits(self, *a, **kw) -> bool:
        return False  # engine never closes trades in live mode
