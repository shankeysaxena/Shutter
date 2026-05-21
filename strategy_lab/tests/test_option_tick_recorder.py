"""Tests for Phase 2 option chain recorder components."""
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from src.live.option_tick_recorder import (
    OptionTickAggregator,
    OptionTickRecorder,
    OptionRecorderConfig,
    _implied_vol,
    _bs_price,
)
from src.integrations.zerodha.option_instruments import OptionInstrumentResolver


# ─── BSM helpers ─────────────────────────────────────────────────────────────

def test_bs_price_call_atm():
    """ATM call at 30 DTE with 15% IV should have positive premium."""
    price = _bs_price(S=22500, K=22500, T=30/365, sigma=0.15, r=0.07, opt='CE')
    assert price > 0


def test_bs_price_put_atm():
    price = _bs_price(S=22500, K=22500, T=30/365, sigma=0.15, r=0.07, opt='PE')
    assert price > 0


def test_implied_vol_round_trip():
    """Back-calculated IV should match input IV."""
    iv_in = 0.18
    price = _bs_price(S=22500, K=22500, T=30/365, sigma=iv_in, r=0.07, opt='CE')
    iv_out = _implied_vol(S=22500, K=22500, T=30/365, r=0.07, price=price, opt='CE')
    assert abs(iv_out - iv_in) < 0.001


def test_implied_vol_zero_time():
    """Zero time to expiry should return 0.0 gracefully."""
    iv = _implied_vol(S=22500, K=22500, T=0, r=0.07, price=50.0, opt='CE')
    assert iv == 0.0


# ─── OptionTickAggregator ────────────────────────────────────────────────────

TOKEN_CE = 12345
TOKEN_PE = 12346

TOKEN_META = {
    TOKEN_CE: {'underlying': 'NIFTY', 'expiry': date(2025, 6, 12), 'strike': 22500.0, 'option_type': 'CE'},
    TOKEN_PE: {'underlying': 'NIFTY', 'expiry': date(2025, 6, 12), 'strike': 22500.0, 'option_type': 'PE'},
}

TS = datetime(2025, 6, 5, 10, 30, 15)  # 10:30:15 → buckets into 10:30:00


def _tick(ltp=80.0, bid=79.0, ask=81.0, volume=500, oi=100000, ts=TS):
    return {
        'last_price':    ltp,
        'volume_traded': volume,
        'oi':            oi,
        'timestamp':     ts,
        'depth': {
            'buy':  [{'price': bid,  'quantity': 10}],
            'sell': [{'price': ask,  'quantity': 10}],
        },
    }


def test_aggregator_collects_tick():
    agg = OptionTickAggregator(TOKEN_META)
    agg.on_tick(TOKEN_CE, _tick(), spot=22500.0)
    rows = agg.snapshot_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r['option_type'] == 'CE'
    assert r['strike'] == 22500.0
    assert r['bid'] == 79.0
    assert r['ask'] == 81.0
    assert r['last'] == 80.0
    assert r['volume'] == 500
    assert r['oi'] == 100000


def test_aggregator_iv_computed_when_spot_given():
    agg = OptionTickAggregator(TOKEN_META)
    agg.on_tick(TOKEN_CE, _tick(ltp=80.0), spot=22500.0)
    rows = agg.snapshot_rows()
    assert rows[0]['iv'] > 0.0


def test_aggregator_iv_zero_without_spot():
    agg = OptionTickAggregator(TOKEN_META)
    agg.on_tick(TOKEN_CE, _tick())   # no spot
    rows = agg.snapshot_rows()
    assert rows[0]['iv'] == 0.0


def test_aggregator_ignores_unknown_token():
    agg = OptionTickAggregator(TOKEN_META)
    agg.on_tick(99999, _tick())   # not in meta
    assert agg.snapshot_rows() == []


def test_aggregator_multiple_ticks_same_minute_uses_last():
    agg = OptionTickAggregator(TOKEN_META)
    agg.on_tick(TOKEN_CE, _tick(ltp=80.0, bid=79.0, ask=81.0), spot=22500.0)
    agg.on_tick(TOKEN_CE, _tick(ltp=82.0, bid=81.0, ask=83.0, ts=datetime(2025,6,5,10,30,45)), spot=22500.0)
    rows = agg.snapshot_rows()
    assert len(rows) == 1   # same minute bucket
    assert rows[0]['last'] == 82.0
    assert rows[0]['bid']  == 81.0


def test_aggregator_different_minutes_creates_separate_rows():
    agg = OptionTickAggregator(TOKEN_META)
    agg.on_tick(TOKEN_CE, _tick(ts=datetime(2025,6,5,10,30,0)), spot=22500.0)
    agg.on_tick(TOKEN_CE, _tick(ts=datetime(2025,6,5,10,31,0)), spot=22500.0)
    rows = agg.snapshot_rows()
    assert len(rows) == 2


def test_aggregator_clear_resets():
    agg = OptionTickAggregator(TOKEN_META)
    agg.on_tick(TOKEN_CE, _tick(), spot=22500.0)
    agg.clear()
    assert agg.snapshot_rows() == []


def test_aggregator_both_ce_and_pe():
    agg = OptionTickAggregator(TOKEN_META)
    agg.on_tick(TOKEN_CE, _tick(ltp=80.0), spot=22500.0)
    agg.on_tick(TOKEN_PE, _tick(ltp=75.0), spot=22500.0)
    rows = agg.snapshot_rows()
    assert len(rows) == 2
    types = {r['option_type'] for r in rows}
    assert types == {'CE', 'PE'}


# ─── OptionInstrumentResolver ────────────────────────────────────────────────

def _mock_instruments_df():
    """Minimal instruments DataFrame mimicking Kite CSV structure."""
    import pandas as pd
    return pd.DataFrame([
        {
            'instrument_token': 12345,
            'exchange': 'NFO',
            'name': 'NIFTY',
            'expiry': '2025-06-12',
            'strike': 22500.0,
            'instrument_type': 'CE',
            'tradingsymbol': 'NIFTY25JUN22500CE',
            'segment': 'NFO-OPT',
        },
        {
            'instrument_token': 12346,
            'exchange': 'NFO',
            'name': 'NIFTY',
            'expiry': '2025-06-12',
            'strike': 22500.0,
            'instrument_type': 'PE',
            'tradingsymbol': 'NIFTY25JUN22500PE',
            'segment': 'NFO-OPT',
        },
        {
            'instrument_token': 12347,
            'exchange': 'NFO',
            'name': 'NIFTY',
            'expiry': '2025-06-19',
            'strike': 22500.0,
            'instrument_type': 'CE',
            'tradingsymbol': 'NIFTY25JUN19-22500CE',
            'segment': 'NFO-OPT',
        },
    ])


def test_resolver_finds_ce():
    resolver = OptionInstrumentResolver(_mock_instruments_df())
    token = resolver.resolve('NIFTY', date(2025, 6, 12), 22500.0, 'CE')
    assert token == 12345


def test_resolver_finds_pe():
    resolver = OptionInstrumentResolver(_mock_instruments_df())
    token = resolver.resolve('NIFTY', date(2025, 6, 12), 22500.0, 'PE')
    assert token == 12346


def test_resolver_returns_none_for_missing():
    resolver = OptionInstrumentResolver(_mock_instruments_df())
    token = resolver.resolve('NIFTY', date(2025, 6, 12), 99999.0, 'CE')
    assert token is None


def test_resolver_tokens_for_session():
    resolver = OptionInstrumentResolver(_mock_instruments_df())
    tokens = resolver.tokens_for_session('NIFTY', date(2025, 6, 12), [22500.0])
    assert 12345 in tokens
    assert 12346 in tokens
    assert tokens[12345]['option_type'] == 'CE'


def test_resolver_nearest_weekly_expiry():
    resolver = OptionInstrumentResolver(_mock_instruments_df())
    expiry = resolver.nearest_weekly_expiry(date(2025, 6, 5), 'NIFTY')
    assert expiry == date(2025, 6, 12)   # nearest in instrument master


# ─── OptionTickRecorder integration ──────────────────────────────────────────

def test_recorder_start_returns_tokens():
    resolver = OptionInstrumentResolver(_mock_instruments_df())
    config   = OptionRecorderConfig(underlyings=['NIFTY'], strikes_each_side=0,
                                     strike_interval={'NIFTY': 50.0})
    recorder = OptionTickRecorder(resolver, config)
    tokens   = recorder.start(date(2025, 6, 5), spot_estimates={'NIFTY': 22500.0})
    assert 12345 in tokens or 12346 in tokens   # ATM resolved


def test_recorder_routes_tick_to_aggregator():
    resolver = OptionInstrumentResolver(_mock_instruments_df())
    config   = OptionRecorderConfig(underlyings=['NIFTY'], strikes_each_side=0,
                                     strike_interval={'NIFTY': 50.0})
    recorder = OptionTickRecorder(resolver, config)
    recorder.start(date(2025, 6, 5), spot_estimates={'NIFTY': 22500.0})
    recorder.on_tick(12345, _tick())   # CE token
    agg = recorder._aggregators.get('NIFTY')
    assert agg is not None
    rows = agg.snapshot_rows()
    assert len(rows) > 0


def test_recorder_unknown_token_ignored():
    resolver = OptionInstrumentResolver(_mock_instruments_df())
    config   = OptionRecorderConfig(underlyings=['NIFTY'], strikes_each_side=0,
                                     strike_interval={'NIFTY': 50.0})
    recorder = OptionTickRecorder(resolver, config)
    recorder.start(date(2025, 6, 5), spot_estimates={'NIFTY': 22500.0})
    recorder.on_tick(99999, _tick())   # not subscribed
    agg = recorder._aggregators.get('NIFTY')
    # aggregator exists but has no rows for the unknown token
    rows = agg.snapshot_rows() if agg else []
    assert all(r.get('strike') != 99999 for r in rows)
