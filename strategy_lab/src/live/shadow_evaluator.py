"""
Phase 6 — Shadow Evaluator.

Runs one allocation policy against live bars with fully isolated state —
own BarEngine, StrategyState, BacktestSimulator, RiskEngine, trade ledger —
but shares only the live bar feed with the primary executor.

Only one policy (the primary) submits paper orders.
Shadow evaluators measure what WOULD have happened without executing.

Why:
  Multiple executing strategies in the same session create ambiguity.
  Shadow mode gives fast multi-policy learning at zero added operational risk.

Promotion path:
  Week 1:   primary=VWAP_PULLBACK_ONLY  shadow=[deterministic_no_orb, all_on]
  Week 2+:  real capital on primary; paper on primary; shadow on allocator
  Week 10+: promote allocator to paper-executed if shadow consistently beats primary

Usage:
    shadow = ShadowEvaluator(
        name='deterministic_no_orb',
        strategies=wrap_strategies(build_strategies(config), 'deterministic_no_orb'),
        config=config,
    )
    # In live session loop:
    shadow.process_bar(bar_event, instrument)
    # At session end:
    report = shadow.session_summary()
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Dict, List, Optional

from src.core.engine import BarEngine
from src.core.models import EngineState, Trade
from src.execution.simulator import BacktestSimulator
from src.live.risk_engine import RiskEngine, RiskState
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class ShadowEvaluator:
    """
    Lightweight shadow portfolio that processes live bars in isolation.
    Uses BacktestSimulator fills (bar-open price) — fast and deterministic.
    """

    def __init__(
        self,
        name:       str,
        strategies: List[BaseStrategy],
        config:     dict,
    ):
        self.name       = name
        self.strategies = strategies
        self.config     = config

        costs = config.get('costs', {})
        self._simulator = BacktestSimulator(
            slippage_per_side=costs.get('slippage_per_side', 2.0),
            brokerage=costs.get('brokerage_per_trade', 20.0),
        )
        self._engine = BarEngine(
            strategies=strategies,
            simulator=self._simulator,
            config=config,
        )
        self._risk_engine    = RiskEngine(config)
        self._risk_state:    RiskState = self._risk_engine.reset_session()
        self._engine_states: Dict[str, EngineState] = {}
        self._current_date:  Optional[date] = None
        self._session_rows:  Dict[str, list] = []

    def process_bar(self, bar_event, instrument: str) -> None:
        """Feed one completed live bar. Mirrors BacktestRuntime.run_session."""
        bar_date = bar_event.candle.timestamp.date()
        if bar_date != self._current_date:
            self._reset_session(bar_date)

        state = self._engine_states.get(instrument)
        if state is None:
            return

        self._engine.process_bar(bar_event, state, instrument)

    def session_summary(self) -> dict:
        """Closed trades + P&L for the current session, by strategy."""
        all_trades: List[Trade] = []
        for state in self._engine_states.values():
            all_trades.extend(state.closed_trades)
            # Include open trades at session mark-to-market (approximate)
            all_trades.extend(state.open_trades)

        if not all_trades:
            return {
                'shadow': self.name, 'n_trades': 0, 'total_pnl': 0.0,
                'win_rate': None, 'by_strategy': {},
            }

        pnl     = [t.net_pnl for t in all_trades if t.net_pnl is not None]
        wins    = [p for p in pnl if p > 0]
        by_strat: dict = {}
        for t in all_trades:
            if t.net_pnl is None:
                continue
            by_strat.setdefault(t.strategy_name, {'n': 0, 'pnl': 0.0})
            by_strat[t.strategy_name]['n']   += 1
            by_strat[t.strategy_name]['pnl'] += t.net_pnl

        return {
            'shadow':     self.name,
            'n_trades':   len(pnl),
            'total_pnl':  round(sum(pnl), 2),
            'win_rate':   round(len(wins) / len(pnl), 3) if pnl else None,
            'by_strategy': by_strat,
        }

    def reset_session(self, session_date: Optional[date] = None) -> None:
        self._reset_session(session_date or date.today())

    def _reset_session(self, new_date: date) -> None:
        self._current_date = new_date
        self._risk_state   = self._risk_engine.reset_session()
        self._engine.reset_strategies()
        self._engine_states = {
            inst: EngineState(
                instrument=inst,
                session_date=new_date,
                open_trades=[], closed_trades=[], queued_signals=[],
                per_strategy_day_trade_count={s.name: 0 for s in self.strategies},
            )
            for inst in (self.config.get('instruments') or ['NIFTY'])
        }


def build_shadow_evaluators(config: dict, shadow_policies: List[str]) -> List[ShadowEvaluator]:
    """
    Build one ShadowEvaluator per policy name using the same config.
    Each evaluator has completely isolated state.
    """
    from src.backtest.experiment import _build_strategies
    from src.strategies.allocator import wrap_strategies

    evaluators = []
    for policy in shadow_policies:
        strategies = wrap_strategies(_build_strategies(config), policy)
        evaluators.append(ShadowEvaluator(
            name=policy,
            strategies=strategies,
            config=config,
        ))
        logger.info(f"Shadow evaluator created: {policy}")
    return evaluators
