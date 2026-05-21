"""Tests for VWAPReversionStrategy and IntradayATRFeature."""
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from src.core.enums import RejectionReason
from src.core.models import (
    BarEvent, Candle, EngineState, FeatureSnapshot, StrategyContext
)
from src.features.atr import IntradayATRFeature
from src.strategies.vwap_reversion import VWAPReversionStrategy


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_CONFIG = {
    'vwap_reversion': {
        'enabled': True,
        'stretch_threshold_atr': 1.5,
        'min_bars_stretched': 2,
        'min_reversal_ratio': 0.15,
        'max_stretch_bars': 30,
        'stop_buffer_pct': 0.001,
        'no_entry_after': '13:30',
        'max_trades_per_day': 2,
    }
}

_SESSION = date(2026, 5, 14)


def _bar(ts_str, close, vwap, vwap_atr_dist=0.0, intraday_atr=10.0,
          high=None, low=None):
    ts = datetime.strptime(f'2026-05-14 {ts_str}', '%Y-%m-%d %H:%M')
    high = high if high is not None else close + 2
    low  = low  if low  is not None else close - 2
    candle = Candle(ts, 'NIFTY', close, high, low, close, 1000)
    features = FeatureSnapshot(
        session_date=_SESSION, minute_index=0, prior_close=vwap,
        vwap=vwap, vwap_distance=(close - vwap) / vwap,
        above_vwap=close > vwap, below_vwap=close < vwap,
        or_high=vwap + 50, or_low=vwap - 50, or_width=0.005, or_ready=True,
        gap_pct=0.002, gap_direction='UP',
        session_high_so_far=close + 5, session_low_so_far=close - 5,
        intraday_atr=intraday_atr,
        vwap_atr_distance=vwap_atr_dist,
    )
    return BarEvent(candle=candle, features=features, is_bar_closed=True,
                     runtime_mode='backtest')


def _ctx(bar_event, config=None):
    state = EngineState(
        instrument='NIFTY', session_date=_SESSION,
        open_trades=[], closed_trades=[], queued_signals=[],
        per_strategy_day_trade_count={'VWAP_REVERSION': 0},
    )
    return StrategyContext(bar_event=bar_event, engine_state=state,
                            strategy_config=config or _CONFIG)


# ─────────────────────────────────────────────────────────────────────────────
# IntradayATRFeature
# ─────────────────────────────────────────────────────────────────────────────

class TestIntradayATRFeature:
    def _make_session(self, n=30):
        ts = [datetime(2026, 5, 14, 9, 15) + timedelta(minutes=i) for i in range(n)]
        return pd.DataFrame({
            'timestamp': ts,
            'open':  [100.0] * n,
            'high':  [102.0 + i * 0.1 for i in range(n)],
            'low':   [98.0  - i * 0.1 for i in range(n)],
            'close': [101.0] * n,
            'vwap':  [100.5] * n,
            'volume': [1000] * n,
        })

    def test_adds_atr_and_vwap_atr_columns(self):
        df = IntradayATRFeature(period=14).calculate(self._make_session(30))
        assert 'intraday_atr' in df.columns
        assert 'vwap_atr_distance' in df.columns

    def test_atr_nan_before_warmup(self):
        df = IntradayATRFeature(period=14).calculate(self._make_session(30))
        assert df['intraday_atr'].iloc[:13].isna().all()

    def test_atr_positive_after_warmup(self):
        df = IntradayATRFeature(period=14).calculate(self._make_session(30))
        assert (df['intraday_atr'].dropna() > 0).all()

    def test_vwap_atr_distance_sign(self):
        df = self._make_session(30)
        df['vwap'] = 101.5   # close=101.0 → below VWAP → distance negative
        result = IntradayATRFeature(period=14).calculate(df)
        warm = result['vwap_atr_distance'].dropna()
        assert (warm < 0).all()

    def test_empty_session_returns_unchanged(self):
        df = IntradayATRFeature(period=14).calculate(pd.DataFrame())
        assert df.empty


# ─────────────────────────────────────────────────────────────────────────────
# VWAPReversionStrategy — entry conditions
# ─────────────────────────────────────────────────────────────────────────────

class TestVWAPReversionEntry:
    def _strategy(self):
        return VWAPReversionStrategy()

    def test_no_signal_when_disabled(self):
        s = self._strategy()
        cfg = {'vwap_reversion': {'enabled': False}}
        ctx = _ctx(_bar('10:00', 22000, 22100, vwap_atr_dist=-2.0), config=cfg)
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.DISABLED

    def test_no_signal_when_atr_not_warm(self):
        s = self._strategy()
        bar = _bar('10:00', 22000, 22100, vwap_atr_dist=None, intraday_atr=None)
        bar.features.vwap_atr_distance = None
        bar.features.intraday_atr = None
        ctx = _ctx(bar)
        assert s.generate_signal(ctx) is None
        assert s.explain_no_signal(ctx) == RejectionReason.ATR_NOT_WARM

    def test_no_signal_insufficient_stretch(self):
        """Only 1.0 ATR from VWAP — below threshold of 1.5."""
        s = self._strategy()
        ctx = _ctx(_bar('10:00', 22000, 22100, vwap_atr_dist=-1.0))
        assert s.generate_signal(ctx) is None

    def test_no_signal_after_cutoff(self):
        s = self._strategy()
        ctx = _ctx(_bar('13:30', 22000, 22100, vwap_atr_dist=-2.0))
        assert s.generate_signal(ctx) is None

    def test_enters_stretched_state_on_large_deviation(self):
        """First bar above threshold: IDLE→STRETCHED, no signal yet."""
        s = self._strategy()
        ctx = _ctx(_bar('10:00', 22000, 22200, vwap_atr_dist=-2.0))
        sig = s.generate_signal(ctx)
        assert sig is None   # no signal on first bar (min_bars_stretched=2)
        st = s._state('NIFTY', _SESSION)
        assert st.phase == 'STRETCHED'
        assert st.direction == 'LONG'

    def test_long_signal_after_reversal(self):
        """Price stretched 2 ATR below VWAP, then retraces — long signal (hybrid_atr default)."""
        s = self._strategy()
        s.generate_signal(_ctx(_bar('10:00', 22000, 22200, vwap_atr_dist=-2.0)))
        s.generate_signal(_ctx(_bar('10:01', 22020, 22200, vwap_atr_dist=-1.8)))
        sig = s.generate_signal(_ctx(_bar('10:02', 22050, 22200, vwap_atr_dist=-1.5)))

        assert sig is not None
        assert sig.direction == 'LONG'
        # Target is always VWAP (unchanged across stop modes)
        assert sig.target_price == 22200
        assert sig.metadata.get('stop_mode') == 'hybrid_atr'
        # Hybrid LONG: max(ATR_stop, extreme_stop)
        # ATR_stop = 22050 - 1.0×10 = 22040
        # extreme = min(bar.low) seen ≈ 21998 → extreme_stop ≈ 21998 - buffer
        # Hybrid = max(22040, ~21980) = 22040 (ATR is tighter)
        assert sig.stop_price >= 22030

    def test_short_signal_after_reversal(self):
        """Price stretched above VWAP, then retraces — short signal (hybrid_atr default)."""
        s = self._strategy()
        vwap = 22000.0
        s.generate_signal(_ctx(_bar('10:00', 22300, vwap, vwap_atr_dist=2.0, high=22305)))
        s.generate_signal(_ctx(_bar('10:01', 22280, vwap, vwap_atr_dist=1.8, high=22285)))
        sig = s.generate_signal(_ctx(_bar('10:02', 22250, vwap, vwap_atr_dist=1.5, high=22255)))

        assert sig is not None
        assert sig.direction == 'SHORT'
        assert sig.target_price == vwap
        assert sig.metadata.get('stop_mode') == 'hybrid_atr'
        # Hybrid SHORT: min(ATR_stop, extreme_stop)
        # ATR_stop = 22250 + 10 = 22260; extreme = 22305+buffer → hybrid = 22260
        assert sig.stop_price <= 22265

    def test_no_signal_on_first_stretched_bar(self):
        """The VERY FIRST bar entering the stretch never fires — state just transitions."""
        s = self._strategy()
        # Bar 1: transitions IDLE → STRETCHED. Should not signal (count = 1 < min_bars=2)
        sig = s.generate_signal(_ctx(_bar('10:00', 22000, 22200, vwap_atr_dist=-2.0)))
        assert sig is None
        assert s._state('NIFTY', _SESSION).phase == 'STRETCHED'
        assert s._state('NIFTY', _SESSION).stretch_bar_count == 1

    def test_used_state_blocks_second_trade_same_direction(self):
        """Once USED, strategy produces no more signals for the session."""
        s = self._strategy()
        vwap = 22200.0
        s.generate_signal(_ctx(_bar('10:00', 22000, vwap, vwap_atr_dist=-2.0)))
        s.generate_signal(_ctx(_bar('10:01', 22020, vwap, vwap_atr_dist=-1.8)))
        sig = s.generate_signal(_ctx(_bar('10:02', 22050, vwap, vwap_atr_dist=-1.5)))
        assert sig is not None   # first signal fires

        # Now try another stretch — should return None (USED)
        sig2 = s.generate_signal(_ctx(_bar('10:10', 22000, vwap, vwap_atr_dist=-2.5)))
        assert sig2 is None

    def test_invalidates_when_stretch_persists_too_long(self):
        """After max_stretch_bars bars, state resets to IDLE."""
        s = self._strategy()
        # Start a stretch
        s.generate_signal(_ctx(_bar('10:00', 22000, 22200, vwap_atr_dist=-2.0)))
        st = s._state('NIFTY', _SESSION)
        assert st.phase == 'STRETCHED'

        # Push past max_stretch_bars (default 30)
        for i in range(1, 31):
            ts = f"10:{i:02d}"
            s.generate_signal(_ctx(_bar(ts, 22000, 22200, vwap_atr_dist=-2.0)))

        assert st.phase == 'IDLE'

    def test_reset_clears_state(self):
        s = self._strategy()
        s.generate_signal(_ctx(_bar('10:00', 22000, 22200, vwap_atr_dist=-2.0)))
        assert s._state('NIFTY', _SESSION).phase == 'STRETCHED'
        s.reset()
        assert len(s._states) == 0

    def test_metadata_contains_expected_keys(self):
        s = self._strategy()
        vwap = 22200.0
        s.generate_signal(_ctx(_bar('10:00', 22000, vwap, vwap_atr_dist=-2.0)))
        s.generate_signal(_ctx(_bar('10:01', 22020, vwap, vwap_atr_dist=-1.8)))
        sig = s.generate_signal(_ctx(_bar('10:02', 22050, vwap, vwap_atr_dist=-1.5)))
        assert sig is not None
        for key in ('vwap_at_signal', 'peak_distance_atr', 'stretch_bars',
                     'retraced_ratio', 'stretch_extreme'):
            assert key in sig.metadata

    def test_hybrid_atr_stop_bounds(self):
        """Hybrid stop = tighter of (ATR stop, stretch extreme stop)."""
        s = self._strategy()
        vwap = 22150.0
        # Very deep stretch — extreme is far below entry
        s.generate_signal(_ctx(_bar('10:00', 21900, vwap, vwap_atr_dist=-2.5)))
        s.generate_signal(_ctx(_bar('10:01', 21920, vwap, vwap_atr_dist=-2.2)))
        sig = s.generate_signal(_ctx(_bar('10:02', 21960, vwap, vwap_atr_dist=-1.8)))
        assert sig is not None
        # Target is VWAP (unchanged)
        assert sig.target_price == vwap
        # ATR stop = 21960 - 1.0×10 = 21950; extreme ≈ 21898
        # Hybrid = max(21950, 21898-buffer) = 21950 (ATR is tighter)
        assert abs(sig.stop_price - (21960 - 10.0)) < 2.0

    def test_stretch_extreme_mode_target_is_vwap(self):
        """With stop_type=stretch_extreme, target_price is explicit VWAP."""
        cfg = {
            'vwap_reversion': {
                **_CONFIG['vwap_reversion'],
                'stop_type': 'stretch_extreme',
            }
        }
        s = self._strategy()
        vwap = 22150.0
        s.generate_signal(_ctx(_bar('10:00', 21900, vwap, vwap_atr_dist=-2.5), config=cfg))
        s.generate_signal(_ctx(_bar('10:01', 21920, vwap, vwap_atr_dist=-2.2), config=cfg))
        sig = s.generate_signal(_ctx(_bar('10:02', 21960, vwap, vwap_atr_dist=-1.8), config=cfg))
        assert sig is not None
        assert sig.target_price == vwap


# ─────────────────────────────────────────────────────────────────────────────
# Simulator honours explicit target_price
# ─────────────────────────────────────────────────────────────────────────────

class TestSimulatorExplicitTarget:
    def test_explicit_target_used_when_nonzero(self):
        from src.execution.simulator import BacktestSimulator
        from src.core.models import Signal

        sim = BacktestSimulator(slippage_per_side=0, brokerage=0)
        sig = Signal(
            strategy_name='VWAP_REVERSION', instrument='NIFTY',
            timestamp=datetime(2026, 5, 14, 10, 0),
            direction='LONG', entry_type='MARKET',
            stop_price=21800.0,
            target_price=22200.0,   # explicit VWAP target
            metadata={},
        )
        bar = _bar('10:01', 22000, 22200, vwap_atr_dist=-1.5)
        bar.candle.open = 22010.0
        trade = sim.process_signal_for_entry(sig, bar, qty=25)
        assert trade.target_price == 22200.0   # must match explicit target

    def test_zero_target_falls_back_to_target_r(self):
        from src.execution.simulator import BacktestSimulator
        from src.core.models import Signal

        sim = BacktestSimulator(slippage_per_side=0, brokerage=0)
        sig = Signal(
            strategy_name='ORB', instrument='NIFTY',
            timestamp=datetime(2026, 5, 14, 10, 0),
            direction='LONG', entry_type='MARKET',
            stop_price=21800.0,
            target_price=0.0,        # placeholder — let simulator compute
            metadata={'target_r': 2.0},
        )
        bar = _bar('10:01', 22000, 22200, vwap_atr_dist=-1.5)
        bar.candle.open = 22000.0
        trade = sim.process_signal_for_entry(sig, bar, qty=25)
        # risk = 22000 - 21800 = 200, target = 22000 + 200*2 = 22400
        assert trade.target_price == 22400.0
