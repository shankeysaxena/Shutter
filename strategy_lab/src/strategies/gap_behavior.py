"""
Gap Behavior Strategy.

Overnight gaps create directional imbalances.
The strategy trades either continuation of the gap or a fill back to prior close.

Setup:
  - Gap % = (today open - yesterday close) / yesterday close
  - Only trade if abs(gap_pct) >= gap_threshold_pct
  - Observe first 15 min (OR window) before acting

Case A — Gap Continuation (gap up: long, gap down: short):
  Entry:  break above OR_high (gap up) / below OR_low (gap down)
  Stop:   opposite side of OR
  Target: 2R

Case B — Gap Fill (gap up: short back toward prior close, gap down: long):
  Entry:  break below OR_low (gap up) / above OR_high (gap down)
  Stop:   opposite side of OR
  Target: 2R

Max 1 trade per session for this strategy.
"""
import math
from datetime import time
from typing import Optional

from src.core.models import Signal, StrategyContext
from src.core.enums import RejectionReason
from src.strategies.base import BaseStrategy


class GapBehaviorStrategy(BaseStrategy):
    name = 'GAP_BEHAVIOR'

    def generate_signal(self, ctx: StrategyContext) -> Optional[Signal]:
        bar = ctx.bar_event.candle
        features = ctx.bar_event.features
        state = ctx.engine_state
        config = ctx.strategy_config.get('gap_behavior', {})

        if not config.get('enabled', True):
            return None

        no_entry_after = config.get('no_entry_after', '12:30')
        h, m = map(int, no_entry_after.split(':'))
        if bar.timestamp.time() >= time(h, m):
            return None

        # Gap threshold guard
        gap_pct = features.gap_pct
        if gap_pct is None:
            return None
        gap_threshold = config.get('gap_threshold_pct', 0.005)
        if abs(gap_pct) < gap_threshold:
            return None

        # OR must be ready
        if not features.or_ready:
            return None

        or_high = features.or_high
        or_low = features.or_low
        try:
            if or_high is None or or_low is None or math.isnan(or_high) or math.isnan(or_low):
                return None
        except TypeError:
            return None

        # Max 1 trade per session for this strategy
        existing = [t for t in state.closed_trades + state.open_trades
                    if t.strategy_name == self.name]
        queued = [s for s in state.queued_signals if s.strategy_name == self.name]
        if existing or queued:
            return None

        target_r = config.get('target_r', 2.0)

        # Gap up scenarios
        if gap_pct > 0:
            # Case A: continuation — break above OR high
            if bar.close > or_high:
                stop_price = or_low
                risk = bar.close - stop_price
                if risk <= 0:
                    return None
                return Signal(
                    strategy_name=self.name,
                    instrument=bar.instrument,
                    timestamp=bar.timestamp,
                    direction='LONG',
                    entry_type='MARKET',
                    stop_price=stop_price,
                    target_price=0.0,
                    metadata={'target_r': target_r, 'setup': 'gap_continuation',
                              'gap_pct': gap_pct},
                )
            # Case B: fill — break below OR low
            if bar.close < or_low:
                stop_price = or_high
                risk = stop_price - bar.close
                if risk <= 0:
                    return None
                return Signal(
                    strategy_name=self.name,
                    instrument=bar.instrument,
                    timestamp=bar.timestamp,
                    direction='SHORT',
                    entry_type='MARKET',
                    stop_price=stop_price,
                    target_price=0.0,
                    metadata={'target_r': target_r, 'setup': 'gap_fill',
                              'gap_pct': gap_pct},
                )

        # Gap down scenarios
        else:
            # Case A: continuation — break below OR low
            if bar.close < or_low:
                stop_price = or_high
                risk = stop_price - bar.close
                if risk <= 0:
                    return None
                return Signal(
                    strategy_name=self.name,
                    instrument=bar.instrument,
                    timestamp=bar.timestamp,
                    direction='SHORT',
                    entry_type='MARKET',
                    stop_price=stop_price,
                    target_price=0.0,
                    metadata={'target_r': target_r, 'setup': 'gap_continuation',
                              'gap_pct': gap_pct},
                )
            # Case B: fill — break above OR high
            if bar.close > or_high:
                stop_price = or_low
                risk = bar.close - stop_price
                if risk <= 0:
                    return None
                return Signal(
                    strategy_name=self.name,
                    instrument=bar.instrument,
                    timestamp=bar.timestamp,
                    direction='LONG',
                    entry_type='MARKET',
                    stop_price=stop_price,
                    target_price=0.0,
                    metadata={'target_r': target_r, 'setup': 'gap_fill',
                              'gap_pct': gap_pct},
                )

        return None

    def explain_no_signal(self, ctx: StrategyContext) -> str:
        config = ctx.strategy_config.get('gap_behavior', {})
        features = ctx.bar_event.features
        state = ctx.engine_state

        if not config.get('enabled', True):
            return RejectionReason.DISABLED

        no_entry_after = config.get('no_entry_after', '12:30')
        h, m = map(int, no_entry_after.split(':'))
        bar = ctx.bar_event.candle
        if bar.timestamp.time() >= time(h, m):
            return RejectionReason.AFTER_CUTOFF

        gap_pct = features.gap_pct
        if gap_pct is None:
            return RejectionReason.GAP_NOT_AVAILABLE

        gap_threshold = config.get('gap_threshold_pct', 0.005)
        if abs(gap_pct) < gap_threshold:
            return RejectionReason.GAP_BELOW_THRESHOLD

        if not features.or_ready:
            return RejectionReason.OR_NOT_READY

        existing = [t for t in state.closed_trades + state.open_trades
                    if t.strategy_name == self.name]
        queued = [s for s in state.queued_signals if s.strategy_name == self.name]
        if existing or queued:
            return RejectionReason.ALREADY_TRADED

        return RejectionReason.NO_BREAKOUT
