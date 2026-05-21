"""
OR Failure Fade strategy.

Philosophy: monetise what ORB is already losing on.
When a breakout of the Opening Range fails — price reclaims inside the OR —
enter in the opposite direction, targeting VWAP or the OR midpoint.

Evidence base (NIFTY + BANKNIFTY 2025):
  249 ORB STOP exits at avg -₹3,089 each — all failed breakouts
  Very wide OR (>0.6%): PF 0.46 — breakouts almost never hold on these days
  Large gap days: -₹24,163 — gap-induced opens reverse hard
  34% of ORB stops happen within 30 min — fast reversals dominate

Signal sequence:
    OR ready (after 09:30)
      ↓
    Close > OR_high (or < OR_low)  →  phase = BROKEN_ABOVE / BROKEN_BELOW
      track extreme_high / extreme_low during the break
      ↓ (within max_failure_bars)
    Close back inside OR  →  SIGNAL emitted (SHORT / LONG)
      stop  = extreme ± stop_buffer
      target = VWAP at signal bar  (or OR midpoint)
      ↓
    USED  (terminal for that direction this session)

    If breakout holds beyond max_failure_bars → phase = IDLE (gave up, it's a real break)

Why R:R works here (unlike VWAP Reversion):
    Stop = breakout_size + small_buffer  (~20 + 6 = 26 pts)
    Target = OR_high to VWAP  (~30–60 pts depending on OR width)
    Wide OR days — where this fires most — have more room to VWAP → R:R improves naturally.

State machine per (instrument, session_date, direction):
    IDLE → BROKEN → USED     (one per direction per session)
"""
from dataclasses import dataclass
from datetime import time
from typing import Dict, Optional, Tuple

from src.core.enums import RejectionReason
from src.core.models import Signal, StrategyContext
from src.strategies.base import BaseStrategy


@dataclass
class _DirectionState:
    phase: str = 'IDLE'          # IDLE | BROKEN | USED
    extreme: Optional[float] = None   # highest high (SHORT) or lowest low (LONG) during break
    break_bar_count: int = 0


@dataclass
class _FadeSessionState:
    above: _DirectionState    # watching for failed OR_high break → SHORT
    below: _DirectionState    # watching for failed OR_low  break → LONG

    def __init__(self):
        self.above = _DirectionState()
        self.below = _DirectionState()


class ORFailureFadeStrategy(BaseStrategy):
    """Fades failed Opening Range breakouts back toward VWAP / OR midpoint."""

    name = 'OR_FAILURE_FADE'

    def __init__(self):
        self._states: Dict[Tuple, _FadeSessionState] = {}

    def reset(self) -> None:
        self._states.clear()

    def _session(self, instrument: str, session_date) -> _FadeSessionState:
        key = (instrument, session_date)
        if key not in self._states:
            self._states[key] = _FadeSessionState()
        return self._states[key]

    # ----------------------------------------------------------------
    # Signal generation
    # ----------------------------------------------------------------

    def generate_signal(self, ctx: StrategyContext) -> Optional[Signal]:
        cfg = ctx.strategy_config.get('or_failure_fade', {})
        if not cfg.get('enabled', False):
            return None

        bar      = ctx.bar_event.candle
        features = ctx.bar_event.features
        state_e  = ctx.engine_state

        # Time gate
        no_entry_after = cfg.get('no_entry_after', '13:30')
        h, m = map(int, no_entry_after.split(':'))
        if bar.timestamp.time() >= time(h, m):
            return None

        # OR must be ready
        if not features.or_ready:
            return None
        or_high = features.or_high
        or_low  = features.or_low
        if or_high is None or or_low is None:
            return None

        vwap = features.vwap
        sess = self._session(bar.instrument, features.session_date)

        # Daily cap check
        traded = {t.direction for t in (state_e.open_trades + state_e.closed_trades)
                  if t.strategy_name == self.name}
        traded |= {s.direction for s in state_e.queued_signals
                   if s.strategy_name == self.name}
        max_trades = cfg.get('max_trades_per_day', 2)
        total_committed = (
            state_e.per_strategy_day_trade_count.get(self.name, 0)
            + sum(1 for s in state_e.queued_signals if s.strategy_name == self.name)
        )
        if total_committed >= max_trades:
            return None

        max_fail            = cfg.get('max_failure_bars', 5)
        buf_pct             = cfg.get('stop_buffer_pct', 0.0003)
        tgt_type            = cfg.get('target_type', 'vwap')
        or_mid              = (or_high + or_low) / 2.0
        min_gap_pct         = cfg.get('min_gap_pct', 0.0)
        min_or_width        = cfg.get('min_or_width_pct', 0.0)
        require_vwap_reclaim = cfg.get('require_vwap_reclaim', False)
        stop_mode           = cfg.get('stop_mode', 'hybrid_atr')
        atr_stop_mult       = cfg.get('atr_stop_multiplier', 1.0)
        atr                 = features.intraday_atr or 0.0

        # Structural filters — checked once per session; if the day's gap or
        # OR width is outside the empirically-validated zone, skip entirely.
        # Both are session-level constants broadcast to every bar by the features.
        gap_abs = abs(features.gap_pct) if features.gap_pct is not None else 0.0
        or_w    = features.or_width     if features.or_width is not None else 0.0

        if gap_abs < min_gap_pct:
            return None
        if or_w < min_or_width:
            return None

        # Snapshot feature values to pass into _advance (features not in scope there)
        feat_snapshot = {
            'or_high':  features.or_high,
            'or_low':   features.or_low,
            'gap_pct':  features.gap_pct,
        }

        # ── Advance the ABOVE state machine (watching for failed OR_high break → SHORT)
        if sess.above.phase != 'USED' and 'SHORT' not in traded:
            sig = self._advance(
                ds=sess.above,
                direction='SHORT',
                close=bar.close, high=bar.high, low=bar.low,
                boundary=or_high, broke_above=True,
                max_fail=max_fail, buf_pct=buf_pct,
                stop_mode=stop_mode, atr_stop_mult=atr_stop_mult, atr=atr,
                vwap=vwap, or_mid=or_mid, tgt_type=tgt_type,
                require_vwap_reclaim=require_vwap_reclaim,
                bar=bar, feat_snapshot=feat_snapshot,
            )
            if sig:
                return sig

        if sess.below.phase != 'USED' and 'LONG' not in traded:
            sig = self._advance(
                ds=sess.below,
                direction='LONG',
                close=bar.close, high=bar.high, low=bar.low,
                boundary=or_low, broke_above=False,
                max_fail=max_fail, buf_pct=buf_pct,
                stop_mode=stop_mode, atr_stop_mult=atr_stop_mult, atr=atr,
                vwap=vwap, or_mid=or_mid, tgt_type=tgt_type,
                require_vwap_reclaim=require_vwap_reclaim,
                bar=bar, feat_snapshot=feat_snapshot,
            )
            if sig:
                return sig

        return None

    def _advance(
        self,
        ds: _DirectionState,
        direction: str,
        close: float,
        high: float,
        low: float,
        boundary: float,
        broke_above: bool,
        max_fail: int,
        buf_pct: float,
        vwap: Optional[float],
        or_mid: float,
        tgt_type: str,
        bar,
        feat_snapshot: Optional[Dict] = None,
        stop_mode: str = 'hybrid_atr',
        atr_stop_mult: float = 1.0,
        atr: float = 0.0,
        require_vwap_reclaim: bool = False,
    ) -> Optional[Signal]:
        """Advance one direction's state machine. Returns a Signal or None."""

        if ds.phase == 'IDLE':
            # Check for break
            if broke_above and close > boundary:
                ds.phase = 'BROKEN'
                ds.extreme = high
                ds.break_bar_count = 1
            elif not broke_above and close < boundary:
                ds.phase = 'BROKEN'
                ds.extreme = low
                ds.break_bar_count = 1
            return None

        if ds.phase == 'BROKEN':
            ds.break_bar_count += 1

            # Update extreme
            if broke_above:
                if high > ds.extreme:
                    ds.extreme = high
            else:
                if low < ds.extreme:
                    ds.extreme = low

            # Check if breakout persisted too long → give up (it's a real break)
            if ds.break_bar_count > max_fail:
                ds.phase = 'IDLE'
                ds.extreme = None
                ds.break_bar_count = 0
                return None

            # Check for failure (close back inside OR)
            failure = (broke_above and close < boundary) or \
                      (not broke_above and close > boundary)

            # Optional VWAP reclaim: require price has crossed VWAP as further
            # confirmation that the OR break is a genuine trap, not sideways chop.
            # For SHORT (failed OR_high break): close must be below VWAP.
            # For LONG  (failed OR_low break):  close must be above VWAP.
            if failure and require_vwap_reclaim and vwap is not None:
                if broke_above:
                    failure = failure and close < vwap
                else:
                    failure = failure and close > vwap

            if not failure:
                return None

            # Failure confirmed → build signal
            buf = close * buf_pct
            extreme_stop_short = ds.extreme + buf
            extreme_stop_long  = ds.extreme - buf
            atr_stop_short     = close + atr_stop_mult * atr if atr > 0 else extreme_stop_short
            atr_stop_long      = close - atr_stop_mult * atr if atr > 0 else extreme_stop_long

            if direction == 'SHORT':
                if stop_mode == 'hybrid_atr' and atr > 0:
                    stop_price = min(extreme_stop_short, atr_stop_short)  # tighter of the two
                elif stop_mode == 'atr_based' and atr > 0:
                    stop_price = atr_stop_short
                else:
                    stop_price = extreme_stop_short
            else:
                if stop_mode == 'hybrid_atr' and atr > 0:
                    stop_price = max(extreme_stop_long, atr_stop_long)    # tighter of the two
                elif stop_mode == 'atr_based' and atr > 0:
                    stop_price = atr_stop_long
                else:
                    stop_price = extreme_stop_long

            # Target: VWAP (if available) or OR midpoint
            if tgt_type == 'vwap' and vwap is not None:
                target_price = vwap
            else:
                target_price = or_mid

            risk = abs(close - stop_price)
            if risk <= 0:
                return None

            # Sanity: target must be on the right side of entry
            if direction == 'SHORT' and target_price >= close:
                target_price = or_mid   # fallback to OR midpoint
            if direction == 'LONG' and target_price <= close:
                target_price = or_mid

            if abs(close - target_price) <= 0:
                return None

            ds.phase = 'USED'
            return Signal(
                strategy_name=self.name,
                instrument=bar.instrument,
                timestamp=bar.timestamp,
                direction=direction,
                entry_type='MARKET',
                stop_price=stop_price,
                target_price=target_price,
                metadata={
                    'boundary':         boundary,
                    'extreme':          ds.extreme,
                    'breakout_size':    abs(ds.extreme - boundary),
                    'break_bars':       ds.break_bar_count,
                    'or_mid':           or_mid,
                    'or_high':          (feat_snapshot or {}).get('or_high'),
                    'or_low':           (feat_snapshot or {}).get('or_low'),
                    'vwap_at_signal':   vwap,
                    'target_type':      tgt_type,
                    'gap_pct':          (feat_snapshot or {}).get('gap_pct'),
                },
            )

        return None

    # ----------------------------------------------------------------
    # Diagnostics
    # ----------------------------------------------------------------

    def explain_no_signal(self, ctx: StrategyContext) -> str:
        cfg = ctx.strategy_config.get('or_failure_fade', {})
        bar = ctx.bar_event.candle
        features = ctx.bar_event.features

        if not cfg.get('enabled', False):
            return RejectionReason.DISABLED

        no_entry_after = cfg.get('no_entry_after', '13:30')
        h, m = map(int, no_entry_after.split(':'))
        if bar.timestamp.time() >= time(h, m):
            return RejectionReason.AFTER_CUTOFF

        if not features.or_ready:
            return RejectionReason.OR_NOT_READY

        # Structural filters
        gap_abs  = abs(features.gap_pct)  if features.gap_pct  is not None else 0.0
        or_w     = features.or_width      if features.or_width  is not None else 0.0
        if gap_abs < cfg.get('min_gap_pct', 0.0):
            return RejectionReason.GAP_BELOW_THRESHOLD
        if or_w < cfg.get('min_or_width_pct', 0.0):
            return RejectionReason.OR_WIDTH_TOO_WIDE   # reuse: too narrow here

        sess = self._session(bar.instrument, features.session_date)

        # Check which direction is most active
        for ds, label in [(sess.above, 'SHORT'), (sess.below, 'LONG')]:
            if ds.phase == 'BROKEN':
                return RejectionReason.WAITING_FOR_FAILURE
            if ds.phase == 'USED':
                return RejectionReason.ALREADY_TRADED

        # Neither direction has seen a break yet
        close = bar.close
        or_high = features.or_high
        or_low  = features.or_low
        if or_high is None or or_low is None:
            return RejectionReason.OR_VALUES_INVALID

        if or_low <= close <= or_high:
            return RejectionReason.OR_NOT_BROKEN

        return RejectionReason.BREAKOUT_HELD

    def near_miss_metrics(self, ctx: StrategyContext) -> dict:
        features = ctx.bar_event.features
        cfg      = ctx.strategy_config.get('or_failure_fade', {})
        gap_abs  = abs(features.gap_pct) if features.gap_pct is not None else 0.0
        min_gap  = cfg.get('min_gap_pct', 0.0)
        sess     = self._session(ctx.bar_event.candle.instrument, features.session_date)
        return {
            'above_phase':        sess.above.phase,
            'below_phase':        sess.below.phase,
            'gap_abs_pct':        round(gap_abs * 100, 2),
            'min_gap_pct':        round(min_gap * 100, 2),
            'gap_pct_to_trigger': round(min(gap_abs / min_gap * 100, 100)) if min_gap > 0 else 100,
        }
