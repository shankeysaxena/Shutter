"""
ReplayRuntime — feeds historical bars one at a time through the shared BarEngine.

Purpose:
- Validate the event-driven engine using known historical data
- Produce structured event logs for debugging
- Serve as the bridge between backtest and live paper trading
"""
from typing import List, Dict, Tuple, Optional
from datetime import date

from src.core.models import EngineState, Trade
from src.core.option_models import MultiLegTrade
from src.core.engine import BarEngine
from src.feeds.replay_feed import ReplayFeed
from src.feeds.option_chain_snapshot import OptionChainFeed
from src.strategies.base import BaseStrategy
from src.execution.simulator import BacktestSimulator
from src.execution.multi_leg_simulator import MultiLegSimulator


class ReplayRuntime:
    """
    Processes BarEvents from a ReplayFeed through the shared BarEngine.
    Identical engine logic to BacktestRuntime — only the bar source differs.
    """

    def __init__(
        self,
        strategies: List[BaseStrategy],
        simulator: BacktestSimulator,
        config: Dict,
        multi_leg_simulator: Optional[MultiLegSimulator] = None,
        chain_feed: Optional[OptionChainFeed] = None,
    ):
        self.engine = BarEngine(
            strategies=strategies,
            simulator=simulator,
            config=config,
            multi_leg_simulator=multi_leg_simulator,
            chain_feed=chain_feed,
        )
        self.strategies = strategies
        self.config = config
        self.engine.reset_strategies()  # ensure no state from a prior run bleeds in

    def run(self, feed: ReplayFeed) -> Tuple[List[Trade], List[Dict]]:
        """Run all sessions from the feed. Returns (single_leg_trades, event_log)."""
        sl, _, log = self.run_full(feed)
        return sl, log

    def run_full(
        self, feed: ReplayFeed
    ) -> Tuple[List[Trade], List[MultiLegTrade], List[Dict]]:
        """Run all sessions; returns (single_leg, multi_leg, event_log)."""
        all_trades: List[Trade] = []
        all_multi_leg_trades: List[MultiLegTrade] = []
        event_log: List[Dict] = []

        for session_date, bar_iter in feed.iter_sessions():
            sl, ml, logs = self._run_session(session_date, feed.instrument, bar_iter)
            all_trades.extend(sl)
            all_multi_leg_trades.extend(ml)
            event_log.extend(logs)

        return all_trades, all_multi_leg_trades, event_log

    def _run_session(
        self, session_date: date, instrument: str, bar_iter
    ) -> Tuple[List[Trade], List[MultiLegTrade], List[Dict]]:
        state = EngineState(
            instrument=instrument,
            session_date=session_date,
            open_trades=[],
            closed_trades=[],
            queued_signals=[],
            per_strategy_day_trade_count={s.name: 0 for s in self.strategies},
        )

        event_log: List[Dict] = []
        last_bar_event = None

        for bar_event in bar_iter:
            last_bar_event = bar_event
            event_log.extend(self.engine.process_bar(bar_event, state, instrument))

        if last_bar_event is not None:
            event_log.extend(self.engine.force_eod_exits(last_bar_event, state, instrument))

        return state.closed_trades, state.closed_multi_leg_trades, event_log
