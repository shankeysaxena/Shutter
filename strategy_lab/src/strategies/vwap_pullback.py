"""
VWAP Pullback Continuation Strategy.

Setup logic:
  Bullish: price establishes trend above VWAP → pulls back near VWAP → recaptures VWAP
  Bearish: price establishes trend below VWAP → pullback toward VWAP → back below VWAP

State machine per direction per session (resets each new session_date):
  IDLE → TREND → PULLBACK → SIGNAL_READY → USED
                         ↘ INVALIDATED (price crosses VWAP decisively)

Entry: next bar open after SIGNAL_READY
Stop: pullback low (long) / pullback high (short)
Target: 2R from actual entry (computed in simulator)
"""
from dataclasses import dataclass, field
from datetime import time
from typing import Optional, Dict

from src.core.models import Signal, StrategyContext
from src.core.enums import RejectionReason
from src.strategies.base import BaseStrategy


@dataclass
class _DirectionalState:
    phase: str = 'IDLE'           # IDLE, TREND, PULLBACK, SIGNAL_READY, USED, INVALIDATED
    consecutive_trend_bars: int = 0
    pullback_extreme: Optional[float] = None   # low for LONG, high for SHORT


@dataclass
class _VWAPSessionState:
    long: _DirectionalState = field(default_factory=_DirectionalState)
    short: _DirectionalState = field(default_factory=_DirectionalState)


class VWAPPullbackStrategy(BaseStrategy):
    name = 'VWAP_PULLBACK'

    def __init__(self):
        self._session_states: Dict = {}   # (instrument, session_date) -> _VWAPSessionState

    def reset(self) -> None:
        """Clear accumulated session state so this instance can be safely reused."""
        self._session_states.clear()

    def _get_session_state(self, instrument: str, session_date) -> _VWAPSessionState:
        key = (instrument, session_date)
        if key not in self._session_states:
            self._session_states[key] = _VWAPSessionState()
        return self._session_states[key]

    def generate_signal(self, ctx: StrategyContext) -> Optional[Signal]:
        bar = ctx.bar_event.candle
        features = ctx.bar_event.features
        state = ctx.engine_state
        config = ctx.strategy_config.get('vwap_pullback', {})

        if not config.get('enabled', True):
            return None

        no_entry_after = config.get('no_entry_after', '13:30')
        h, m = map(int, no_entry_after.split(':'))
        if bar.timestamp.time() >= time(h, m):
            return None

        max_trades = config.get('max_trades_per_day', 2)
        filled = state.per_strategy_day_trade_count.get(self.name, 0)
        queued = sum(1 for s in state.queued_signals if s.strategy_name == self.name)
        if filled + queued >= max_trades:
            return None

        if features.vwap is None or features.vwap_distance is None:
            return None

        pullback_zone_pct = config.get('pullback_zone_pct', 0.003)
        min_trend_bars = config.get('min_trend_bars', 3)
        target_r = config.get('target_r', 2.0)

        ss = self._get_session_state(bar.instrument, features.session_date)

        # Advance both state machines before checking for signal
        _advance(ss.long,  'LONG',  bar, features, pullback_zone_pct, min_trend_bars)
        _advance(ss.short, 'SHORT', bar, features, pullback_zone_pct, min_trend_bars)

        traded = {t.direction for t in state.closed_trades + state.open_trades
                  if t.strategy_name == self.name}
        traded |= {s.direction for s in state.queued_signals if s.strategy_name == self.name}

        # Long signal
        if ss.long.phase == 'SIGNAL_READY' and 'LONG' not in traded:
            pullback_low = ss.long.pullback_extreme
            if pullback_low is not None:
                risk = bar.close - pullback_low
                if risk > 0:
                    ss.long.phase = 'USED'
                    return Signal(
                        strategy_name=self.name,
                        instrument=bar.instrument,
                        timestamp=bar.timestamp,
                        direction='LONG',
                        entry_type='MARKET',
                        stop_price=pullback_low,
                        target_price=0.0,
                        metadata={'target_r': target_r, 'pullback_low': pullback_low},
                    )
            ss.long.phase = 'INVALIDATED'

        # Short signal
        if ss.short.phase == 'SIGNAL_READY' and 'SHORT' not in traded:
            pullback_high = ss.short.pullback_extreme
            if pullback_high is not None:
                risk = pullback_high - bar.close
                if risk > 0:
                    ss.short.phase = 'USED'
                    return Signal(
                        strategy_name=self.name,
                        instrument=bar.instrument,
                        timestamp=bar.timestamp,
                        direction='SHORT',
                        entry_type='MARKET',
                        stop_price=pullback_high,
                        target_price=0.0,
                        metadata={'target_r': target_r, 'pullback_high': pullback_high},
                    )
            ss.short.phase = 'INVALIDATED'

        return None

    def explain_no_signal(self, ctx: StrategyContext) -> str:
        config = ctx.strategy_config.get('vwap_pullback', {})
        bar = ctx.bar_event.candle
        features = ctx.bar_event.features
        state = ctx.engine_state

        if not config.get('enabled', True):
            return RejectionReason.DISABLED

        no_entry_after = config.get('no_entry_after', '13:30')
        h, m = map(int, no_entry_after.split(':'))
        if bar.timestamp.time() >= time(h, m):
            return RejectionReason.AFTER_CUTOFF

        max_trades = config.get('max_trades_per_day', 2)
        filled = state.per_strategy_day_trade_count.get(self.name, 0)
        queued = sum(1 for s in state.queued_signals if s.strategy_name == self.name)
        if filled + queued >= max_trades:
            return RejectionReason.MAX_TRADES_REACHED

        if features.vwap is None or features.vwap_distance is None:
            return RejectionReason.VWAP_NOT_AVAILABLE

        ss = self._get_session_state(bar.instrument, features.session_date)
        # Report the most actionable state
        if ss.long.phase in ('INVALIDATED', 'USED') and ss.short.phase in ('INVALIDATED', 'USED'):
            return RejectionReason.SETUP_INVALIDATED
        if ss.long.phase == 'IDLE' or ss.short.phase == 'IDLE':
            return RejectionReason.TREND_NOT_ESTABLISHED
        if ss.long.phase == 'TREND' or ss.short.phase == 'TREND':
            return RejectionReason.NO_PULLBACK
        if ss.long.phase == 'PULLBACK' or ss.short.phase == 'PULLBACK':
            return RejectionReason.IN_PULLBACK

        return RejectionReason.NO_SIGNAL

    def near_miss_metrics(self, ctx: StrategyContext) -> dict:
        features = ctx.bar_event.features
        config   = ctx.strategy_config.get('vwap_pullback', {})
        ss = self._get_session_state(
            ctx.bar_event.candle.instrument, features.session_date
        )
        min_bars = config.get('min_trend_bars', 3)
        return {
            'phase':         ss.long.phase if features.above_vwap else ss.short.phase,
            'trend_bars':    ss.long.consecutive_trend_bars if features.above_vwap
                             else ss.short.consecutive_trend_bars,
            'trend_bars_needed': min_bars,
            'pct_to_trigger': round(
                min(ss.long.consecutive_trend_bars, ss.short.consecutive_trend_bars)
                / min_bars * 100
            ),
        }


def _advance(
    ds: _DirectionalState,
    direction: str,
    bar,
    features,
    pullback_zone_pct: float,
    min_trend_bars: int,
) -> None:
    """
    Advance one directional state machine by one bar.
    Mutates ds in place.

    State transitions for LONG:
      IDLE  → TREND      : consecutive above_vwap >= min_trend_bars
      TREND → PULLBACK   : vwap_distance < pullback_zone_pct (approaching VWAP)
      TREND → INVALIDATED: close below VWAP
      PULLBACK → SIGNAL_READY: close above VWAP AND distance back above zone
      PULLBACK → INVALIDATED:  close below VWAP
      (track pullback_extreme = min(low) during PULLBACK)

    SHORT is the exact mirror.
    """
    if ds.phase in ('USED', 'INVALIDATED'):
        return

    above = features.above_vwap
    below = features.below_vwap
    dist = features.vwap_distance  # (close - vwap) / vwap

    if direction == 'LONG':
        if ds.phase == 'IDLE':
            if above:
                ds.consecutive_trend_bars += 1
                if ds.consecutive_trend_bars >= min_trend_bars:
                    ds.phase = 'TREND'
            else:
                ds.consecutive_trend_bars = 0

        elif ds.phase == 'TREND':
            if below:
                ds.phase = 'INVALIDATED'
            elif dist is not None and dist < pullback_zone_pct:
                ds.phase = 'PULLBACK'
                ds.pullback_extreme = bar.low

        elif ds.phase == 'PULLBACK':
            if below:
                ds.phase = 'INVALIDATED'
            elif above and dist is not None and dist >= pullback_zone_pct:
                ds.phase = 'SIGNAL_READY'
            else:
                # Still in pullback zone — track the lowest low seen
                if ds.pullback_extreme is None or bar.low < ds.pullback_extreme:
                    ds.pullback_extreme = bar.low

    else:  # SHORT
        if ds.phase == 'IDLE':
            if below:
                ds.consecutive_trend_bars += 1
                if ds.consecutive_trend_bars >= min_trend_bars:
                    ds.phase = 'TREND'
            else:
                ds.consecutive_trend_bars = 0

        elif ds.phase == 'TREND':
            if above:
                ds.phase = 'INVALIDATED'
            elif dist is not None and dist > -pullback_zone_pct:
                ds.phase = 'PULLBACK'
                ds.pullback_extreme = bar.high

        elif ds.phase == 'PULLBACK':
            if above:
                ds.phase = 'INVALIDATED'
            elif below and dist is not None and dist <= -pullback_zone_pct:
                ds.phase = 'SIGNAL_READY'
            else:
                if ds.pullback_extreme is None or bar.high > ds.pullback_extreme:
                    ds.pullback_extreme = bar.high
