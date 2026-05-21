"""
Phase 6.4 — RiskEngine.

Sits between the strategy/allocator layer and the executor. Every signal
and every order passes through here before being executed.

Principle: the runtime does not need to know the risk rules. It calls
approve_signal() and approve_order() and acts on the boolean result.

Risk checks (in order):
  1. Session halted? (daily loss cap hit, kill switch active)
  2. Max open positions?
  3. Strategy-level daily trade limit?
  4. Strategy-level daily loss cap?
  5. Cooldown after loss streak?

Cooldown design:
  If a strategy hits N consecutive losses, it goes into cooldown for
  `cooldown_minutes`. During cooldown, approve_signal returns False for
  that strategy.

Config (all under risk_engine in base.yaml):
  daily_loss_cap           — session halts if cumulative net P&L drops below this
  per_strategy_loss_cap    — individual strategy halted if its session P&L drops below
  max_open_positions       — total open positions across all strategies
  max_trades_per_session   — hard cap on total fills per session
  cooldown_after_losses    — consecutive losses before cooldown
  cooldown_minutes         — cooldown duration in minutes
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from src.core.models import Signal
from src.core.option_models import MultiLegSignal

logger = logging.getLogger(__name__)


@dataclass
class RiskState:
    """Running risk metrics for one session."""
    session_net_pnl:  float = 0.0
    entries_accepted: int   = 0   # signals approved + orders submitted
    fills_closed:     int   = 0   # completed (exit-filled) trades
    open_positions:   int   = 0   # currently open trade count
    halted:           bool  = False
    halt_reason:      str   = ''

    # Option-specific counters (Phase 3)
    open_option_positions: int   = 0
    option_trades_today:   int   = 0
    option_session_pnl:    float = 0.0

    # Per-strategy trackers
    strategy_pnl:          Dict[str, float]    = field(default_factory=dict)
    strategy_fills:        Dict[str, int]      = field(default_factory=dict)
    # Two separate counters so cooldown resets don't hide session-level decay:
    #   cooldown_streaks: resets on win OR when cooldown fires
    #   kill_streaks:     resets ONLY on win — cooldown never clears it
    cooldown_streaks:      Dict[str, int]      = field(default_factory=dict)
    kill_streaks:          Dict[str, int]      = field(default_factory=dict)
    cooldown_until:        Dict[str, datetime] = field(default_factory=dict)
    # Strategies permanently disabled for the rest of this session
    killed_strategies:     set                 = field(default_factory=set)

    @property
    def total_fills(self) -> int:
        return self.entries_accepted


class RiskEngine:
    """
    Deterministic risk gate. All decisions are rule-based, no ML.

    Designed to be called synchronously from the main event loop:
      approved, reason = engine.approve_signal(signal, risk_state)
    """

    def __init__(self, config: dict):
        rc = config.get('risk_engine', {})
        self.daily_loss_cap        = rc.get('daily_loss_cap', -15_000)
        self.per_strategy_loss_cap = rc.get('per_strategy_loss_cap', -8_000)
        self.max_open_positions    = rc.get('max_open_positions', 4)
        self.max_trades_per_session= rc.get('max_trades_per_session', 20)
        self.cooldown_after_losses = rc.get('cooldown_after_losses', 3)
        self.cooldown_minutes      = rc.get('cooldown_minutes', 30)
        # Hard kill: strategy permanently disabled if consecutive losses hit this
        ks = rc.get('strategy_kill_switch', {})
        self.kill_after_losses     = ks.get('consecutive_losses', 10)

        # Phase 3 — option-specific limits
        self.max_open_option_positions    = rc.get('max_open_option_positions', 1)
        self.max_option_trades_per_session= rc.get('max_option_trades_per_session', 2)
        self.max_daily_option_loss        = rc.get('max_daily_option_loss', -3_000)

    # ----------------------------------------------------------------
    # Public interface — called by LivePaperRuntime
    # ----------------------------------------------------------------

    def approve_signal(
        self,
        signal: Signal,
        state: RiskState,
        now: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """
        Approve or reject a strategy signal before order submission.
        Returns (approved: bool, reason: str).

        NOTE (FIX 3): entries_accepted increments in record_trade_open(), which
        is called only after the entry fill arrives. For PaperExecutor (immediate
        fills) this is fine. For a future BrokerExecutor with async fills, a
        signal could be approved twice before the first fill arrives if the fill
        takes > 1 bar. To close this, the runtime should track pending_entries
        count here. Deferred until BrokerExecutor is implemented.
        """
        now = now or datetime.now()

        if state.halted:
            return False, f'session_halted:{state.halt_reason}'

        # Hard kill switch: strategy permanently disabled for the session
        if signal.strategy_name in state.killed_strategies:
            return False, f'strategy_killed:{signal.strategy_name}'

        if state.open_positions >= self.max_open_positions:
            return False, f'max_positions:{self.max_open_positions}'

        if state.entries_accepted >= self.max_trades_per_session:
            return False, f'max_trades:{self.max_trades_per_session}'

        strat = signal.strategy_name

        # Per-strategy loss cap
        strat_pnl = state.strategy_pnl.get(strat, 0.0)
        if strat_pnl <= self.per_strategy_loss_cap:
            return False, f'strategy_loss_cap:{strat}:{strat_pnl:.0f}'

        # Cooldown check
        cooldown_end = state.cooldown_until.get(strat)
        if cooldown_end and now < cooldown_end:
            remaining = int((cooldown_end - now).total_seconds() / 60)
            return False, f'cooldown:{strat}:{remaining}min_remaining'

        return True, 'approved'

    def approve_order(
        self,
        order_instrument: str,
        state: RiskState,
    ) -> Tuple[bool, str]:
        """Final check at order submission time (after signal approval)."""
        if state.halted:
            return False, f'session_halted:{state.halt_reason}'
        return True, 'approved'

    def should_halt_session(self, state: RiskState) -> Tuple[bool, str]:
        """Check if session-level risk limits have been breached."""
        if state.session_net_pnl <= self.daily_loss_cap:
            return True, f'daily_loss_cap:{state.session_net_pnl:.0f}'
        return False, ''

    # ----------------------------------------------------------------
    # State update — called by the runtime after each fill / trade close
    # ----------------------------------------------------------------

    def record_trade_open(self, state: RiskState) -> None:
        state.open_positions  += 1
        state.entries_accepted += 1

    def record_trade_close(
        self,
        state: RiskState,
        strategy_name: str,
        net_pnl: float,
        now: Optional[datetime] = None,
    ) -> None:
        """Update risk state after a trade closes. Checks for session halt."""
        now = now or datetime.now()
        state.open_positions  = max(0, state.open_positions - 1)
        state.fills_closed   += 1
        state.session_net_pnl += net_pnl

        state.strategy_pnl[strategy_name]   = (
            state.strategy_pnl.get(strategy_name, 0.0) + net_pnl
        )
        state.strategy_fills[strategy_name] = (
            state.strategy_fills.get(strategy_name, 0) + 1
        )

        # Loss streak tracking
        if net_pnl <= 0:
            # Increment both counters on every loss
            c_streak = state.cooldown_streaks.get(strategy_name, 0) + 1
            k_streak = state.kill_streaks.get(strategy_name, 0) + 1
            state.cooldown_streaks[strategy_name] = c_streak
            state.kill_streaks[strategy_name]     = k_streak

            # Kill check first — kill_streaks is never reset by cooldown
            if k_streak >= self.kill_after_losses:
                state.killed_strategies.add(strategy_name)
                state.cooldown_streaks[strategy_name] = 0
                state.kill_streaks[strategy_name]     = 0
                logger.critical(
                    f"STRATEGY KILLED for session: {strategy_name} hit "
                    f"{k_streak} consecutive losses (kill threshold={self.kill_after_losses}). "
                    f"No more signals today."
                )
            elif c_streak >= self.cooldown_after_losses:
                # Temporary cooldown — resets cooldown_streak but NOT kill_streak
                until = now + timedelta(minutes=self.cooldown_minutes)
                state.cooldown_until[strategy_name]   = until
                state.cooldown_streaks[strategy_name] = 0   # ← only this resets
                # kill_streaks deliberately NOT reset here
                logger.warning(
                    f"{strategy_name}: {c_streak} consecutive losses — "
                    f"cooldown until {until.strftime('%H:%M')} "
                    f"(session kill streak: {k_streak}/{self.kill_after_losses})"
                )
        else:
            # Win: reset both counters
            state.cooldown_streaks[strategy_name] = 0
            state.kill_streaks[strategy_name]     = 0

        # Session halt check
        halted, reason = self.should_halt_session(state)
        if halted and not state.halted:
            state.halted      = True
            state.halt_reason = reason
            logger.warning(f"SESSION HALTED: {reason}")

    # ----------------------------------------------------------------
    # Phase 3 — Options risk checks
    # ----------------------------------------------------------------

    def approve_multi_leg_signal(
        self,
        signal: MultiLegSignal,
        state:  RiskState,
        now:    Optional[datetime] = None,
        options_cfg: Optional[dict] = None,
    ) -> Tuple[bool, str]:
        """
        Approve or reject a multi-leg (options) signal.
        Called by LivePaperRuntime before filling a queued MultiLegSignal.
        """
        now = now or datetime.now()
        if state.halted:
            return False, f'session_halted:{state.halt_reason}'

        if state.open_option_positions >= self.max_open_option_positions:
            return False, f'max_option_positions:{self.max_open_option_positions}'

        if state.option_trades_today >= self.max_option_trades_per_session:
            return False, f'max_option_trades:{self.max_option_trades_per_session}'

        if state.option_session_pnl <= self.max_daily_option_loss:
            return False, f'daily_option_loss_cap:{state.option_session_pnl:.0f}'

        # No new entry after cutoff
        if options_cfg:
            long_cfg = options_cfg.get('long_option', {})
            cutoff_str = long_cfg.get('no_new_entry_after', '14:30')
            h, m = map(int, cutoff_str.split(':'))
            from datetime import time as _time
            if now.time() >= _time(h, m):
                return False, f'no_new_entry_after:{cutoff_str}'

        return True, 'approved'

    def record_multi_leg_open(self, state: RiskState) -> None:
        state.open_option_positions += 1
        state.option_trades_today   += 1

    def record_multi_leg_close(self, state: RiskState, net_pnl: float) -> None:
        state.open_option_positions = max(0, state.open_option_positions - 1)
        state.option_session_pnl   += net_pnl
        logger.info(f"Option trade closed: net_pnl=₹{net_pnl:.2f}  "
                    f"session_option_pnl=₹{state.option_session_pnl:.2f}")

    def reset_session(self) -> RiskState:
        """Create a fresh RiskState for a new session."""
        return RiskState()
