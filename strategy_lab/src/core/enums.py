"""
Named constants for structured signal rejection reasons.

Using constants instead of raw strings lets callers match against known
reasons reliably and prevents typo-silent mismatches in logs.
All strategies and the engine should reference these constants.
"""


class RejectionReason:
    # Generic
    NO_SIGNAL = 'no_signal'
    DISABLED = 'disabled'
    AFTER_CUTOFF = 'after_cutoff'
    DAILY_CAP_REACHED = 'daily_cap_reached'
    MAX_TRADES_REACHED = 'max_trades_reached'

    # Opening range
    OR_NOT_READY = 'or_not_ready'
    OR_VALUES_INVALID = 'or_values_invalid'
    NO_BREAKOUT = 'no_breakout'

    # Direction / trade guards
    LONG_ALREADY_TRADED = 'long_already_traded'
    SHORT_ALREADY_TRADED = 'short_already_traded'
    BOTH_DIRECTIONS_TRADED = 'both_directions_traded'
    ALREADY_TRADED = 'already_traded'

    # VWAP Pullback
    VWAP_NOT_AVAILABLE = 'vwap_not_available'
    TREND_NOT_ESTABLISHED = 'trend_not_established'
    NO_PULLBACK = 'no_pullback'
    IN_PULLBACK = 'in_pullback'
    SETUP_INVALIDATED = 'setup_invalidated'

    # Gap Behavior
    GAP_BELOW_THRESHOLD = 'gap_below_threshold'
    GAP_NOT_AVAILABLE = 'gap_not_available'

    # Compression Breakout
    NOT_COMPRESSED             = 'not_compressed'
    COMPRESSION_WATCHING       = 'compression_watching'
    OUTSIDE_ENTRY_TIME         = 'outside_entry_time'

    # VWAP Mean Reversion
    VWAP_STRETCH_INSUFFICIENT = 'vwap_stretch_insufficient'
    NO_REVERSAL_SIGNAL        = 'no_reversal_signal'
    STRETCH_INVALIDATED       = 'stretch_invalidated'
    ATR_NOT_WARM              = 'atr_not_warm'

    # OR Failure Fade
    OR_NOT_BROKEN             = 'or_not_broken'
    WAITING_FOR_FAILURE       = 'waiting_for_failure'
    BREAKOUT_HELD             = 'breakout_held'

    # Iron Fly
    CHAIN_NOT_AVAILABLE        = 'chain_not_available'
    DTE_NOT_ALLOWED            = 'dte_not_allowed'
    EVENT_DAY_0DTE_BLOCKED     = 'event_day_0dte_blocked'
    TREND_TOO_STRONG           = 'trend_too_strong'
    OR_WIDTH_TOO_WIDE          = 'or_width_too_wide'
    OR_HISTORY_NOT_READY       = 'or_history_not_ready'
    IV_REGIME_OUT_OF_BAND      = 'iv_regime_out_of_band'
    IV_REGIME_NOT_WARM         = 'iv_regime_not_warm'
    LIQUIDITY_INSUFFICIENT     = 'liquidity_insufficient'
    STRUCTURE_ALREADY_OPEN     = 'structure_already_open'
    DAY_DONE                   = 'day_done'
    OUTSIDE_ENTRY_WINDOW       = 'outside_entry_window'
