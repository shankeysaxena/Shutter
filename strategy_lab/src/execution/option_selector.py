"""
OptionSelector — picks expiry and strike from a ChainSnapshot for a given OptionIntent.

Rules (Phase 1 — long option buying only):

  Expiry:  nearest weekly with ≥1 full trading day remaining.
           On expiry day itself, step to next expiry (unless
           allow_expiry_day_trades: true in config).

  Strike:  ATM or 1-step ITM in the direction of the trade.
           BULLISH → CE at ATM or 1 strike below spot (ITM CE).
           BEARISH → PE at ATM or 1 strike above spot (ITM PE).

  Reject when:
    - bid or ask ≤ 0
    - premium below min_premium or above max_premium
    - bid-ask spread > max_spread_pct × mid
    - quote age > max_quote_age_seconds (uses chain.timestamp vs intent.timestamp)
    - no valid quote found in chain
"""
from datetime import date, timedelta
from typing import Optional

from src.core.option_models import ChainSnapshot, ChainQuote, OptionLeg
from src.core.option_intent import OptionIntent, DIRECTION_BULLISH, DIRECTION_BEARISH


class SelectionRejected(Exception):
    """Raised (with reason string) when no valid option can be selected."""


class OptionSelector:
    """Selects a single OptionLeg from a ChainSnapshot for a given OptionIntent."""

    def select(
        self,
        intent: OptionIntent,
        chain: ChainSnapshot,
        config: dict,
    ) -> OptionLeg:
        """
        Return the best OptionLeg for the intent, or raise SelectionRejected.

        config keys (all optional, sensible defaults):
          expiry_policy            : 'nearest_weekly_excluding_expiry_day' (default)
          allow_expiry_day_trades  : false (default)
          strike_policy            : 'atm_or_one_itm' (default)
          strike_interval          : {NIFTY: 50, BANKNIFTY: 100}
          min_premium              : 20
          max_premium              : 250
          max_spread_pct           : 0.03
          max_quote_age_seconds    : 5
        """
        self._validate_expiry(intent, chain, config)
        quote = self._select_strike(intent, chain, config)
        self._validate_quote(quote, intent, chain, config)
        option_type = 'CE' if intent.direction == DIRECTION_BULLISH else 'PE'
        return OptionLeg(
            instrument=intent.instrument,
            expiry=chain.expiry,
            strike=quote.strike,
            option_type=option_type,
            side='BUY',
            qty=1,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_expiry(self, intent: OptionIntent, chain: ChainSnapshot, config: dict) -> None:
        session_date = intent.timestamp.date()
        allow_expiry_day = config.get('allow_expiry_day_trades', False)
        if chain.expiry == session_date and not allow_expiry_day:
            raise SelectionRejected(
                f'expiry_day_blocked: expiry={chain.expiry} == session={session_date}'
            )

    def _atm_strike(self, spot: float, interval: float) -> float:
        return round(spot / interval) * interval

    def _select_strike(
        self, intent: OptionIntent, chain: ChainSnapshot, config: dict
    ) -> ChainQuote:
        intervals = config.get('strike_interval', {'NIFTY': 50, 'BANKNIFTY': 100})
        interval  = intervals.get(intent.instrument, 50)
        atm       = self._atm_strike(chain.spot, interval)
        option_type = 'CE' if intent.direction == DIRECTION_BULLISH else 'PE'

        # ATM first, then 1-step ITM
        if intent.direction == DIRECTION_BULLISH:
            strikes_to_try = [atm, atm - interval]   # ITM CE = lower strike
        else:
            strikes_to_try = [atm, atm + interval]   # ITM PE = higher strike

        for strike in strikes_to_try:
            q = chain.quote(strike, option_type)
            if q is not None and q.bid > 0 and q.ask > 0:
                return q

        raise SelectionRejected(
            f'no_valid_quote: spot={chain.spot} atm={atm} option_type={option_type}'
        )

    def _validate_quote(
        self,
        quote: ChainQuote,
        intent: OptionIntent,
        chain: ChainSnapshot,
        config: dict,
    ) -> None:
        mid = (quote.bid + quote.ask) / 2.0
        min_prem    = config.get('min_premium', 20.0)
        max_prem    = config.get('max_premium', 250.0)
        max_spread  = config.get('max_spread_pct', 0.03)
        max_age_sec = config.get('max_quote_age_seconds', 5)

        if mid < min_prem:
            raise SelectionRejected(f'premium_too_low: mid={mid:.1f} < min={min_prem}')
        if mid > max_prem:
            raise SelectionRejected(f'premium_too_high: mid={mid:.1f} > max={max_prem}')

        spread_pct = (quote.ask - quote.bid) / mid if mid > 0 else 1.0
        if spread_pct > max_spread:
            raise SelectionRejected(
                f'spread_too_wide: spread_pct={spread_pct:.3f} > max={max_spread}'
            )

        age_sec = abs((intent.timestamp - chain.timestamp).total_seconds())
        if age_sec > max_age_sec:
            raise SelectionRejected(
                f'stale_quote: age={age_sec:.1f}s > max={max_age_sec}s'
            )
