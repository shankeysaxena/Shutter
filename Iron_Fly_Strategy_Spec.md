# Intraday Anchored Iron Fly — Implementation Specification

**Status:** Spec / pre-implementation
**Depends on design doc:** `Iron_Fly_Strategy_Design.md`
**Target lab phase:** Phase 4.6 (new — option chain infrastructure required)
**Date:** 2026-05-18

---

## 0. Scope of this document

This spec is the implementation contract. It defines: file layout, data-model extensions, config schema, entry/exit algorithms in pseudocode, new `RejectionReason` constants, new features and feeds required, the multi-leg execution model, and acceptance criteria.

What this spec does **not** cover:
- Implementation of the option-chain *data source* (broker API integration) — that is downstream of Phase 5
- Live execution (only paper / backtest / replay in scope here)
- Risk management beyond per-trade max loss (portfolio-level risk is out of scope)

## 1. Decisions taken on open questions

The design doc flagged five open questions. Defaults chosen here; all are config-overridable and intended to be swept during validation.

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | Wing width W: fixed points / fixed % / dynamic | **% of spot, default 0.5%** | Scales naturally across NIFTY (~25k) and BANKNIFTY (~55k); ATR-based deferred to v2 |
| 2 | Profit target band | **Single tier, 40% of max profit** | Scaled exit deferred to v2 to keep first impl reviewable |
| 3 | DTE selection | **Allow {0, 1, 2} DTE, but reject 0DTE on event-day blacklist** | 0DTE has best theta but most gamma; event-day filter is the conservative middle path |
| 4 | Re-entry per day | **One trade per underlying per day** | Re-entry after no-progress exit risks doubling down on a structurally bad day |
| 5 | Touch exit granularity | **Bar-close** (matches existing lab convention) | Intra-bar exits require execution-model rework; logged as v2 enhancement |

## 2. File layout (new and modified)

### 2.1 New files

```
strategy_lab/src/
├── strategies/
│   └── iron_fly.py                  # New strategy class
├── features/
│   └── iv_regime.py                 # ATM IV trailing percentile feature
├── feeds/
│   └── option_chain_snapshot.py     # Option chain feed (interface + synthetic impl)
├── core/
│   └── option_models.py             # OptionLeg, MultiLegSignal, MultiLegTrade, ChainSnapshot
└── execution/
    └── multi_leg_simulator.py       # Leg-by-leg fill simulator with realistic delays

strategy_lab/tests/strategies/
└── test_iron_fly.py                 # Unit + integration tests

strategy_lab/config/
└── iron_fly.yaml                    # Standalone config (additive to base.yaml)
```

### 2.2 Modified files

| File | Change |
|---|---|
| `src/core/enums.py` | Add new `RejectionReason` constants (see §6) |
| `src/core/models.py` | Add option-aware models (or import from `option_models.py`) |
| `src/core/engine.py` | BarEngine must accept and route `ChainSnapshot` alongside bar events |
| `src/strategies/base.py` | Extend `StrategyContext` to optionally carry `chain_snapshot` |
| `src/backtest/simulator.py` | Multi-leg routing path (delegates to `multi_leg_simulator`) |
| `src/runtimes/backtest.py` | Wire option-chain feed into runtime |
| `src/runtimes/replay.py` | Same |
| `config/base.yaml` | Add `iron_fly` strategy stub (disabled by default) |

## 3. Data model extensions

### 3.1 Option-aware models

```python
# src/core/option_models.py

@dataclass
class OptionLeg:
    instrument: str          # e.g. "NIFTY"
    expiry: date
    strike: float
    option_type: str         # "CE" | "PE"
    side: str                # "BUY" | "SELL"
    qty: int                 # in lots (sign carried by side, not qty)

@dataclass
class ChainQuote:
    strike: float
    option_type: str         # "CE" | "PE"
    bid: float
    ask: float
    last: float
    iv: float                # implied vol, decimal (0.15 = 15%)

@dataclass
class ChainSnapshot:
    timestamp: datetime
    underlying: str
    spot: float
    expiry: date
    quotes: List[ChainQuote]
    atm_iv: float            # Convenience: IV of nearest ATM strike

@dataclass
class MultiLegSignal:
    strategy_name: str
    instrument: str          # underlying
    timestamp: datetime
    structure_type: str      # "IRON_FLY"
    legs: List[OptionLeg]
    metadata: Dict           # entry context (spot, IV, ATM strike, wing W, etc.)

@dataclass
class LegFill:
    leg: OptionLeg
    fill_price: float
    fill_time: datetime

@dataclass
class MultiLegTrade:
    trade_id: str
    strategy_name: str
    instrument: str
    structure_type: str
    entry_time: datetime
    entry_fills: List[LegFill]
    net_entry_credit: float          # positive = credit received
    exit_time: Optional[datetime]
    exit_fills: List[LegFill]
    net_exit_debit: Optional[float]  # positive = debit paid to close
    exit_reason: Optional[str]
    gross_pnl: Optional[float]
    net_pnl: Optional[float]
    runtime_mode: str
    metadata: Dict
```

### 3.2 `StrategyContext` extension

```python
@dataclass
class StrategyContext:
    bar_event: BarEvent
    engine_state: EngineState
    strategy_config: Dict
    chain_snapshot: Optional[ChainSnapshot] = None  # NEW — None for non-options strategies
```

Existing strategies are unaffected because they ignore `chain_snapshot`.

### 3.3 `EngineState` extension

```python
@dataclass
class EngineState:
    ...
    open_multi_leg_trades: List[MultiLegTrade] = field(default_factory=list)
    closed_multi_leg_trades: List[MultiLegTrade] = field(default_factory=list)
```

Single-leg `Trade` lists remain for existing strategies. Two parallel ledgers, joined at the analytics layer.

## 4. Config schema

```yaml
# config/iron_fly.yaml
# Merged additively into base.yaml under strategies.iron_fly

strategies:
  iron_fly:
    enabled: true

    # --- Universe ---
    underlyings: [NIFTY, BANKNIFTY]
    allowed_dte: [0, 1, 2]
    event_day_blacklist_0dte: true        # if today is on event_days list, reject 0DTE
    event_days_file: data/event_days.csv  # one ISO date per line

    # --- Entry time window ---
    entry_window_start: "09:45"
    entry_window_end: "13:30"

    # --- Entry filters ---
    trend_filter:
      max_vwap_distance_pct: 0.0025       # |spot - vwap| / spot < 0.25%
    range_filter:
      or_width_lookback_days: 20
      max_or_width_vs_median: 1.0         # OR width < 1.0 × N-day median
    iv_regime_filter:
      lookback_days: 60
      min_percentile: 0.25
      max_percentile: 0.75
    liquidity_filter:
      max_atm_spread_pct: 0.02            # (ask - bid) / mid < 2% on ATM strikes
      require_two_sided_wings: true

    # --- Structure ---
    wing_width_pct_of_spot: 0.005         # W = 0.5% of spot, rounded to nearest strike interval
    strike_interval:                       # used to snap ATM and wings to tradable strikes
      NIFTY: 50
      BANKNIFTY: 100

    # --- Sizing ---
    risk_per_trade_pct: 0.005             # 0.5% of capital per trade
    capital: 1000000                      # ₹10L default for backtest
    max_lots_per_trade: 10                # safety cap

    # --- Exits (ordered by priority — first to fire wins) ---
    exits:
      touch_exit:
        enabled: true
        # touches counted on bar close (intra-bar deferred to v2)
      no_progress:
        enabled: true
        checkpoints:
          - { offset_minutes: 45, min_profit_pct_of_max: 0.10 }
          - { offset_minutes: 90, min_profit_pct_of_max: 0.25 }
      profit_target:
        enabled: true
        pct_of_max_profit: 0.40
      vol_expansion:
        enabled: true
        premium_multiple_threshold: 1.3   # current_short_premium / entry_short_premium
        max_spot_move_pct: 0.005          # only fire if spot moved < 0.5%; else touch_exit handles it
      hard_time_stop: "15:15"

    # --- Operational ---
    one_trade_per_underlying_per_day: true
```

## 5. Strategy state machine

A single Iron Fly instance carries per-underlying state across bars of the same session, reset at session boundary via `reset()`.

```
                         ┌────────────────┐
   (session start) ────► │     IDLE       │
                         └───────┬────────┘
                                 │ entry filters pass
                                 │ + chain snapshot valid
                                 ▼
                         ┌────────────────┐
                         │   ENTERING     │ (signal emitted; awaiting fills)
                         └───────┬────────┘
                                 │ all 4 legs filled
                                 ▼
                         ┌────────────────┐
                         │     OPEN       │ ◄─── re-evaluate each bar:
                         └───────┬────────┘      check exit layers in order
                                 │ any exit fires
                                 ▼
                         ┌────────────────┐
                         │    EXITING     │ (closing signal emitted)
                         └───────┬────────┘
                                 │ all 4 legs closed
                                 ▼
                         ┌────────────────┐
                         │     DONE       │ (terminal for the day if one_trade_per_day=true)
                         └────────────────┘
```

Per-underlying state struct (held on the strategy instance):

```python
@dataclass
class IronFlyState:
    phase: str                              # IDLE | ENTERING | OPEN | EXITING | DONE
    entry_time: Optional[datetime]
    entry_bar_index: Optional[int]
    entry_chain_snapshot: Optional[ChainSnapshot]
    entry_short_premium: Optional[float]    # sum of short-leg credits at entry
    entry_max_profit: Optional[float]       # net credit received
    entry_max_loss: Optional[float]         # wing_width - net_credit
    short_call_strike: Optional[float]
    short_put_strike: Optional[float]
    open_trade_ref: Optional[MultiLegTrade]
```

`reset()` clears this back to `phase=IDLE`.

## 6. Entry algorithm

```python
def generate_signal(ctx: StrategyContext) -> Optional[MultiLegSignal]:
    cfg = ctx.strategy_config['iron_fly']
    state = self._state[ctx.bar_event.candle.instrument]
    bar = ctx.bar_event.candle
    chain = ctx.chain_snapshot

    # ── Phase guards
    if state.phase != "IDLE": return None
    if not cfg['enabled']: return None
    if bar.instrument not in cfg['underlyings']: return None

    # ── Time window
    t = bar.timestamp.time()
    if t < parse_time(cfg['entry_window_start']): return None
    if t >= parse_time(cfg['entry_window_end']): return None

    # ── Chain availability
    if chain is None: return None
    if chain.expiry is None: return None

    # ── DTE filter
    dte = (chain.expiry - bar.timestamp.date()).days
    if dte not in cfg['allowed_dte']: return None
    if dte == 0 and cfg['event_day_blacklist_0dte']:
        if bar.timestamp.date() in self._event_days: return None

    # ── Trend filter
    vwap = ctx.bar_event.features.vwap
    if vwap is None: return None
    if abs(bar.close - vwap) / bar.close >= cfg['trend_filter']['max_vwap_distance_pct']:
        return None

    # ── Range filter
    or_width = ctx.bar_event.features.or_width
    if or_width is None: return None
    median_or = self._or_width_median(bar.instrument)  # cached, N-day trailing
    if median_or is None: return None
    if or_width >= cfg['range_filter']['max_or_width_vs_median'] * median_or:
        return None

    # ── IV regime filter
    iv_pct = self._iv_percentile(bar.instrument, chain.atm_iv)  # 0..1
    if iv_pct is None: return None
    if not (cfg['iv_regime_filter']['min_percentile']
            <= iv_pct <=
            cfg['iv_regime_filter']['max_percentile']):
        return None

    # ── Build structure
    atm_strike = snap_to_strike(bar.close, cfg['strike_interval'][bar.instrument])
    W = round_to_strike(bar.close * cfg['wing_width_pct_of_spot'],
                        cfg['strike_interval'][bar.instrument])
    upper_strike = atm_strike + W
    lower_strike = atm_strike - W

    # ── Liquidity filter on the four legs
    legs_quotes = lookup_quotes(chain, [
        (atm_strike, "CE"), (atm_strike, "PE"),
        (upper_strike, "CE"), (lower_strike, "PE"),
    ])
    if any(q is None for q in legs_quotes): return None
    if not passes_liquidity(legs_quotes, cfg['liquidity_filter']): return None

    # ── Construct signal
    legs = [
        OptionLeg(bar.instrument, chain.expiry, atm_strike, "CE", "SELL", qty=1),
        OptionLeg(bar.instrument, chain.expiry, atm_strike, "PE", "SELL", qty=1),
        OptionLeg(bar.instrument, chain.expiry, upper_strike, "CE", "BUY", qty=1),
        OptionLeg(bar.instrument, chain.expiry, lower_strike, "PE", "BUY", qty=1),
    ]

    # Position sizing happens in simulator using credit + W + risk budget
    return MultiLegSignal(
        strategy_name=self.name,
        instrument=bar.instrument,
        timestamp=bar.timestamp,
        structure_type="IRON_FLY",
        legs=legs,
        metadata={
            "spot": bar.close,
            "atm_strike": atm_strike,
            "wing_width": W,
            "dte": dte,
            "atm_iv": chain.atm_iv,
            "iv_percentile": iv_pct,
            "or_width": or_width,
            "vwap": vwap,
        },
    )
```

### 6.1 `explain_no_signal` — new RejectionReason constants

Add to `src/core/enums.py`:

```python
class RejectionReason:
    ...
    # Iron Fly
    CHAIN_NOT_AVAILABLE        = 'chain_not_available'
    DTE_NOT_ALLOWED            = 'dte_not_allowed'
    EVENT_DAY_0DTE_BLOCKED     = 'event_day_0dte_blocked'
    TREND_TOO_STRONG           = 'trend_too_strong'
    OR_WIDTH_TOO_WIDE          = 'or_width_too_wide'
    IV_REGIME_OUT_OF_BAND      = 'iv_regime_out_of_band'
    LIQUIDITY_INSUFFICIENT     = 'liquidity_insufficient'
    STRUCTURE_ALREADY_OPEN     = 'structure_already_open'
    DAY_DONE                   = 'day_done'
```

`explain_no_signal` mirrors `generate_signal`'s guard order, returning the first matching reason.

## 7. Exit algorithm

Evaluated once per bar while `phase == OPEN`. Layers checked in priority order; first match wins.

```python
def evaluate_exits(ctx, state, current_chain) -> Optional[str]:
    cfg = ctx.strategy_config['iron_fly']['exits']
    bar = ctx.bar_event.candle

    # ── 1. Touch exit (highest priority)
    if cfg['touch_exit']['enabled']:
        if bar.close >= state.short_call_strike: return "TOUCH_EXIT_CALL"
        if bar.close <= state.short_put_strike:  return "TOUCH_EXIT_PUT"

    # ── 2. No-progress time exit
    if cfg['no_progress']['enabled']:
        elapsed_min = (bar.timestamp - state.entry_time).total_seconds() / 60
        current_pnl = mark_to_market(state.open_trade_ref, current_chain)
        pct_of_max = current_pnl / state.entry_max_profit if state.entry_max_profit > 0 else 0

        for cp in cfg['no_progress']['checkpoints']:
            # Fire only on the bar where elapsed first crosses the checkpoint
            if cp['offset_minutes'] <= elapsed_min < cp['offset_minutes'] + BAR_MINUTES:
                if pct_of_max < cp['min_profit_pct_of_max']:
                    return f"NO_PROGRESS_T+{cp['offset_minutes']}"

    # ── 3. Profit target
    if cfg['profit_target']['enabled']:
        current_pnl = mark_to_market(state.open_trade_ref, current_chain)
        if current_pnl >= cfg['profit_target']['pct_of_max_profit'] * state.entry_max_profit:
            return "PROFIT_TARGET"

    # ── 4. Vol expansion
    if cfg['vol_expansion']['enabled']:
        current_short_prem = current_short_leg_premium(state, current_chain)
        spot_move_pct = abs(bar.close - state.entry_spot) / state.entry_spot
        if (current_short_prem / state.entry_short_premium
                >= cfg['vol_expansion']['premium_multiple_threshold']
            and spot_move_pct < cfg['vol_expansion']['max_spot_move_pct']):
            return "VOL_EXPANSION"

    # ── 5. Hard time stop
    if bar.timestamp.time() >= parse_time(cfg['hard_time_stop']):
        return "HARD_TIME_STOP"

    return None
```

### 7.1 Exit reason constants

Stored as strings in `MultiLegTrade.exit_reason`. Listed for analytics consumers:

```
TOUCH_EXIT_CALL, TOUCH_EXIT_PUT,
NO_PROGRESS_T+45, NO_PROGRESS_T+90,
PROFIT_TARGET,
VOL_EXPANSION,
HARD_TIME_STOP
```

## 8. Position sizing

Performed by the simulator at signal-arrival time (not by the strategy):

```python
def size_position(signal: MultiLegSignal, cfg, fills_estimate) -> int:
    # 1. Compute net credit from quoted mids (estimate)
    estimated_credit = (
        + mid(fills_estimate.short_call) + mid(fills_estimate.short_put)
        - mid(fills_estimate.long_call)  - mid(fills_estimate.long_put)
    )
    # 2. Wing width in points
    W = signal.metadata['wing_width']
    # 3. Max loss per lot in rupees
    lot_size = cfg['risk']['lot_size'][signal.instrument]
    max_loss_per_lot = (W - estimated_credit) * lot_size
    if max_loss_per_lot <= 0: return 0   # malformed structure; reject

    # 4. Lots
    risk_budget = cfg['iron_fly']['capital'] * cfg['iron_fly']['risk_per_trade_pct']
    lots = int(risk_budget // max_loss_per_lot)
    lots = min(lots, cfg['iron_fly']['max_lots_per_trade'])
    return lots
```

The simulator then multiplies every leg's `qty` by `lots` before routing to the multi-leg execution model.

## 9. Multi-leg execution model

`src/execution/multi_leg_simulator.py` simulates leg-by-leg fills under three modes (config-controlled):

| Mode | Behavior | When to use |
|---|---|---|
| `ideal` | All 4 legs fill at mid simultaneously | Smoke tests only |
| `realistic` | Shorts fill first at bid + slippage; wings fill 1–3 ticks later at ask + slippage | Default for backtest |
| `pessimistic` | Shorts fill at bid; wings fill 5–10 ticks later, possibly at worse than ask | Stress tests |

Realistic mode models the **naked-window risk**: there is a measurable interval where the shorts are filled but the wings are not. If spot moves > X% during the naked window, the structure is recorded as having entered at an adjusted (worse) max-loss.

Exits use the same leg-by-leg model; touch exits route as market orders on all 4 legs simultaneously.

## 10. Feature additions

### 10.1 IV regime feature

`src/features/iv_regime.py` maintains, per underlying, a rolling buffer of `(timestamp, atm_iv)` over `lookback_days`. Exposes:

```python
class IVRegimeFeature:
    def update(self, instrument: str, timestamp: datetime, atm_iv: float) -> None: ...
    def percentile(self, instrument: str, atm_iv: float) -> Optional[float]: ...
        # Returns position of `atm_iv` in the trailing distribution, [0,1]
        # None if buffer not yet warmed up
```

Wired into `BarEngine` alongside existing features; populated from `ChainSnapshot.atm_iv` each bar.

### 10.2 OR-width median (already partially available)

The opening-range feature must additionally expose `or_width_median(instrument, N)` — a trailing N-day median of OR widths. Can be computed from existing OR feature state with a small buffer; not a new feature.

## 11. Option chain feed

### 11.1 Interface

```python
# src/feeds/option_chain_snapshot.py

class OptionChainFeed(ABC):
    @abstractmethod
    def snapshot_at(self, timestamp: datetime, underlying: str) -> Optional[ChainSnapshot]: ...

class SyntheticOptionChainFeed(OptionChainFeed):
    """Black-Scholes + smile + configurable noise. For initial validation only."""
    ...

class HistoricalOptionChainFeed(OptionChainFeed):
    """Reads stored chain snapshots from disk. To be populated from real broker data."""
    ...
```

### 11.2 Runtime integration

`BarEngine` accepts an optional `OptionChainFeed` at construction. For each bar, it queries the feed for a snapshot at the bar's close time, passes it into `StrategyContext.chain_snapshot`. Non-options strategies receive the snapshot but ignore it.

## 12. Testing plan

### 12.1 Unit tests (per file, mirroring lab convention)

| Test target | Scenarios |
|---|---|
| `test_iron_fly.py` — entry | All filters pass; each filter fails in isolation; chain absent; DTE invalid; event-day 0DTE blocked; liquidity fails |
| `test_iron_fly.py` — exit | Each exit layer fires in isolation; touch + no-progress order conflict (touch wins); hard stop overrides |
| `test_iron_fly.py` — state machine | IDLE→OPEN→DONE happy path; signal rejected when phase != IDLE; reset() returns to IDLE |
| `test_iv_regime.py` | Buffer warm-up; percentile correctness; reset behavior |
| `test_multi_leg_simulator.py` | All three modes (ideal/realistic/pessimistic); naked-window slippage accounting; partial-fill handling |
| `test_option_chain_feed.py` | Synthetic chain monotonicity (call price decreases in strike, put increases); IV smile shape |

### 12.2 Integration tests

| Test | Description |
|---|---|
| Backtest single day, synthetic chain, expected entry | Construct a day that should pass all filters; assert exactly one trade emitted with correct legs |
| Backtest single day, no entry (trend day) | spot drifts away from VWAP; assert `TREND_TOO_STRONG` rejection |
| Backtest expiry day, profit target hit | Trade enters, decays naturally, exits at profit target |
| Backtest, touch exit fires | Spot reaches short strike mid-session; assert touch exit, P&L matches mark-to-market |
| Backtest, no-progress exit fires | Flat tape with no decay; assert exit at T+45 or T+90 |
| Backtest vs replay parity | Same inputs → identical trade ledger (uses existing parity comparison) |

### 12.3 Validation gates (must pass before any sizing-up beyond backtest)

1. **Sanity on synthetic chain** — at least 30 trading days, win rate in 60–80% range, net positive expectancy
2. **Real-chain backtest** — N expiry cycles (target: 6) on real option-chain snapshots
3. **Event-day stress replay** — explicit replay on every event-day in dataset; max drawdown bounded by `max_lots × max_loss_per_lot × N_events`
4. **Parameter sensitivity sweep** — vary `no_progress.checkpoints` thresholds, `profit_target.pct_of_max_profit`, `wing_width_pct_of_spot` over a grid; reject strategy if positive Sharpe exists only in narrow parameter slice
5. **Replay parity** — backtest and replay produce identical trade ledger

## 13. Acceptance criteria for "implementation complete"

The strategy is considered implemented (ready for validation, not ready for live paper) when:

- [ ] All new files in §2.1 exist and pass unit tests
- [ ] All modified files in §2.2 pass existing tests (no regressions on ORB / VWAP / Gap)
- [ ] `config/iron_fly.yaml` exists with documented defaults from §4
- [ ] Synthetic-chain backtest runs end-to-end with non-zero trades on a curated test dataset
- [ ] `MultiLegTrade` entries appear in run output with correct entry/exit fills, P&L, and exit reason
- [ ] All entries in §6.1's RejectionReason additions appear in `explain_no_signal` paths
- [ ] Integration tests in §12.2 pass
- [ ] Comparison utility extended to handle multi-leg trades (parity test passes)

Live paper readiness (Phase 5) requires §12.3 validation gates to pass — those are out of scope for "implementation complete."

## 14. Out of scope (explicitly deferred)

- v2 enhancements:
  - Scaled profit-target exits (partial close at 30%, rest at 50%)
  - ATR-based dynamic wing width
  - Intra-bar touch exits
  - Re-entry after no-progress exit (with cooldown)
  - Jade Lizard / Big Lizard variants
- Broker integration for live option-chain data (Phase 5+)
- Portfolio-level risk management across strategies
- Greeks-based exits (delta-neutrality drift, etc.) — current spec uses price-and-premium proxies only

---

**Next step after spec approval:** sequence the implementation tasks. Recommended order: option models + chain feed interface + synthetic feed → IV regime feature → multi-leg execution simulator → Iron Fly strategy → integration tests → backtest run on synthetic data. Each is independently testable.
