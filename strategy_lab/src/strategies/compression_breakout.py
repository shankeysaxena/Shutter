"""
Compression Breakout Strategy — v2 (architectural fix).

v1 problem: double-detection — strategy checked compression independently from
allocator, causing zero trades (incompatible thresholds + timing).

v2 fix: allocator detects compression once → stores comp_high/comp_low in
SessionState → passes via ctx.market_state. Strategy only watches for breakout.

  MarketStateDetector   → detects compression, stores comp_high/comp_low
  AllocationGatedStrategy → grants access when compression_detected=True,
                            passes SessionState via ctx.market_state
  CompressionBreakoutStrategy → reads range, watches for breakout only
"""
from dataclasses import dataclass, field
from datetime import time
from typing import Dict, Optional, Set, Tuple

from src.core.enums import RejectionReason
from src.core.models import Signal, StrategyContext
from src.strategies.base import BaseStrategy


@dataclass
class _CompressionState:
    phase: str = 'IDLE'           # IDLE | WATCHING | USED
    direction_used: Set[str] = field(default_factory=set)


class CompressionBreakoutStrategy(BaseStrategy):
    """Breakout from allocator-detected intraday compression range."""

    name = 'COMPRESSION_BREAKOUT'

    def __init__(self):
        self._states: Dict[Tuple, _CompressionState] = {}

    def reset(self) -> None:
        self._states.clear()

    def _state(self, instrument: str, session_date) -> _CompressionState:
        key = (instrument, session_date)
        if key not in self._states:
            self._states[key] = _CompressionState()
        return self._states[key]

    def generate_signal(self, ctx: StrategyContext) -> Optional[Signal]:
        cfg = ctx.strategy_config.get('compression_breakout', {})
        if not cfg.get('enabled', False):
            return None

        bar      = ctx.bar_event.candle
        features = ctx.bar_event.features
        state_e  = ctx.engine_state

        # Time gates
        h1, m1 = map(int, cfg.get('entry_start', '10:00').split(':'))
        h2, m2 = map(int, cfg.get('no_entry_after', '15:00').split(':'))
        if bar.timestamp.time() < time(h1, m1) or bar.timestamp.time() >= time(h2, m2):
            return None

        atr = features.intraday_atr
        if not atr or atr <= 0:
            return None
        min_atr = cfg.get('min_atr', 0.0)
        if atr < min_atr:
            return None

        # Read compression range from allocator — no re-detection here
        market_state = getattr(ctx, 'market_state', None)
        if not market_state or not market_state.compression_detected:
            return None
        comp_high = market_state.comp_high
        comp_low  = market_state.comp_low
        if comp_high is None or comp_low is None:
            return None

        s = self._state(bar.instrument, features.session_date)

        # Daily cap
        filled = state_e.per_strategy_day_trade_count.get(self.name, 0)
        queued = sum(1 for sig in state_e.queued_signals if sig.strategy_name == self.name)
        if filled + queued >= cfg.get('max_trades_per_day', 1):
            return None

        if s.phase == 'IDLE':
            s.phase = 'WATCHING'
        if s.phase != 'WATCHING':
            return None

        comp_range = comp_high - comp_low
        if comp_range <= 0:
            return None

        # Breakout detection from allocator's range
        # Cap overshoot: skip if bar has already run too far past the breakout level
        # (large overshoot wrecks R:R when stop is anchored at the opposite end of range)
        max_overshoot = comp_range * cfg.get('max_overshoot_ratio', 0.5)
        direction = None
        if (bar.close > comp_high
                and bar.close <= comp_high + max_overshoot
                and 'LONG' not in s.direction_used):
            direction = 'LONG'
        elif (bar.close < comp_low
                and bar.close >= comp_low - max_overshoot
                and 'SHORT' not in s.direction_used):
            direction = 'SHORT'

        if direction is None:
            return None

        # ATR-based stop and target: consistent 2:1 R:R regardless of comp_range size
        # or how far bar.close has overshot comp_high/low at signal time.
        atr_stop_mult   = cfg.get('atr_stop_multiplier', 1.0)
        atr_target_mult = cfg.get('atr_target_multiplier', 2.0)
        if direction == 'LONG':
            stop_price   = bar.close - atr_stop_mult * atr
            target_price = bar.close + atr_target_mult * atr
        else:
            stop_price   = bar.close + atr_stop_mult * atr
            target_price = bar.close - atr_target_mult * atr

        risk = atr_stop_mult * atr
        if risk <= 0:
            return None

        s.direction_used.add(direction)
        if len(s.direction_used) >= cfg.get('max_trades_per_day', 1):
            s.phase = 'USED'

        return Signal(
            strategy_name=self.name,
            instrument=bar.instrument,
            timestamp=bar.timestamp,
            direction=direction,
            entry_type='MARKET',
            stop_price=stop_price,
            target_price=target_price,
            metadata={
                'comp_high':        comp_high,
                'comp_low':         comp_low,
                'comp_range_pts':   round(comp_range, 1),
                'intraday_atr':     round(atr, 2),
                'atr_stop_mult':    atr_stop_mult,
                'atr_target_mult':  atr_target_mult,
                'risk_pts':         round(risk, 1),
                'reward_pts':       round(atr_target_mult * atr, 1),
            },
        )

    def near_miss_metrics(self, ctx: StrategyContext) -> dict:
        market_state = getattr(ctx, 'market_state', None)
        if not market_state or not market_state.compression_detected:
            return {'compression_detected': False}
        comp_high = market_state.comp_high
        comp_low  = market_state.comp_low
        close     = ctx.bar_event.candle.close
        if comp_high and comp_low:
            return {
                'compression_detected': True,
                'comp_high':    comp_high,
                'comp_low':     comp_low,
                'dist_to_high': round(comp_high - close, 1),
                'dist_to_low':  round(close - comp_low, 1),
                'phase': self._state(ctx.bar_event.candle.instrument,
                                      ctx.bar_event.features.session_date).phase,
            }
        return {'compression_detected': True}

    def explain_no_signal(self, ctx: StrategyContext) -> str:
        cfg = ctx.strategy_config.get('compression_breakout', {})
        bar = ctx.bar_event.candle
        t   = bar.timestamp.time()
        h1, m1 = map(int, cfg.get('entry_start', '10:00').split(':'))
        h2, m2 = map(int, cfg.get('no_entry_after', '15:00').split(':'))

        if not cfg.get('enabled', False):
            return RejectionReason.DISABLED
        if t < time(h1, m1) or t >= time(h2, m2):
            return RejectionReason.OUTSIDE_ENTRY_TIME
        atr = ctx.bar_event.features.intraday_atr
        if not atr:
            return RejectionReason.ATR_NOT_WARM
        if atr < cfg.get('min_atr', 0.0):
            return RejectionReason.ATR_NOT_WARM

        market_state = getattr(ctx, 'market_state', None)
        if not market_state or not market_state.compression_detected:
            return RejectionReason.NOT_COMPRESSED

        s = self._state(bar.instrument, ctx.bar_event.features.session_date)
        if s.phase == 'USED':
            return RejectionReason.ALREADY_TRADED
        return RejectionReason.COMPRESSION_WATCHING
