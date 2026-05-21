"""
VWAP Mean Reversion Strategy.

Philosophy: the OPPOSITE of VWAP Pullback Continuation.
  - VWAPPullback: trend → pullback near VWAP → continue trend
  - VWAPReversion: price STRETCHES far from VWAP → rejection → revert BACK TO VWAP

State machine per (instrument, session_date):
    IDLE
      │ |vwap_atr_distance| ≥ stretch_threshold AND ATR warm
      ▼
    STRETCHED  (tracks direction, extreme, bar count)
      │ close moves back toward VWAP by ≥ min_reversal_ratio of peak distance
      │ AND min_bars_stretched elapsed
      ▼
    SIGNAL_READY  → signal emitted on this bar
      │
      ▼
    USED  (terminal for the session in that direction)

Entry:
    direction  : LONG if stretched below VWAP, SHORT if stretched above

Stop / target — two modes (Phase 4.9A calibration):

  stop_type: stretch_extreme (original)
      stop = stretch_extreme ± stop_buffer_pct
      KNOWN PROBLEM: stop_buffer (0.1% ≈ 22pts) > entry-to-extreme distance (≈14pts)
      → stop is wider than target → inverted R:R 0.61 → -₹47,669 loss on 298 trades

  stop_type: atr_based (default after calibration)
      stop = entry ∓ atr_stop_multiplier × intraday_atr
      Consistent risk regardless of stretch magnitude.
      target_r = atr_target_multiplier / atr_stop_multiplier (passed to simulator)
      At 0.8 stop × 1.2 target: R:R 1.50, expectancy +₹150/trade at 58% WR.

Max 2 trades per day (one per direction) to limit overtrading on choppy sessions.
"""
from dataclasses import dataclass, field
from datetime import time
from typing import Dict, Optional, Set, Tuple

from src.core.enums import RejectionReason
from src.core.models import Signal, StrategyContext
from src.strategies.base import BaseStrategy


# ─────────────────────────────────────────────────────────────────────────────
# Per-session state
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _ReversionState:
    phase: str = 'IDLE'           # IDLE | STRETCHED | USED
    direction: Optional[str] = None     # 'LONG' (price below VWAP) or 'SHORT' (above)
    stretch_extreme: Optional[float] = None   # worst price seen during stretch
    peak_distance: Optional[float] = None     # max |vwap_distance| seen (fractional)
    stretch_bar_count: int = 0


class VWAPReversionStrategy(BaseStrategy):
    name = 'VWAP_REVERSION'

    def __init__(self):
        self._states: Dict[Tuple, _ReversionState] = {}

    def reset(self) -> None:
        self._states.clear()

    def _state(self, instrument: str, session_date) -> _ReversionState:
        key = (instrument, session_date)
        if key not in self._states:
            self._states[key] = _ReversionState()
        return self._states[key]

    # ─────────────────────────────────────────────────────────────────────
    # Main signal generation
    # ─────────────────────────────────────────────────────────────────────

    def generate_signal(self, ctx: StrategyContext) -> Optional[Signal]:
        cfg = ctx.strategy_config.get('vwap_reversion', {})
        if not cfg.get('enabled', False):
            return None

        bar      = ctx.bar_event.candle
        features = ctx.bar_event.features
        state    = ctx.engine_state

        # Time gate
        no_entry_after = cfg.get('no_entry_after', '13:30')
        h, m = map(int, no_entry_after.split(':'))
        if bar.timestamp.time() >= time(h, m):
            return None

        # VWAP and ATR must be available
        vwap = features.vwap
        vad  = features.vwap_atr_distance   # signed ATR-distance from VWAP
        if vwap is None or vad is None:
            return None

        s = self._state(bar.instrument, features.session_date)
        if s.phase == 'USED':
            return None

        # Max trades cap (one per direction per day)
        traded_directions = {
            t.direction for t in (state.open_trades + state.closed_trades)
            if t.strategy_name == self.name
        }
        traded_directions |= {
            sig.direction for sig in state.queued_signals
            if sig.strategy_name == self.name
        }
        max_per_day = cfg.get('max_trades_per_day', 2)
        total_committed = (
            state.per_strategy_day_trade_count.get(self.name, 0)
            + sum(1 for sig in state.queued_signals if sig.strategy_name == self.name)
        )
        if total_committed >= max_per_day:
            return None

        # ── Advance state machine
        threshold     = cfg.get('stretch_threshold_atr', 1.5)
        min_bars      = cfg.get('min_bars_stretched', 2)
        min_reversal  = cfg.get('min_reversal_ratio', 0.25)
        max_stretch   = cfg.get('max_stretch_bars', 30)
        # stop_mode options:
        #   stretch_extreme  — original (stop at worst stretch point + buffer)
        #   atr_based        — pure ATR stop (ignores stretch extreme)
        #   hybrid_atr       — ATR cap: min(extreme_stop, entry ± N×ATR); tighter of the two
        stop_mode     = cfg.get('stop_mode', 'hybrid_atr')
        atr_stop_mult = cfg.get('atr_stop_multiplier', 1.0)
        stop_buffer   = cfg.get('stop_buffer_pct', 0.0008)

        if s.phase == 'IDLE':
            if abs(vad) >= threshold:
                s.phase = 'STRETCHED'
                s.direction = 'LONG' if vad < 0 else 'SHORT'
                s.stretch_extreme = bar.low if s.direction == 'LONG' else bar.high
                s.peak_distance = abs(vad)
                s.stretch_bar_count = 1
            return None

        if s.phase == 'STRETCHED':
            s.stretch_bar_count += 1

            # Update extreme (track worst point of the stretch)
            if s.direction == 'LONG':
                if bar.low < s.stretch_extreme:
                    s.stretch_extreme = bar.low
            else:
                if bar.high > s.stretch_extreme:
                    s.stretch_extreme = bar.high

            # Update peak distance
            if abs(vad) > s.peak_distance:
                s.peak_distance = abs(vad)

            # Invalidate if stretch drags on too long without reverting
            if s.stretch_bar_count > max_stretch:
                s.phase = 'IDLE'
                return None

            # Opposite stretch: market going further from VWAP in new direction
            # (e.g. price was above, now also below) — invalidate
            if (s.direction == 'LONG' and vad > threshold) or \
               (s.direction == 'SHORT' and vad < -threshold):
                s.phase = 'IDLE'
                return None

            # Reversal signal: close has retraced ≥ min_reversal_ratio of peak distance
            # AND at least min_bars have elapsed since stretch began
            if s.stretch_bar_count >= min_bars:
                retraced = (s.peak_distance - abs(vad)) / s.peak_distance
                if retraced >= min_reversal:
                    # Check we haven't already traded this direction
                    if s.direction in traded_directions:
                        s.phase = 'USED'
                        return None

                    stop_buffer_pts = bar.close * stop_buffer
                    if s.direction == 'LONG':
                        stop_price = s.stretch_extreme - stop_buffer_pts
                    else:
                        stop_price = s.stretch_extreme + stop_buffer_pts

                    risk = abs(bar.close - stop_price)
                    if risk <= 0:
                        return None

                    # ── Stop calculation (entries and target unchanged)
                    atr = features.intraday_atr or 0.0
                    stop_buffer_pts = bar.close * stop_buffer

                    if s.direction == 'LONG':
                        extreme_stop = s.stretch_extreme - stop_buffer_pts
                        atr_stop     = bar.close - atr_stop_mult * atr if atr > 0 else extreme_stop
                    else:
                        extreme_stop = s.stretch_extreme + stop_buffer_pts
                        atr_stop     = bar.close + atr_stop_mult * atr if atr > 0 else extreme_stop

                    if stop_mode == 'hybrid_atr' and atr > 0:
                        # Take the tighter (less risk) of the two: ATR cap prevents
                        # stop from blowing out on violent sessions.
                        if s.direction == 'LONG':
                            stop_price = max(extreme_stop, atr_stop)
                        else:
                            stop_price = min(extreme_stop, atr_stop)
                    elif stop_mode == 'atr_based' and atr > 0:
                        stop_price = atr_stop
                    else:
                        stop_price = extreme_stop   # stretch_extreme (original)

                    # Target: VWAP at signal bar (unchanged across all stop modes)
                    target_price = vwap
                    target_r     = None

                    risk = abs(bar.close - stop_price)
                    if risk <= 0:
                        return None

                    s.phase = 'USED'
                    return Signal(
                        strategy_name=self.name,
                        instrument=bar.instrument,
                        timestamp=bar.timestamp,
                        direction=s.direction,
                        entry_type='MARKET',
                        stop_price=stop_price,
                        target_price=target_price,
                        metadata={
                            'vwap_at_signal':      vwap,
                            'vwap_atr_distance':   round(vad, 3),
                            'peak_distance_atr':   round(s.peak_distance, 3),
                            'stretch_extreme':     s.stretch_extreme,
                            'stretch_bars':        s.stretch_bar_count,
                            'retraced_ratio':      round((s.peak_distance - abs(vad)) / s.peak_distance, 3),
                            'intraday_atr':        features.intraday_atr,
                            'stop_mode':           stop_mode,
                            **(({'target_r': target_r}) if target_r is not None else {}),
                        },
                    )

        return None

    # ─────────────────────────────────────────────────────────────────────
    # Diagnostics
    # ─────────────────────────────────────────────────────────────────────

    def explain_no_signal(self, ctx: StrategyContext) -> str:
        cfg = ctx.strategy_config.get('vwap_reversion', {})
        bar      = ctx.bar_event.candle
        features = ctx.bar_event.features

        if not cfg.get('enabled', False):
            return RejectionReason.DISABLED

        no_entry_after = cfg.get('no_entry_after', '13:30')
        h, m = map(int, no_entry_after.split(':'))
        if bar.timestamp.time() >= time(h, m):
            return RejectionReason.AFTER_CUTOFF

        if features.vwap is None or features.vwap_distance is None:
            return RejectionReason.VWAP_NOT_AVAILABLE

        if features.vwap_atr_distance is None:
            return RejectionReason.ATR_NOT_WARM

        s = self._state(bar.instrument, features.session_date)
        if s.phase == 'USED':
            return RejectionReason.ALREADY_TRADED

        if s.phase == 'IDLE':
            return RejectionReason.VWAP_STRETCH_INSUFFICIENT

        if s.phase == 'STRETCHED':
            return RejectionReason.NO_REVERSAL_SIGNAL

        return RejectionReason.NO_SIGNAL

    def near_miss_metrics(self, ctx: StrategyContext) -> dict:
        features  = ctx.bar_event.features
        cfg       = ctx.strategy_config.get('vwap_reversion', {})
        threshold = cfg.get('stretch_threshold_atr', 1.5)
        vad       = features.vwap_atr_distance
        if vad is None:
            return {}
        current_stretch = abs(vad)
        s = self._state(ctx.bar_event.candle.instrument, features.session_date)
        return {
            'phase':            s.phase,
            'stretch_atr':      round(current_stretch, 2),
            'threshold_atr':    threshold,
            'pct_to_trigger':   round(min(current_stretch / threshold * 100, 100)),
            'peak_stretch_atr': round(s.peak_distance, 2) if s.peak_distance else None,
        }
