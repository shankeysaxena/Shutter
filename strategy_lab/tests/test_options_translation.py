"""Tests for OptionsTranslationLayer — Intent → MultiLegSignal."""
from datetime import datetime, date

import pytest

from src.core.option_models import ChainSnapshot, ChainQuote, MultiLegSignal
from src.core.option_intent import (
    OptionIntent, DIRECTION_BULLISH, DIRECTION_BEARISH,
    STRUCTURE_LONG_OPTION, STRUCTURE_DEBIT_SPREAD,
)
from src.execution.options_translation import OptionsTranslationLayer


# ─── helpers ──────────────────────────────────────────────────────────────────

EXPIRY = date(2025, 6, 12)
SPOT   = 22_500.0
TS     = datetime(2025, 6, 5, 10, 30, 0)

BASE_CFG = {
    'options': {
        'structures_enabled': {'long_option': True, 'debit_spread': False},
        'strike_interval': {'NIFTY': 50},
        'min_premium': 20.0,
        'max_premium': 250.0,
        'max_spread_pct': 0.03,
        'max_quote_age_seconds': 5,
        'allow_expiry_day_trades': False,
    },
}


def _chain(spot: float = SPOT) -> ChainSnapshot:
    interval = 50
    atm = round(spot / interval) * interval
    quotes = []
    for offset in (-100, -50, 0, 50, 100):
        strike = atm + offset
        quotes.append(ChainQuote(strike=strike, option_type='CE', bid=80.0, ask=82.0, last=81.0, iv=0.15))
        quotes.append(ChainQuote(strike=strike, option_type='PE', bid=75.0, ask=77.0, last=76.0, iv=0.15))
    return ChainSnapshot(timestamp=TS, underlying='NIFTY', spot=spot, expiry=EXPIRY, quotes=quotes, atm_iv=0.15)


def _intent(direction: str = DIRECTION_BULLISH, structure: str = STRUCTURE_LONG_OPTION) -> OptionIntent:
    return OptionIntent(
        strategy_name='VWAP_PULLBACK',
        instrument='NIFTY',
        timestamp=TS,
        direction=direction,
        preferred_structure=structure,
        underlying_entry=SPOT,
        underlying_stop=SPOT - 100,
        underlying_target=SPOT + 200,
        max_hold_minutes=60,
    )


translator = OptionsTranslationLayer()


# ─── long option ─────────────────────────────────────────────────────────────

def test_bullish_returns_buy_ce():
    sig = translator.translate(_intent(DIRECTION_BULLISH), _chain(), BASE_CFG)
    assert sig is not None
    assert isinstance(sig, MultiLegSignal)
    assert len(sig.legs) == 1
    assert sig.legs[0].option_type == 'CE'
    assert sig.legs[0].side == 'BUY'


def test_bearish_returns_buy_pe():
    sig = translator.translate(_intent(DIRECTION_BEARISH), _chain(), BASE_CFG)
    assert sig is not None
    assert sig.legs[0].option_type == 'PE'
    assert sig.legs[0].side == 'BUY'


def test_structure_type_is_long_option():
    sig = translator.translate(_intent(), _chain(), BASE_CFG)
    assert sig.structure_type == STRUCTURE_LONG_OPTION


def test_strategy_name_preserved():
    sig = translator.translate(_intent(), _chain(), BASE_CFG)
    assert sig.strategy_name == 'VWAP_PULLBACK'


def test_metadata_contains_direction_and_strike():
    sig = translator.translate(_intent(DIRECTION_BULLISH), _chain(), BASE_CFG)
    assert sig.metadata['direction'] == DIRECTION_BULLISH
    assert 'strike' in sig.metadata
    assert 'expiry' in sig.metadata


def test_disabled_long_option_returns_none():
    cfg = {
        'options': {**BASE_CFG['options'], 'structures_enabled': {'long_option': False}},
    }
    sig = translator.translate(_intent(), _chain(), cfg)
    assert sig is None


def test_no_chain_returns_none_gracefully():
    # Translation layer should not be called without a chain, but handle it safely
    # by testing that when selector rejects (bad quotes), we get None
    from src.core.option_models import ChainQuote
    bad_quotes = [ChainQuote(strike=22_500.0, option_type='CE', bid=0.0, ask=0.0, last=0.0, iv=0.15),
                  ChainQuote(strike=22_450.0, option_type='CE', bid=0.0, ask=0.0, last=0.0, iv=0.15),
                  ChainQuote(strike=22_500.0, option_type='PE', bid=0.0, ask=0.0, last=0.0, iv=0.15)]
    bad_chain = ChainSnapshot(timestamp=TS, underlying='NIFTY', spot=SPOT, expiry=EXPIRY, quotes=bad_quotes, atm_iv=0.15)
    sig = translator.translate(_intent(), bad_chain, BASE_CFG)
    assert sig is None


# ─── debit spread ────────────────────────────────────────────────────────────

def test_debit_spread_disabled_returns_none():
    sig = translator.translate(_intent(structure=STRUCTURE_DEBIT_SPREAD), _chain(), BASE_CFG)
    assert sig is None  # disabled in BASE_CFG


def test_debit_spread_enabled_bullish_creates_two_legs():
    cfg = {
        'options': {**BASE_CFG['options'], 'structures_enabled': {'long_option': True, 'debit_spread': True}},
        'debit_spread': {'short_leg_distance_strikes': 2},
    }
    sig = translator.translate(_intent(DIRECTION_BULLISH, STRUCTURE_DEBIT_SPREAD), _chain(), cfg)
    assert sig is not None
    assert len(sig.legs) == 2
    buy_legs  = [l for l in sig.legs if l.side == 'BUY']
    sell_legs = [l for l in sig.legs if l.side == 'SELL']
    assert len(buy_legs) == 1
    assert len(sell_legs) == 1
    assert buy_legs[0].option_type == 'CE'
    assert sell_legs[0].option_type == 'CE'
    # Short leg must be OTM (higher strike for CE)
    assert sell_legs[0].strike > buy_legs[0].strike


def test_debit_spread_enabled_bearish_creates_two_legs():
    cfg = {
        'options': {**BASE_CFG['options'], 'structures_enabled': {'long_option': True, 'debit_spread': True}},
        'debit_spread': {'short_leg_distance_strikes': 2},
    }
    sig = translator.translate(_intent(DIRECTION_BEARISH, STRUCTURE_DEBIT_SPREAD), _chain(), cfg)
    assert sig is not None
    assert len(sig.legs) == 2
    buy_legs  = [l for l in sig.legs if l.side == 'BUY']
    sell_legs = [l for l in sig.legs if l.side == 'SELL']
    assert buy_legs[0].option_type == 'PE'
    assert sell_legs[0].option_type == 'PE'
    # Short leg must be OTM (lower strike for PE)
    assert sell_legs[0].strike < buy_legs[0].strike


# ─── unknown structure ───────────────────────────────────────────────────────

def test_unknown_structure_returns_none():
    intent = OptionIntent(
        strategy_name='TEST', instrument='NIFTY', timestamp=TS,
        direction=DIRECTION_BULLISH, preferred_structure='UNKNOWN_STRUCT',
        underlying_entry=SPOT, underlying_stop=None, underlying_target=None, max_hold_minutes=60,
    )
    sig = translator.translate(intent, _chain(), BASE_CFG)
    assert sig is None
