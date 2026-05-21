"""
OptionsTranslationLayer — converts OptionIntent into a concrete MultiLegSignal.

Phase 1: LONG_OPTION only.
  BULLISH → BUY ATM/ITM CE
  BEARISH → BUY ATM/ITM PE

Phase 2 (stub, not yet enabled): DEBIT_SPREAD.
  BULLISH → BUY ATM CE + SELL OTM CE
  BEARISH → BUY ATM PE + SELL OTM PE

The layer delegates strike/expiry selection entirely to OptionSelector so
that the translation logic stays structure-focused, not quote-focused.
"""
from typing import Optional

from src.core.option_models import ChainSnapshot, MultiLegSignal
from src.core.option_intent import (
    OptionIntent,
    DIRECTION_BULLISH,
    STRUCTURE_LONG_OPTION,
    STRUCTURE_DEBIT_SPREAD,
)
from src.execution.option_selector import OptionSelector, SelectionRejected


class OptionsTranslationLayer:
    """Converts OptionIntent + ChainSnapshot into a MultiLegSignal."""

    def __init__(self, selector: Optional[OptionSelector] = None):
        self._selector = selector or OptionSelector()

    def translate(
        self,
        intent: OptionIntent,
        chain: ChainSnapshot,
        config: dict,
    ) -> Optional[MultiLegSignal]:
        """
        Return a MultiLegSignal or None if the intent cannot be satisfied.

        config is the top-level strategy config dict; keys consulted:
          options.structures_enabled.long_option  (default True)
          options.structures_enabled.debit_spread (default False)
          options.*  (passed through to OptionSelector)
        """
        options_cfg = config.get('options', {})
        structures  = options_cfg.get('structures_enabled', {})
        selector_cfg = {**options_cfg}  # OptionSelector reads from same dict

        structure = intent.preferred_structure

        if structure == STRUCTURE_LONG_OPTION:
            if not structures.get('long_option', True):
                return None
            return self._build_long_option(intent, chain, selector_cfg)

        if structure == STRUCTURE_DEBIT_SPREAD:
            if not structures.get('debit_spread', False):
                return None
            return self._build_debit_spread(intent, chain, selector_cfg, config)

        return None  # unknown structure

    # ------------------------------------------------------------------

    def _build_long_option(
        self,
        intent: OptionIntent,
        chain: ChainSnapshot,
        selector_cfg: dict,
    ) -> Optional[MultiLegSignal]:
        try:
            leg = self._selector.select(intent, chain, selector_cfg)
        except SelectionRejected as exc:
            return None  # caller can log if needed; None = skip this bar

        return MultiLegSignal(
            strategy_name=intent.strategy_name,
            instrument=intent.instrument,
            timestamp=intent.timestamp,
            structure_type=STRUCTURE_LONG_OPTION,
            legs=[leg],
            metadata={
                **intent.metadata,
                'direction':          intent.direction,
                'underlying_entry':   intent.underlying_entry,
                'underlying_stop':    intent.underlying_stop,
                'underlying_target':  intent.underlying_target,
                'max_hold_minutes':   intent.max_hold_minutes,
                'expiry':             str(chain.expiry),
                'strike':             leg.strike,
                'option_type':        leg.option_type,
            },
        )

    def _build_debit_spread(
        self,
        intent: OptionIntent,
        chain: ChainSnapshot,
        selector_cfg: dict,
        config: dict,
    ) -> Optional[MultiLegSignal]:
        """Phase 2 stub — not yet enabled in live config."""
        try:
            long_leg = self._selector.select(intent, chain, selector_cfg)
        except SelectionRejected:
            return None

        # Determine short leg: N strikes OTM from long leg
        spread_cfg  = config.get('debit_spread', {})
        n_strikes   = spread_cfg.get('short_leg_distance_strikes', 2)
        intervals   = selector_cfg.get('strike_interval', {'NIFTY': 50, 'BANKNIFTY': 100})
        interval    = intervals.get(intent.instrument, 50)
        option_type = long_leg.option_type

        if intent.direction == DIRECTION_BULLISH:
            short_strike = long_leg.strike + n_strikes * interval
        else:
            short_strike = long_leg.strike - n_strikes * interval

        short_quote = chain.quote(short_strike, option_type)
        if short_quote is None or short_quote.bid <= 0:
            return None

        from src.core.option_models import OptionLeg
        short_leg = OptionLeg(
            instrument=intent.instrument,
            expiry=chain.expiry,
            strike=short_strike,
            option_type=option_type,
            side='SELL',
            qty=1,
        )

        return MultiLegSignal(
            strategy_name=intent.strategy_name,
            instrument=intent.instrument,
            timestamp=intent.timestamp,
            structure_type=STRUCTURE_DEBIT_SPREAD,
            legs=[long_leg, short_leg],
            metadata={
                **intent.metadata,
                'direction':        intent.direction,
                'underlying_entry': intent.underlying_entry,
                'expiry':           str(chain.expiry),
                'long_strike':      long_leg.strike,
                'short_strike':     short_strike,
                'option_type':      option_type,
            },
        )
