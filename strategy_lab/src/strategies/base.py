from abc import ABC, abstractmethod
from typing import Optional, List, Tuple, Union
from src.core.models import Signal, StrategyContext
from src.core.option_models import MultiLegSignal, MultiLegTrade


class BaseStrategy(ABC):
    name: str

    @abstractmethod
    def generate_signal(
        self, ctx: StrategyContext
    ) -> Optional[Union[Signal, MultiLegSignal]]:
        pass

    def explain_no_signal(self, ctx: StrategyContext) -> str:
        """
        Returns a structured reason why generate_signal returned None.
        Override in each strategy to provide specific rejection reasons.
        Default is 'no_signal' for strategies that have not implemented this.
        """
        return 'no_signal'

    def near_miss_metrics(self, ctx: StrategyContext) -> dict:
        """
        Return numeric context showing how close the strategy was to firing.
        Used by the heartbeat telemetry to show proximity to trigger.
        Default: empty dict. Override in each strategy for rich telemetry.

        Example return:
            {'stretch_atr': 1.1, 'threshold_atr': 1.5,
             'pct_to_trigger': 73}  # 73% of the way to threshold
        """
        return {}

    def evaluate_multi_leg_exits(
        self, ctx: StrategyContext
    ) -> List[Tuple[MultiLegTrade, str]]:
        """
        For strategies that manage multi-leg (options) trades, return
        (trade, exit_reason) pairs for any open multi-leg trades that should
        be closed this bar. Single-leg strategies leave this as the default.
        """
        return []

    def reset(self) -> None:
        """
        Clear any internal state accumulated across sessions or runs.
        Call this at the start of each instrument run to prevent state bleed
        when the same strategy instance is reused (e.g. backtest then replay).
        Stateless strategies can leave this as a no-op.
        """
