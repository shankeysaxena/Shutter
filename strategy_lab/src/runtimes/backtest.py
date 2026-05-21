import pandas as pd
from typing import List, Dict, Tuple, Optional

from src.core.models import EngineState, Trade
from src.core.option_models import MultiLegTrade
from src.core.utils import row_to_bar_event
from src.core.engine import BarEngine
from src.strategies.base import BaseStrategy
from src.execution.simulator import BacktestSimulator
from src.execution.multi_leg_simulator import MultiLegSimulator
from src.feeds.option_chain_snapshot import OptionChainFeed


class BacktestRuntime:
    """
    Runs the bar-by-bar engine over historical sessions.
    All per-bar logic is delegated to BarEngine.
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

    def run_session(
        self, instrument: str, session_date, session_df: pd.DataFrame
    ) -> List[Trade]:
        """Returns closed (single-leg) trades for the session. Multi-leg trades available via run_session_full."""
        _, trades, _ = self._run_session_inner(instrument, session_date, session_df)
        return trades

    def run_session_with_log(
        self, instrument: str, session_date, session_df: pd.DataFrame
    ) -> Tuple[List[Trade], List[Dict]]:
        """Returns (closed_trades, event_log) — useful for debugging a specific session."""
        log, trades, _ = self._run_session_inner(instrument, session_date, session_df)
        return trades, log

    def run_session_full(
        self, instrument: str, session_date, session_df: pd.DataFrame
    ) -> Tuple[List[Trade], List[MultiLegTrade], List[Dict]]:
        """Returns (single_leg_trades, multi_leg_trades, event_log)."""
        log, sl_trades, ml_trades = self._run_session_inner(instrument, session_date, session_df)
        return sl_trades, ml_trades, log

    def _run_session_inner(self, instrument, session_date, session_df):
        if session_df.empty:
            return [], [], []

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
        for _, row in session_df.iterrows():
            last_bar_event = row_to_bar_event(row, instrument, 'backtest')
            event_log.extend(self.engine.process_bar(last_bar_event, state, instrument))

        if last_bar_event is not None:
            event_log.extend(self.engine.force_eod_exits(last_bar_event, state, instrument))

        return event_log, state.closed_trades, state.closed_multi_leg_trades
