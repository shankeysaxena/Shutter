"""
Intraday Anchored Iron Fly strategy.

Non-directional short-premium structure on NIFTY/BANKNIFTY weekly options
(0-2 DTE) with five-layered exits:
  1. Touch exit at the configurable touch boundary (default = wing strikes;
     see spec v2 §7.1 — NOT short strikes, since iron fly shorts sit at ATM)
  2. No-progress time exits at T+45 / T+90 (thresholds need real-chain calibration)
  3. Profit target at 15% of max profit (intraday; NOT hold-to-expiry)
  4. Vol-expansion exit
  5. Hard time stop at 15:15

State machine per (instrument, session_date):
  IDLE → ENTERING → OPEN → DONE     (DONE is terminal for the day)

See Iron_Fly_Strategy_Design.md and Iron_Fly_Strategy_Spec_v2.md for context.
Important: P&L from runs against SyntheticOptionChainFeed is not valid for
edge claims (spec v2 §13) — use historical chain snapshots for any strategy
conclusions.
"""
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from typing import Dict, List, Optional, Set, Tuple, Union
import statistics

from src.core.enums import RejectionReason
from src.core.models import StrategyContext
from src.core.option_models import (
    ChainSnapshot,
    LegFill,
    MultiLegSignal,
    MultiLegTrade,
    OptionLeg,
)
from src.features.iv_regime import IVRegimeFeature
from src.strategies.base import BaseStrategy


PHASE_IDLE = 'IDLE'
PHASE_ENTERING = 'ENTERING'
PHASE_OPEN = 'OPEN'
PHASE_DONE = 'DONE'


@dataclass
class _IronFlyState:
    phase: str = PHASE_IDLE
    entry_time: Optional[datetime] = None
    entry_spot: Optional[float] = None
    entry_short_premium_per_unit: Optional[float] = None   # mid(SC) + mid(SP) at entry
    entry_max_profit_rupees: Optional[float] = None         # trade.net_entry_credit
    short_call_strike: Optional[float] = None              # ATM (Iron Fly shorts sit here)
    short_put_strike: Optional[float] = None
    # Touch boundaries — by default at the wings (max-loss frontier) so a touch
    # means the trade has reached its bounded loss region. Configurable via
    # exits.touch_exit.distance_pct_of_wing (1.0 = wing, 0.5 = midway).
    touch_upper: Optional[float] = None
    touch_lower: Optional[float] = None
    open_trade_id: Optional[str] = None
    no_progress_fired: Set[int] = field(default_factory=set)


class IronFlyStrategy(BaseStrategy):
    name = 'IRON_FLY'

    def __init__(
        self,
        event_days: Optional[Set[date]] = None,
        iv_regime: Optional[IVRegimeFeature] = None,
        or_history_min_days: int = 5,
    ):
        self._session_states: Dict[Tuple[str, date], _IronFlyState] = {}
        self._or_width_history: Dict[str, List[Tuple[date, float]]] = {}
        self._captured_or: Set[Tuple[str, date]] = set()
        self._iv_regime = iv_regime or IVRegimeFeature(lookback_days=60, min_observations=500)
        self._event_days: Set[date] = set(event_days) if event_days else set()
        self._or_history_min_days = or_history_min_days

    def reset(self) -> None:
        self._session_states.clear()
        self._or_width_history.clear()
        self._captured_or.clear()
        self._iv_regime.reset()

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    def _state_for(self, instrument: str, session_date: date) -> _IronFlyState:
        key = (instrument, session_date)
        if key not in self._session_states:
            self._session_states[key] = _IronFlyState()
        return self._session_states[key]

    def _capture_or_width(self, instrument: str, session_date: date, or_width: float) -> None:
        key = (instrument, session_date)
        if key in self._captured_or:
            return
        self._captured_or.add(key)
        self._or_width_history.setdefault(instrument, []).append((session_date, or_width))

    def _or_width_median(self, instrument: str, today: date, lookback_days: int) -> Optional[float]:
        history = self._or_width_history.get(instrument, [])
        cutoff = today - timedelta(days=lookback_days)
        recent = [w for d, w in history if cutoff <= d < today]
        if len(recent) < self._or_history_min_days:
            return None
        return statistics.median(recent)

    # ----------------------------------------------------------------
    # Entry
    # ----------------------------------------------------------------

    def generate_signal(self, ctx: StrategyContext) -> Optional[MultiLegSignal]:
        cfg_root = ctx.strategy_config.get('iron_fly', {}) if ctx.strategy_config else {}
        if not cfg_root.get('enabled', False):
            return None

        bar = ctx.bar_event.candle
        features = ctx.bar_event.features
        instrument = bar.instrument
        chain = ctx.chain_snapshot

        # Always update IV regime if chain is available — even when filters fail
        if chain is not None:
            self._iv_regime.update(instrument, bar.timestamp, chain.atm_iv)

        # Capture OR width for trailing median
        if features.or_ready and features.or_width is not None:
            self._capture_or_width(instrument, features.session_date, features.or_width)

        if instrument not in cfg_root.get('underlyings', []):
            return None

        state = self._state_for(instrument, features.session_date)
        if state.phase != PHASE_IDLE:
            return None

        # Time window
        win_start = _parse_time(cfg_root.get('entry_window_start', '09:45'))
        win_end = _parse_time(cfg_root.get('entry_window_end', '13:30'))
        t = bar.timestamp.time()
        if t < win_start or t >= win_end:
            return None

        if chain is None or chain.expiry is None:
            return None

        # DTE filter
        dte = (chain.expiry - bar.timestamp.date()).days
        if dte not in cfg_root.get('allowed_dte', [0, 1, 2]):
            return None
        if dte == 0 and cfg_root.get('event_day_blacklist_0dte', True):
            if bar.timestamp.date() in self._event_days:
                return None

        # Trend filter
        vwap = features.vwap
        if vwap is None or vwap <= 0:
            return None
        max_dist = cfg_root.get('trend_filter', {}).get('max_vwap_distance_pct', 0.0025)
        if abs(bar.close - vwap) / bar.close >= max_dist:
            return None

        # Range filter
        rf = cfg_root.get('range_filter', {})
        median_or = self._or_width_median(
            instrument, features.session_date, rf.get('or_width_lookback_days', 20)
        )
        if median_or is None:
            return None
        if features.or_width is None:
            return None
        if features.or_width >= rf.get('max_or_width_vs_median', 1.0) * median_or:
            return None

        # IV regime filter
        ivf = cfg_root.get('iv_regime_filter', {})
        iv_pct = self._iv_regime.percentile(instrument, chain.atm_iv)
        if iv_pct is None:
            return None
        if not (ivf.get('min_percentile', 0.25) <= iv_pct <= ivf.get('max_percentile', 0.75)):
            return None

        # Build structure
        interval = cfg_root.get('strike_interval', {}).get(instrument)
        if interval is None or interval <= 0:
            return None
        atm = round(bar.close / interval) * interval
        wing_pct = cfg_root.get('wing_width_pct_of_spot', 0.005)
        wing_pts = max(round(bar.close * wing_pct / interval) * interval, interval)
        upper_strike = atm + wing_pts
        lower_strike = atm - wing_pts

        sc_quote = chain.quote(atm, 'CE')
        sp_quote = chain.quote(atm, 'PE')
        lc_quote = chain.quote(upper_strike, 'CE')
        lp_quote = chain.quote(lower_strike, 'PE')
        if any(q is None for q in (sc_quote, sp_quote, lc_quote, lp_quote)):
            return None

        # Liquidity filter
        liq = cfg_root.get('liquidity_filter', {})
        max_spread = liq.get('max_atm_spread_pct', 0.02)
        for q in (sc_quote, sp_quote):
            mid = (q.bid + q.ask) / 2.0
            if mid <= 0 or (q.ask - q.bid) / mid > max_spread:
                return None
        if liq.get('require_two_sided_wings', True):
            for q in (lc_quote, lp_quote):
                if q.bid <= 0 or q.ask <= 0:
                    return None

        # Per-unit estimated net credit at mid
        sc_mid = (sc_quote.bid + sc_quote.ask) / 2.0
        sp_mid = (sp_quote.bid + sp_quote.ask) / 2.0
        lc_mid = (lc_quote.bid + lc_quote.ask) / 2.0
        lp_mid = (lp_quote.bid + lp_quote.ask) / 2.0
        net_credit_per_unit = sc_mid + sp_mid - lc_mid - lp_mid
        if net_credit_per_unit <= 0:
            return None

        lot_size = (
            ctx.strategy_config.get('risk', {}).get('lot_size', {}).get(instrument)
            if ctx.strategy_config else None
        )
        if lot_size is None or lot_size <= 0:
            # Fall back to iron_fly config lot_size if not set in risk block
            lot_size = cfg_root.get('lot_size', {}).get(instrument, 1)

        max_loss_per_lot_rupees = (wing_pts - net_credit_per_unit) * lot_size
        if max_loss_per_lot_rupees <= 0:
            return None

        legs = [
            OptionLeg(instrument, chain.expiry, atm, 'CE', 'SELL', qty=1),
            OptionLeg(instrument, chain.expiry, atm, 'PE', 'SELL', qty=1),
            OptionLeg(instrument, chain.expiry, upper_strike, 'CE', 'BUY', qty=1),
            OptionLeg(instrument, chain.expiry, lower_strike, 'PE', 'BUY', qty=1),
        ]

        # Stash everything the exit logic needs (rupee-level + per-unit context)
        touch_pct = cfg_root.get('exits', {}).get('touch_exit', {}).get('distance_pct_of_wing', 1.0)
        touch_offset = wing_pts * touch_pct
        state.phase = PHASE_ENTERING
        state.entry_spot = bar.close
        state.entry_short_premium_per_unit = sc_mid + sp_mid
        state.short_call_strike = atm
        state.short_put_strike = atm
        state.touch_upper = atm + touch_offset
        state.touch_lower = atm - touch_offset

        signal_metadata = {
            'spot': bar.close,
            'atm_strike': atm,
            'wing_width': wing_pts,
            'dte': dte,
            'atm_iv': chain.atm_iv,
            'iv_percentile': iv_pct,
            'or_width': features.or_width,
            'or_width_median': median_or,
            'vwap': vwap,
            'estimated_net_credit_per_unit': net_credit_per_unit,
            'max_loss_per_lot_rupees': max_loss_per_lot_rupees,
            'lot_size': lot_size,
            'entry_short_premium_per_unit': state.entry_short_premium_per_unit,
        }
        return MultiLegSignal(
            strategy_name=self.name,
            instrument=instrument,
            timestamp=bar.timestamp,
            structure_type='IRON_FLY',
            legs=legs,
            metadata=signal_metadata,
        )

    # ----------------------------------------------------------------
    # Notify hooks — called by the engine after multi-leg fills/closes
    # ----------------------------------------------------------------

    def on_multi_leg_filled(self, trade: MultiLegTrade) -> None:
        """Called by the engine when a queued MultiLegSignal of ours fills."""
        state = self._state_for(trade.instrument, trade.entry_time.date())
        if state.phase != PHASE_ENTERING:
            return
        state.phase = PHASE_OPEN
        state.entry_time = trade.entry_time
        state.entry_max_profit_rupees = trade.net_entry_credit
        state.open_trade_id = trade.trade_id

    def on_multi_leg_closed(self, trade: MultiLegTrade) -> None:
        """Called by the engine when one of our open multi-leg trades closes."""
        state = self._state_for(trade.instrument, trade.entry_time.date())
        if state.open_trade_id == trade.trade_id:
            state.phase = PHASE_DONE

    def on_multi_leg_rejected(self, signal: MultiLegSignal) -> None:
        """Called by the engine when a queued multi-leg signal cannot be sized/filled."""
        state = self._state_for(signal.instrument, signal.timestamp.date())
        if state.phase == PHASE_ENTERING:
            state.phase = PHASE_IDLE

    # ----------------------------------------------------------------
    # Exit evaluation
    # ----------------------------------------------------------------

    def evaluate_multi_leg_exits(
        self, ctx: StrategyContext
    ) -> List[Tuple[MultiLegTrade, str]]:
        cfg_root = ctx.strategy_config.get('iron_fly', {}) if ctx.strategy_config else {}
        if not cfg_root.get('enabled', False):
            return []
        chain = ctx.chain_snapshot
        if chain is None:
            return []

        bar = ctx.bar_event.candle
        exits: List[Tuple[MultiLegTrade, str]] = []

        for trade in ctx.engine_state.open_multi_leg_trades:
            if trade.strategy_name != self.name:
                continue
            state = self._state_for(trade.instrument, trade.entry_time.date())
            if state.phase != PHASE_OPEN:
                continue

            reason = self._exit_reason(trade, state, bar, chain, cfg_root)
            if reason is not None:
                exits.append((trade, reason))

        return exits

    def _exit_reason(
        self,
        trade: MultiLegTrade,
        state: _IronFlyState,
        bar,
        chain: ChainSnapshot,
        cfg_root: Dict,
    ) -> Optional[str]:
        exits_cfg = cfg_root.get('exits', {})

        # 1. Touch exit (highest priority) — at touch boundaries (default: wing strikes).
        # v2.1 #6: use bar.high / bar.low instead of bar.close so an intra-bar
        # touch that retraces doesn't pretend nothing happened. Conservative
        # for risk management: any intra-bar breach triggers the exit.
        if exits_cfg.get('touch_exit', {}).get('enabled', True):
            if state.touch_upper is not None and bar.high >= state.touch_upper:
                return 'TOUCH_EXIT_CALL'
            if state.touch_lower is not None and bar.low <= state.touch_lower:
                return 'TOUCH_EXIT_PUT'

        # Compute current unrealized P&L from chain mids
        mtm = _mark_to_market(trade, chain)
        if mtm is None:
            return None  # cannot evaluate the remaining layers; defer

        max_profit = state.entry_max_profit_rupees or trade.net_entry_credit
        pct_of_max = (mtm / max_profit) if max_profit and max_profit > 0 else 0.0

        # 2. No-progress checkpoints
        np_cfg = exits_cfg.get('no_progress', {})
        if np_cfg.get('enabled', True) and state.entry_time is not None:
            elapsed_min = (bar.timestamp - state.entry_time).total_seconds() / 60.0
            for cp in np_cfg.get('checkpoints', []):
                off = cp.get('offset_minutes')
                if off in state.no_progress_fired:
                    continue
                if elapsed_min >= off:
                    state.no_progress_fired.add(off)
                    if pct_of_max < cp.get('min_profit_pct_of_max', 0.10):
                        return f'NO_PROGRESS_T+{off}'

        # 3. Profit target
        pt_cfg = exits_cfg.get('profit_target', {})
        if pt_cfg.get('enabled', True):
            if pct_of_max >= pt_cfg.get('pct_of_max_profit', 0.40):
                return 'PROFIT_TARGET'

        # 4. Vol expansion
        ve_cfg = exits_cfg.get('vol_expansion', {})
        if ve_cfg.get('enabled', True) and state.entry_short_premium_per_unit:
            current_short_prem = _current_short_premium_per_unit(trade, chain)
            if current_short_prem is not None and state.entry_spot:
                spot_move_pct = abs(bar.close - state.entry_spot) / state.entry_spot
                ratio = current_short_prem / state.entry_short_premium_per_unit
                if (ratio >= ve_cfg.get('premium_multiple_threshold', 1.3)
                        and spot_move_pct < ve_cfg.get('max_spot_move_pct', 0.005)):
                    return 'VOL_EXPANSION'

        # 5. Hard time stop
        hard_stop = _parse_time(exits_cfg.get('hard_time_stop', '15:15'))
        if bar.timestamp.time() >= hard_stop:
            return 'HARD_TIME_STOP'

        return None

    # ----------------------------------------------------------------
    # Diagnostics
    # ----------------------------------------------------------------

    def explain_no_signal(self, ctx: StrategyContext) -> str:
        cfg_root = ctx.strategy_config.get('iron_fly', {}) if ctx.strategy_config else {}
        if not cfg_root.get('enabled', False):
            return RejectionReason.DISABLED

        bar = ctx.bar_event.candle
        features = ctx.bar_event.features
        instrument = bar.instrument
        chain = ctx.chain_snapshot

        if instrument not in cfg_root.get('underlyings', []):
            return RejectionReason.NO_SIGNAL

        state = self._state_for(instrument, features.session_date)
        if state.phase == PHASE_OPEN or state.phase == PHASE_ENTERING:
            return RejectionReason.STRUCTURE_ALREADY_OPEN
        if state.phase == PHASE_DONE:
            return RejectionReason.DAY_DONE

        win_start = _parse_time(cfg_root.get('entry_window_start', '09:45'))
        win_end = _parse_time(cfg_root.get('entry_window_end', '13:30'))
        t = bar.timestamp.time()
        if t < win_start or t >= win_end:
            return RejectionReason.OUTSIDE_ENTRY_WINDOW

        if chain is None or chain.expiry is None:
            return RejectionReason.CHAIN_NOT_AVAILABLE

        dte = (chain.expiry - bar.timestamp.date()).days
        if dte not in cfg_root.get('allowed_dte', [0, 1, 2]):
            return RejectionReason.DTE_NOT_ALLOWED
        if dte == 0 and cfg_root.get('event_day_blacklist_0dte', True):
            if bar.timestamp.date() in self._event_days:
                return RejectionReason.EVENT_DAY_0DTE_BLOCKED

        vwap = features.vwap
        if vwap is None or vwap <= 0:
            return RejectionReason.VWAP_NOT_AVAILABLE
        max_dist = cfg_root.get('trend_filter', {}).get('max_vwap_distance_pct', 0.0025)
        if abs(bar.close - vwap) / bar.close >= max_dist:
            return RejectionReason.TREND_TOO_STRONG

        if features.or_width is None:
            return RejectionReason.OR_NOT_READY
        median_or = self._or_width_median(
            instrument, features.session_date,
            cfg_root.get('range_filter', {}).get('or_width_lookback_days', 20),
        )
        if median_or is None:
            return RejectionReason.OR_HISTORY_NOT_READY
        if features.or_width >= cfg_root.get('range_filter', {}).get('max_or_width_vs_median', 1.0) * median_or:
            return RejectionReason.OR_WIDTH_TOO_WIDE

        iv_pct = self._iv_regime.percentile(instrument, chain.atm_iv)
        if iv_pct is None:
            return RejectionReason.IV_REGIME_NOT_WARM
        ivf = cfg_root.get('iv_regime_filter', {})
        if not (ivf.get('min_percentile', 0.25) <= iv_pct <= ivf.get('max_percentile', 0.75)):
            return RejectionReason.IV_REGIME_OUT_OF_BAND

        return RejectionReason.LIQUIDITY_INSUFFICIENT


# --------------------------------------------------------------------
# Module helpers
# --------------------------------------------------------------------

def _parse_time(s: str) -> time:
    h, m = map(int, s.split(':'))
    return time(h, m)


def _mark_to_market(trade: MultiLegTrade, chain: ChainSnapshot) -> Optional[float]:
    """Unrealized P&L at chain mids (excludes brokerage). Mirrors MultiLegSimulator.mark_to_market."""
    unrealized_close_value = 0.0
    for entry_fill in trade.entry_fills:
        leg = entry_fill.leg
        q = chain.quote(leg.strike, leg.option_type)
        if q is None:
            return None
        mid = (q.bid + q.ask) / 2.0
        units = leg.qty * trade.lot_size
        if leg.side == 'SELL':
            unrealized_close_value += mid * units
        else:
            unrealized_close_value -= mid * units
    return trade.net_entry_credit - unrealized_close_value


def _current_short_premium_per_unit(trade: MultiLegTrade, chain: ChainSnapshot) -> Optional[float]:
    """Sum of mids of the SELL legs at the current chain. Per-unit (not scaled)."""
    total = 0.0
    found = 0
    for fill in trade.entry_fills:
        leg = fill.leg
        if leg.side != 'SELL':
            continue
        q = chain.quote(leg.strike, leg.option_type)
        if q is None:
            return None
        total += (q.bid + q.ask) / 2.0
        found += 1
    return total if found > 0 else None
