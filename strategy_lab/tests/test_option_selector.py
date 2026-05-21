"""Tests for OptionSelector — expiry/strike selection and rejection filters."""
from datetime import datetime, date, timedelta

import pytest

from src.core.option_models import ChainSnapshot, ChainQuote, OptionLeg
from src.core.option_intent import OptionIntent, DIRECTION_BULLISH, DIRECTION_BEARISH, STRUCTURE_LONG_OPTION
from src.execution.option_selector import OptionSelector, SelectionRejected


# ─── helpers ──────────────────────────────────────────────────────────────────

SESSION_DATE = date(2025, 6, 5)   # Thursday, not expiry day
EXPIRY_NEXT  = date(2025, 6, 12)  # next Thursday
SPOT         = 22_500.0

BASE_CFG = {
    'strike_interval': {'NIFTY': 50},
    'min_premium': 20.0,
    'max_premium': 250.0,
    'max_spread_pct': 0.03,
    'max_quote_age_seconds': 5,
    'allow_expiry_day_trades': False,
}


def _chain(
    spot: float = SPOT,
    expiry: date = EXPIRY_NEXT,
    ts: datetime = None,
    quotes: list = None,
) -> ChainSnapshot:
    if ts is None:
        ts = datetime(2025, 6, 5, 10, 30, 0)
    if quotes is None:
        quotes = _default_quotes(spot)
    return ChainSnapshot(
        timestamp=ts,
        underlying='NIFTY',
        spot=spot,
        expiry=expiry,
        quotes=quotes,
        atm_iv=0.15,
    )


def _default_quotes(spot: float) -> list:
    """Build quotes around spot: strikes from spot-100 to spot+100."""
    interval = 50
    atm = round(spot / interval) * interval
    quotes = []
    for offset in (-100, -50, 0, 50, 100):
        strike = atm + offset
        quotes.append(ChainQuote(strike=strike, option_type='CE', bid=80.0, ask=82.0, last=81.0, iv=0.15))
        quotes.append(ChainQuote(strike=strike, option_type='PE', bid=75.0, ask=77.0, last=76.0, iv=0.15))
    return quotes


def _intent(direction: str = DIRECTION_BULLISH) -> OptionIntent:
    return OptionIntent(
        strategy_name='VWAP_PULLBACK',
        instrument='NIFTY',
        timestamp=datetime(2025, 6, 5, 10, 30, 0),
        direction=direction,
        preferred_structure=STRUCTURE_LONG_OPTION,
        underlying_entry=SPOT,
        underlying_stop=SPOT - 100,
        underlying_target=SPOT + 200,
        max_hold_minutes=60,
    )


selector = OptionSelector()


# ─── expiry rules ─────────────────────────────────────────────────────────────

def test_non_expiry_day_passes():
    leg = selector.select(_intent(), _chain(expiry=EXPIRY_NEXT), BASE_CFG)
    assert leg.expiry == EXPIRY_NEXT


def test_expiry_day_raises_by_default():
    chain = _chain(expiry=SESSION_DATE)   # expiry == session date
    intent = OptionIntent(
        strategy_name='VWAP_PULLBACK', instrument='NIFTY',
        timestamp=datetime(SESSION_DATE.year, SESSION_DATE.month, SESSION_DATE.day, 10, 30),
        direction=DIRECTION_BULLISH, preferred_structure=STRUCTURE_LONG_OPTION,
        underlying_entry=SPOT, underlying_stop=None, underlying_target=None, max_hold_minutes=60,
    )
    with pytest.raises(SelectionRejected, match='expiry_day_blocked'):
        selector.select(intent, chain, BASE_CFG)


def test_expiry_day_allowed_when_config_says_so():
    cfg = {**BASE_CFG, 'allow_expiry_day_trades': True}
    chain = _chain(expiry=SESSION_DATE)
    intent = OptionIntent(
        strategy_name='VWAP_PULLBACK', instrument='NIFTY',
        timestamp=datetime(SESSION_DATE.year, SESSION_DATE.month, SESSION_DATE.day, 10, 30),
        direction=DIRECTION_BULLISH, preferred_structure=STRUCTURE_LONG_OPTION,
        underlying_entry=SPOT, underlying_stop=None, underlying_target=None, max_hold_minutes=60,
    )
    leg = selector.select(intent, chain, cfg)
    assert leg is not None


# ─── strike / direction rules ─────────────────────────────────────────────────

def test_bullish_selects_ce():
    leg = selector.select(_intent(DIRECTION_BULLISH), _chain(), BASE_CFG)
    assert leg.option_type == 'CE'
    assert leg.side == 'BUY'


def test_bearish_selects_pe():
    leg = selector.select(_intent(DIRECTION_BEARISH), _chain(), BASE_CFG)
    assert leg.option_type == 'PE'
    assert leg.side == 'BUY'


def test_bullish_selects_atm_or_itm_ce():
    # ATM for NIFTY at 22500 = 22500
    leg = selector.select(_intent(DIRECTION_BULLISH), _chain(spot=22_500.0), BASE_CFG)
    assert leg.strike in (22_500.0, 22_450.0)  # ATM or 1-step ITM


def test_bearish_selects_atm_or_itm_pe():
    leg = selector.select(_intent(DIRECTION_BEARISH), _chain(spot=22_500.0), BASE_CFG)
    assert leg.strike in (22_500.0, 22_550.0)  # ATM or 1-step ITM (higher strike)


# ─── rejection filters ────────────────────────────────────────────────────────

def test_rejects_zero_bid():
    quotes = [ChainQuote(strike=22_500.0, option_type='CE', bid=0.0, ask=80.0, last=40.0, iv=0.15),
              ChainQuote(strike=22_500.0, option_type='PE', bid=75.0, ask=77.0, last=76.0, iv=0.15),
              ChainQuote(strike=22_450.0, option_type='CE', bid=0.0, ask=80.0, last=40.0, iv=0.15)]
    with pytest.raises(SelectionRejected, match='no_valid_quote'):
        selector.select(_intent(DIRECTION_BULLISH), _chain(quotes=quotes), BASE_CFG)


def test_rejects_premium_below_min():
    quotes = [ChainQuote(strike=22_500.0, option_type='CE', bid=5.0, ask=7.0, last=6.0, iv=0.15),
              ChainQuote(strike=22_450.0, option_type='CE', bid=8.0, ask=10.0, last=9.0, iv=0.15),
              ChainQuote(strike=22_500.0, option_type='PE', bid=75.0, ask=77.0, last=76.0, iv=0.15)]
    with pytest.raises(SelectionRejected, match='premium_too_low'):
        selector.select(_intent(DIRECTION_BULLISH), _chain(quotes=quotes), BASE_CFG)


def test_rejects_premium_above_max():
    quotes = [ChainQuote(strike=22_500.0, option_type='CE', bid=300.0, ask=310.0, last=305.0, iv=0.15),
              ChainQuote(strike=22_450.0, option_type='CE', bid=320.0, ask=330.0, last=325.0, iv=0.15),
              ChainQuote(strike=22_500.0, option_type='PE', bid=75.0, ask=77.0, last=76.0, iv=0.15)]
    with pytest.raises(SelectionRejected, match='premium_too_high'):
        selector.select(_intent(DIRECTION_BULLISH), _chain(quotes=quotes), BASE_CFG)


def test_rejects_wide_spread():
    # bid=60, ask=100 → spread_pct = 40/80 = 0.50 >> 0.03
    quotes = [ChainQuote(strike=22_500.0, option_type='CE', bid=60.0, ask=100.0, last=80.0, iv=0.15),
              ChainQuote(strike=22_450.0, option_type='CE', bid=55.0, ask=95.0, last=75.0, iv=0.15),
              ChainQuote(strike=22_500.0, option_type='PE', bid=75.0, ask=77.0, last=76.0, iv=0.15)]
    with pytest.raises(SelectionRejected, match='spread_too_wide'):
        selector.select(_intent(DIRECTION_BULLISH), _chain(quotes=quotes), BASE_CFG)


def test_rejects_stale_quote():
    stale_chain = _chain(ts=datetime(2025, 6, 5, 10, 0, 0))  # 30 min stale
    intent = _intent()  # timestamp 10:30
    with pytest.raises(SelectionRejected, match='stale_quote'):
        selector.select(intent, stale_chain, BASE_CFG)


def test_leg_instrument_matches_intent():
    leg = selector.select(_intent(), _chain(), BASE_CFG)
    assert leg.instrument == 'NIFTY'


def test_leg_qty_is_one():
    leg = selector.select(_intent(), _chain(), BASE_CFG)
    assert leg.qty == 1
