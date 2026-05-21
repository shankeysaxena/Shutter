from datetime import time
from typing import Optional
import math
from src.core.models import Signal, StrategyContext
from src.core.enums import RejectionReason
from src.strategies.base import BaseStrategy

class ORBStrategy(BaseStrategy):
    name = "ORB"

    def generate_signal(self, ctx: StrategyContext) -> Optional[Signal]:
        bar = ctx.bar_event.candle
        features = ctx.bar_event.features
        state = ctx.engine_state
        config = ctx.strategy_config.get('orb', {})

        if not config.get('enabled', True):
            return None

        no_entry_after = config.get('no_entry_after', "12:00")
        hour, minute = map(int, no_entry_after.split(':'))
        cutoff_time = time(hour, minute)

        if bar.timestamp.time() >= cutoff_time:
            return None

        if not features.or_ready:
            return None

        # Fix: NaN values from the dataframe are not None — check for both
        or_high = features.or_high
        or_low = features.or_low
        if or_high is None or or_low is None:
            return None
        try:
            if math.isnan(or_high) or math.isnan(or_low):
                return None
        except TypeError:
            return None

        target_r = config.get('target_r', 2.0)

        # Max 1 trade per direction — includes open/closed trades AND queued signals
        # so a duplicate signal on the same bar cannot slip through before filling
        existing_directions = {t.direction for t in state.closed_trades + state.open_trades
                               if t.strategy_name == self.name}
        existing_directions |= {s.direction for s in state.queued_signals
                                 if s.strategy_name == self.name}

        # Long Breakout
        if bar.close > or_high and "LONG" not in existing_directions:
            stop_price = or_low
            risk = bar.close - stop_price
            if risk <= 0:
                return None
            return Signal(
                strategy_name=self.name,
                instrument=bar.instrument,
                timestamp=bar.timestamp,
                direction="LONG",
                entry_type="MARKET",
                stop_price=stop_price,
                target_price=0.0,  # placeholder; simulator recomputes from actual fill
                metadata={'target_r': target_r, 'or_high': or_high, 'or_low': or_low},
            )

        # Short Breakout
        if bar.close < or_low and "SHORT" not in existing_directions:
            stop_price = or_high
            risk = stop_price - bar.close
            if risk <= 0:
                return None
            return Signal(
                strategy_name=self.name,
                instrument=bar.instrument,
                timestamp=bar.timestamp,
                direction="SHORT",
                entry_type="MARKET",
                stop_price=stop_price,
                target_price=0.0,  # placeholder; simulator recomputes from actual fill
                metadata={'target_r': target_r, 'or_high': or_high, 'or_low': or_low},
            )

        return None

    def explain_no_signal(self, ctx: StrategyContext) -> str:
        """Returns a specific reason why no signal was generated this bar."""
        config = ctx.strategy_config.get('orb', {})
        bar = ctx.bar_event.candle
        features = ctx.bar_event.features
        state = ctx.engine_state

        if not config.get('enabled', True):
            return RejectionReason.DISABLED

        no_entry_after = config.get('no_entry_after', '12:00')
        h, m = map(int, no_entry_after.split(':'))
        if bar.timestamp.time() >= time(h, m):
            return RejectionReason.AFTER_CUTOFF

        if not features.or_ready:
            return RejectionReason.OR_NOT_READY

        or_high = features.or_high
        or_low = features.or_low
        try:
            if or_high is None or or_low is None or math.isnan(or_high) or math.isnan(or_low):
                return RejectionReason.OR_VALUES_INVALID
        except TypeError:
            return RejectionReason.OR_VALUES_INVALID

        existing_directions = {t.direction for t in state.closed_trades + state.open_trades
                               if t.strategy_name == self.name}
        existing_directions |= {s.direction for s in state.queued_signals
                                 if s.strategy_name == self.name}

        if bar.close > or_high and 'LONG' in existing_directions:
            return RejectionReason.LONG_ALREADY_TRADED
        if bar.close < or_low and 'SHORT' in existing_directions:
            return RejectionReason.SHORT_ALREADY_TRADED
        if 'LONG' in existing_directions and 'SHORT' in existing_directions:
            return RejectionReason.BOTH_DIRECTIONS_TRADED

        return RejectionReason.NO_BREAKOUT
