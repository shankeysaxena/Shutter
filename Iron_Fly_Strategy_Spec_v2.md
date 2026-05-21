# Intraday Anchored Iron Fly — Implementation Specification v2

**Status:** Spec v2 — **IMPLEMENTED 2026-05-18**. Supersedes v1 (`Iron_Fly_Strategy_Spec.md`).
**Depends on design doc:** `Iron_Fly_Strategy_Design.md`
**Lab phase:** Phase 4.6 plumbing complete on synthetic chain. Real-chain validation (Phase 4.8) is the next gate.
**Date:** 2026-05-18

---

## Ship status

All §17 acceptance criteria met. Final test count: **251 passing** (167 pre-v1 + 56 v1 + 28 v2). Smoke runner completes 65 sessions in **8.4s** — within the 30s budget set in §16.

What landed:
- §11 ExperimentRunner wiring — `IronFlyStrategy` participates in the standard `main.py --config` flow
- §12 Unified ledger + multi-leg metrics — `summary.json` carries a `multi_leg` subsection; `multi_leg_trades.csv`, `multi_leg_summary.json`, `exit_reasons.csv` written when applicable
- §13 Synthetic-chain warning — `ExperimentResult.data_source_warning` auto-set; surfaced in `main.py` console and `summary.json`
- §16 Determinism + per-session progress + 30s budget

What this validates: the **plumbing**. What this does *not* validate: edge — that requires real or replay-quality chain data, which is the Phase 4.8 work.

---

## 0. What changed from v1

v1 was implemented end-to-end (~14 files, 56 new tests, smoke runner). Code review surfaced five concerns. v2 incorporates each one explicitly so the spec, code, and reports are aligned.

| # | v1 issue | v2 resolution |
|---|---|---|
| 1 | Iron Fly not wired into `ExperimentRunner` — needs a separate smoke runner | §11 makes `ExperimentRunner` integration a mandatory step before any strategy validation work |
| 2 | `MultiLegTrade` not included in standard reports (summary, ledger, by-strategy) | §12 specifies a unified ledger format and multi-leg metrics |
| 3 | Touch exit defaulted to wing strikes in code; spec text said short strikes | §7.1 redefines touch-exit semantics for iron fly explicitly, with default + rationale |
| 4 | Synthetic chain feed is fine for plumbing but produces misleading P&L | §13 draws an explicit line: synthetic for tests/plumbing only, never for edge judgment |
| 5 | `run_iron_fly_smoke.py` had reproducibility concerns in the reviewer's environment | §16 adds a determinism/timeout audit before any sweep work |

v2 also corrects two design defects v1 carried over from v1's spec:

- **No-progress thresholds (10% / 25%)** were unrealistic for intraday iron fly under round-trip spread + brokerage costs. v2 marks these as needing data-driven calibration, not a fixed-default decision. (§7.3)
- **Touch geometry** for iron fly is structurally different from iron condor. v2 names the parameter and the default with rationale. (§7.1)

## 1. Decisions taken (locked unless explicitly revisited)

| # | Decision | Default | Notes |
|---|---|---|---|
| 1 | Wing width W | 0.5% of spot, snapped to strike interval | ATR-based dynamic deferred to v3 |
| 2 | Profit target | 15% of max profit (intraday) | v1's 40% was a hold-to-expiry value; intraday capture is structurally lower |
| 3 | DTE | {0, 1, 2}, 0DTE blocked on event-day list | event-day list loaded from CSV; empty in smoke |
| 4 | Re-entry per day | One trade per underlying per day | Re-entry deferred to v3 |
| 5 | Touch exit boundary | At wing strikes (`distance_pct_of_wing = 1.0`) | See §7.1 for rationale and alternatives |
| 6 | No-progress thresholds | **No fixed default** — must be derived from real-data sweep | v1's defaults were noise; see §7.3 |
| 7 | Chain pricing source | Synthetic for tests; real-or-replay for any edge claim | See §13 |
| 8 | Sizing rule | `lots = floor(capital × risk_pct / max_loss_per_lot)` capped at `max_lots_per_trade` | Unchanged from v1 |

## 2. File layout

Mostly unchanged from v1. Two new files and one significant rewrite:

### New files (v2)

```
strategy_lab/src/
└── analytics/
    └── multi_leg_metrics.py         # NEW — multi-leg ledger + metrics
strategy_lab/tests/
└── test_multi_leg_metrics.py        # NEW — coverage for the above
```

### Modified files (v2)

| File | Change in v2 |
|---|---|
| `src/backtest/experiment.py` | `_build_strategies` adds `IronFlyStrategy`; runner builds and passes `MultiLegSimulator` + `OptionChainFeed` to `BacktestRuntime` when `iron_fly.enabled` |
| `src/analytics/metrics.py` | Extend `generate_trade_ledger` to accept multi-leg trades; produce unified output |
| `src/analytics/reporting.py` | Summary, by_strategy, exit-reason breakdown include multi-leg structures |
| `config/base.yaml` | Add `iron_fly` block (enabled=false by default); add `option_chain_feed` config |
| `src/strategies/iron_fly.py` | Reaffirm touch-exit semantics in module docstring; remove "short_strike" comment confusion |
| `src/runtimes/replay.py` | Mirror backtest runtime's multi-leg wiring (for replay parity) |

All v1 files (`option_models.py`, `option_chain_snapshot.py`, `iv_regime.py`, `multi_leg_simulator.py`, `iron_fly.py`, engine changes) remain. v2 builds on top, not over.

## 3. Data model — unchanged from v1

`OptionLeg`, `ChainQuote`, `ChainSnapshot`, `MultiLegSignal`, `LegFill`, `MultiLegTrade` stay as written. v2 doesn't touch them.

The unified ledger (§12) is a *projection* over single-leg + multi-leg trades, not a new storage model.

## 4. Config schema (v2)

```yaml
# config/base.yaml additions

iron_fly:
  enabled: false                          # off by default; flip per-experiment

  # Universe and timing — same as v1
  underlyings: [NIFTY, BANKNIFTY]
  allowed_dte: [0, 1, 2]
  event_day_blacklist_0dte: true
  event_days_file: data/event_days.csv
  entry_window_start: "09:45"
  entry_window_end: "13:30"

  # Entry filters — same as v1
  trend_filter:
    max_vwap_distance_pct: 0.0025
  range_filter:
    or_width_lookback_days: 20
    max_or_width_vs_median: 1.0
  iv_regime_filter:
    lookback_days: 60
    min_percentile: 0.25
    max_percentile: 0.75
  liquidity_filter:
    max_atm_spread_pct: 0.02
    require_two_sided_wings: true

  # Structure
  wing_width_pct_of_spot: 0.005
  strike_interval:
    NIFTY: 50
    BANKNIFTY: 100

  # Sizing
  risk_per_trade_pct: 0.005
  capital: 1000000
  max_lots_per_trade: 10
  lot_size:
    NIFTY: 25
    BANKNIFTY: 15

  # Exits — note touch_exit and no_progress changes
  exits:
    touch_exit:
      enabled: true
      distance_pct_of_wing: 1.0          # 1.0 = at wing strikes; see §7.1
    no_progress:
      enabled: true
      # Thresholds DELIBERATELY left low until real-chain sweep produces calibrated values.
      # v1's 10% / 25% will fire on every trade under round-trip spread costs.
      checkpoints:
        - { offset_minutes: 45, min_profit_pct_of_max: 0.01 }
        - { offset_minutes: 90, min_profit_pct_of_max: 0.05 }
    profit_target:
      enabled: true
      pct_of_max_profit: 0.15            # intraday — not hold-to-expiry
    vol_expansion:
      enabled: true
      premium_multiple_threshold: 1.3
      max_spot_move_pct: 0.005
    hard_time_stop: "15:15"

  # Operational
  one_trade_per_underlying_per_day: true

option_chain_feed:                       # NEW top-level block
  type: synthetic                        # synthetic | historical
  synthetic:
    atm_iv: 0.15
    skew: -0.02
    smile: 0.30
    risk_free_rate: 0.07
    num_strikes_each_side: 20
    spread_pct: 0.01
    min_spread: 0.5
    expiry_weekday: 3                    # Thursday
    # Optional: daily-varying ATM IV for plumbing tests that want IV-regime signal
    daily_iv_variation: false
  historical:
    snapshot_dir: data/option_chain_snapshots

multi_leg_simulator:                     # NEW top-level block
  mode: realistic                        # ideal | realistic | pessimistic
  tick_size: 0.05
  brokerage_per_leg: 20.0
```

## 5. State machine — unchanged from v1

`IDLE → ENTERING → OPEN → DONE` per (instrument, session_date), reset between runs. Notification callbacks `on_multi_leg_filled` / `on_multi_leg_closed` / `on_multi_leg_rejected` stay.

## 6. Entry algorithm — unchanged from v1

Filter order and rejection-reason mapping stay as in v1 §6.

## 7. Exit algorithm — corrected in v2

### 7.1 Touch exit — new explicit semantics

**The choice:** For an Iron Fly, the short call and short put both sit at the ATM strike. "Exit on short-strike touch" therefore means "exit on any deviation from ATM at the bar close" — this fires almost every bar and produces a strategy that is structurally unable to hold.

v2 resolves this by introducing a configurable touch boundary:

```python
touch_offset = wing_width * exits.touch_exit.distance_pct_of_wing
touch_upper = atm + touch_offset
touch_lower = atm - touch_offset
```

| `distance_pct_of_wing` | What it means | When to use |
|---|---|---|
| 0.0 | At ATM (short strike) | "Cut on any drift" — original v1 spec interpretation. Almost never holds. |
| 0.3–0.5 | Inside breakeven band | Defensive — exits as gamma starts to bite, before max loss locks in. Reasonable for tight markets. |
| 1.0 *(default)* | At wing strike | At the max-loss frontier — by the time spot reaches here, the trade is already realized loss. |
| > 1.0 | Outside wings | Pointless — wings cap loss anyway. |

**Default rationale (1.0):** for the first validation pass we want the trade to survive long enough to capture theta on days that stay range-bound. Tightening this is a parameter-sweep concern, not a default decision.

Cross-reference to design doc §3.4: the design doc said "spot touches either short strike → close immediately." v2 amends this to "touches the configurable touch boundary, defaulting to the wing strikes." Update the design doc accordingly when this spec is approved.

### 7.2 Touch exit code mapping

```python
# state.touch_upper / state.touch_lower are computed at entry from wing_pts
if bar.close >= state.touch_upper: return 'TOUCH_EXIT_CALL'
if bar.close <= state.touch_lower: return 'TOUCH_EXIT_PUT'
```

State fields renamed in v2 for clarity: `touch_upper`, `touch_lower` (not `short_call_strike`/`short_put_strike` which now serve only as bookkeeping for analytics).

### 7.3 No-progress exit — calibration policy

v1 spec used `{T+45: 10% of max, T+90: 25%}`. Empirically these fire on every trade in the smoke run because:

1. Iron Fly max profit (per the unified definition = net entry credit) is only fully realized at expiry pin.
2. Intraday theta capture in 45 min is on the order of **1–3% of max profit**, not 10%.
3. Round-trip spread + brokerage on a 4-leg structure is ~₹300–500 — comparable to or larger than 45-min theta capture in synthetic data.

v2 policy: **no_progress thresholds are not a fixed default.** They are an output of the validation sweep on real chain data. Smoke runner uses {1%, 5%} as a "doesn't fire spuriously" placeholder; production values land in §16.

The exit *logic* is unchanged — only the threshold-as-default decision moves.

### 7.4 Other exit layers — unchanged from v1

Profit target (15% of max — intraday), vol expansion (1.3× short premium under 0.5% spot move), hard time stop (15:15) stay.

## 8. Position sizing — unchanged from v1

Computed in engine via `_size_multi_leg` using `signal.metadata['max_loss_per_lot_rupees']`.

## 9. Multi-leg execution model — unchanged from v1

Three modes (`ideal` / `realistic` / `pessimistic`). v2 makes mode configurable via top-level `multi_leg_simulator` block (§4).

## 10. Feature additions — unchanged from v1

`IVRegimeFeature` (held by strategy), OR-width history (held by strategy). v2 does not change either.

## 11. ExperimentRunner integration — **new section, must-fix**

v1 left `IronFlyStrategy` out of `_build_strategies()`. v2 fixes this so that the standard validation pipeline (the same `main.py` flow used for ORB/VWAP/Gap) can run Iron Fly experiments.

### 11.1 `_build_strategies` extension

```python
def _build_strategies(config: dict) -> List[BaseStrategy]:
    strategies = []
    s_cfg = config.get('strategies', {})

    if s_cfg.get('orb', {}).get('enabled', True):
        strategies.append(ORBStrategy())
    if s_cfg.get('vwap_pullback', {}).get('enabled', True):
        strategies.append(VWAPPullbackStrategy())
    if s_cfg.get('gap_behavior', {}).get('enabled', True):
        strategies.append(GapBehaviorStrategy())
    # v2:
    if s_cfg.get('iron_fly', {}).get('enabled', False):
        event_days = _load_event_days(s_cfg['iron_fly'].get('event_days_file'))
        strategies.append(IronFlyStrategy(event_days=event_days))
    return strategies
```

### 11.2 Runtime construction

`ExperimentRunner.run()` builds and threads through both option-chain feed and multi-leg simulator when any options strategy is enabled:

```python
def _build_chain_feed(config):
    cfg = config.get('option_chain_feed', {})
    if cfg.get('type', 'synthetic') == 'synthetic':
        sc = cfg.get('synthetic', {})
        return SyntheticOptionChainFeed(
            atm_iv=sc.get('atm_iv', 0.15),
            skew=sc.get('skew', -0.02),
            smile=sc.get('smile', 0.30),
            strike_interval={'NIFTY': 50, 'BANKNIFTY': 100},
            num_strikes_each_side=sc.get('num_strikes_each_side', 20),
            spread_pct=sc.get('spread_pct', 0.01),
            min_spread=sc.get('min_spread', 0.5),
            expiry_provider=WeeklyExpiryProvider(weekday=sc.get('expiry_weekday', 3)),
        )
    return HistoricalOptionChainFeed(snapshot_dir=cfg['historical']['snapshot_dir'])

def _build_multi_leg_simulator(config):
    cfg = config.get('multi_leg_simulator', {})
    return MultiLegSimulator(
        mode=cfg.get('mode', 'realistic'),
        tick_size=cfg.get('tick_size', 0.05),
        brokerage_per_leg=cfg.get('brokerage_per_leg', 20.0),
    )

# In run():
options_enabled = any(isinstance(s, IronFlyStrategy) for s in strategies)
chain_feed = _build_chain_feed(config) if options_enabled else None
ml_sim = _build_multi_leg_simulator(config) if options_enabled else None

runtime = BacktestRuntime(
    strategies=strategies,
    simulator=simulator,
    config=config,
    multi_leg_simulator=ml_sim,
    chain_feed=chain_feed,
)
```

Same wiring for `ReplayRuntime` so backtest/replay parity holds.

### 11.3 Event-day CSV loader

Format: one ISO date per line, no header.

```python
def _load_event_days(path: Optional[str]) -> Set[date]:
    if not path or not os.path.exists(path):
        return set()
    days = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                days.add(date.fromisoformat(line))
    return days
```

### 11.4 ExperimentResult extension

```python
@dataclass
class ExperimentResult:
    ...
    all_trades: List[Trade]                          # unchanged
    all_multi_leg_trades: List[MultiLegTrade]        # NEW
    ledger: pd.DataFrame                             # now unified — see §12
    multi_leg_summary: Dict                          # NEW — see §12.3
```

## 12. Unified ledger + multi-leg metrics — **new section, must-fix**

### 12.1 Unified trade-ledger format

Both single-leg and multi-leg trades project into one DataFrame with these columns:

| Column | Single-leg value | Multi-leg value |
|---|---|---|
| `trade_id` | uuid | uuid |
| `strategy` | strategy.name | strategy.name |
| `instrument` | symbol | underlying |
| `trade_type` | `'single_leg'` | `'multi_leg'` |
| `structure` | direction (LONG/SHORT) | structure_type (IRON_FLY) |
| `entry_time` | candle ts | leg fills ts |
| `entry_price` | trade.entry_price | `NaN` (use `net_entry_credit`) |
| `net_entry_credit` | `NaN` | trade.net_entry_credit |
| `exit_time` | trade.exit_time | trade.exit_time |
| `exit_price` | trade.exit_price | `NaN` (use `net_exit_debit`) |
| `net_exit_debit` | `NaN` | trade.net_exit_debit |
| `exit_reason` | STOP/TARGET/EOD | TOUCH/NO_PROGRESS/.../EOD |
| `qty` | trade.qty | total units across all legs |
| `n_legs` | 1 | `len(entry_fills)` |
| `gross_pnl` | trade.gross_pnl | trade.gross_pnl |
| `net_pnl` | trade.net_pnl | trade.net_pnl |
| `r_multiple` | trade.r_multiple | `NaN` (R doesn't apply cleanly) |
| `runtime_mode` | trade.runtime_mode | trade.runtime_mode |
| `metadata_json` | json.dumps(metadata) | json.dumps(metadata) |

`MetricsEngine.generate_trade_ledger(single_leg_trades, multi_leg_trades)` returns this combined DataFrame.

### 12.2 Multi-leg-specific metrics

`src/analytics/multi_leg_metrics.py`:

```python
def compute_multi_leg_summary(trades: List[MultiLegTrade]) -> Dict:
    if not trades:
        return {'total_trades': 0}
    pnls = [t.net_pnl for t in trades if t.net_pnl is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    credits = [t.net_entry_credit for t in trades]
    max_losses = [t.metadata.get('max_loss_per_lot_rupees', 0) for t in trades]

    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t.exit_reason or 'NONE'].append(t.net_pnl or 0)

    return {
        'total_trades':         len(trades),
        'win_rate':             len(wins) / len(trades),
        'avg_win':              statistics.mean(wins) if wins else 0,
        'avg_loss':             statistics.mean(losses) if losses else 0,
        'total_net_pnl':        sum(pnls),
        'total_gross_pnl':      sum(t.gross_pnl or 0 for t in trades),
        'avg_net_credit':       statistics.mean(credits),
        'avg_max_loss_per_lot': statistics.mean(max_losses),
        'max_drawdown':         _running_drawdown(pnls),
        'exits_by_reason':      {k: {'n': len(v), 'total_pnl': sum(v)} for k, v in by_reason.items()},
    }
```

### 12.3 Report integration

`ReportWriter` writes three additional artifacts when multi-leg trades are present:

- `multi_leg_trades.csv` — full ledger including leg-level expansion (one row per leg fill)
- `multi_leg_summary.json` — output of `compute_multi_leg_summary`
- `exit_reasons.csv` — exit_reason × count × total_pnl × avg_pnl

`summary.json` (top-level) gets a `multi_leg` subsection alongside the existing `single_leg` subsection.

### 12.4 by_strategy report

Already keyed by `strategy_name`; multi-leg strategies appear as additional rows. No structural change — the unified ledger feeds the existing groupby.

## 13. Synthetic vs real chain — **explicit boundary**

| Use case | Allowed feed | Why |
|---|---|---|
| Unit tests | Synthetic | Deterministic, fast |
| Integration tests | Synthetic | Same |
| Plumbing/pipeline smoke (`run_iron_fly_smoke.py`) | Synthetic | Validates wiring, *not* edge |
| Parameter sweeps | **Historical only** | Synthetic prices smooth out skew kinks, liquidity premia, and event-day vol shocks — the exact regime where the strategy lives or dies |
| Live paper / live | **Historical-validated** | Self-explanatory |

Any P&L claim, win rate, or Sharpe number from synthetic-chain runs is documentation/illustration, not evidence. The unified ledger and reports tag this explicitly:

```python
report['data_source_warning'] = 'SYNTHETIC_CHAIN — not valid for edge claims'
```

This tag is set automatically by `ExperimentRunner` when `option_chain_feed.type == 'synthetic'`.

## 14. Out of scope (explicit)

- **Phase 5 / live paper** — explicitly NOT next. Phases 4.6 (options infra) and 4.8 (validation harness) come first.
- v3 enhancements (deferred): scaled profit targets, ATR wings, intra-bar touch, re-entry with cooldown, Jade Lizard variant, Greeks-based exits.

## 15. Testing plan — incremental over v1

v1 already added 56 tests (option models, chain feed, IV regime, multi-leg simulator, iron fly). v2 adds:

| New test file | Coverage |
|---|---|
| `test_multi_leg_metrics.py` | `compute_multi_leg_summary` happy path, empty list, single-trade, mixed wins/losses, exits_by_reason correctness |
| `test_experiment_runner_iron_fly.py` | `_build_strategies` adds Iron Fly when enabled; chain feed + multi-leg simulator wired; result includes `all_multi_leg_trades`; unified ledger correctness; `data_source_warning` tag |

Plus regression: all 223 existing tests must continue to pass.

## 16. Reproducibility audit — **new must-fix item**

Reviewer reported `run_iron_fly_smoke.py` did not complete cleanly in their sandbox. Before any sweep work, v2 requires:

1. **Determinism check**: synthetic chain feed must produce identical snapshots given identical inputs. Add `test_synthetic_feed_determinism` to lock this.
2. **Wall-clock baseline**: smoke runner over 65 sessions must complete in under 30 seconds on a stock laptop. If it doesn't, profile before adding any features.
3. **Per-session progress**: smoke runner emits a one-line per-session status update so a hang is immediately visible.

These are reliability gates, not validation gates — they protect the validation work that comes after.

## 17. Acceptance criteria — all met

- [x] §11 — `ExperimentRunner` builds and runs Iron Fly when enabled; backtest + replay both work
- [x] §12 — unified ledger; multi-leg metrics; report artifacts written
- [x] §13 — `data_source_warning` tag appears in synthetic-chain run output
- [x] §16 — determinism (4 tests), per-session progress, 8.4s wall-clock vs 30s budget
- [x] §7.1 — module docstring + design-doc cross-reference updated; no contradictory comments
- [x] No regressions: 223 prior tests + 28 v2 tests = 251 passing
- [x] `config/base.yaml` has `iron_fly` (off by default) + `option_chain_feed` + `multi_leg_simulator` blocks
- [x] `run_iron_fly_smoke.py` kept as a fast plumbing-check tool; `main.py --config` is the standard path

## 18. What we are NOT validating with v2

v2 finishes the *plumbing and reporting* layer. It does not — and cannot, on synthetic data — answer:

- Does the Iron Fly have edge?
- Are the no-progress thresholds correct?
- Is the touch boundary tuned right?
- How does the strategy behave on event days?

Those are Phase 4.8 (validation harness on real chain data) questions. v2 makes them *answerable*; it does not answer them.

---

---

## v2.1 patch — Phase 4.6B (shipped 2026-05-18)

Second review surfaced seven gaps in v2's plumbing. All addressed; **256 tests passing (254 fast in 1.6s, 2 slow gated by `-m slow`)**.

| # | Issue | Resolution |
|---|---|---|
| 1 | Full test suite slow due to E2E experiment runs | `@pytest.mark.slow` marker added; `addopts = -m 'not slow'` in `pyproject.toml`. Default suite stays fast; `pytest -m slow` runs the E2Es; `pytest -m ""` runs everything |
| 2 | E2E test could pass with zero multi-leg trades | New `test_iron_fly_lifecycle.py` — 3 deterministic tests: signal→fill→exit→ledger, ledger projection, intra-bar touch breach. State is hand-constructed so filters provably pass |
| 3 | `IVRegimeFeature` params were hardcoded; config blocks ignored | `_build_strategies` now reads `iv_regime_filter.lookback_days` and new `iv_regime_filter.min_observations`, plus `range_filter.or_history_min_days`, and threads them into the strategy constructor |
| 4 | `daily_iv_variation` referenced in spec/config but not implemented | Implemented as sinusoidal day-over-day shift (period 14d, amplitude 0.05) in `SyntheticOptionChainFeed`. Explicit `atm_iv_provider` still takes precedence |
| 5 | `max_total_trades_per_day` didn't count multi-leg trades | Engine cap now counts open + closed-today + queued multi-leg trades alongside single-leg state |
| 6 | Touch exit used `bar.close`, missing intra-bar breaches | Now uses `bar.high >= touch_upper` / `bar.low <= touch_lower`. Conservative for risk management — any intra-bar breach fires |
| 7 | Multi-leg fill timing undocumented and untested | `BarEngine` docstring documents the asymmetry (single-leg fills at T+1 open, multi-leg at T+1 close-derived chain). New `test_multi_leg_entry_timing.py` locks the semantics with a stub chain feed |

### Updated config schema (v2.1 additions)

```yaml
strategies:
  iron_fly:
    range_filter:
      or_history_min_days: 5            # NEW — wired
    iv_regime_filter:
      min_observations: 500             # NEW — wired

option_chain_feed:
  synthetic:
    daily_iv_variation: false           # NEW — implemented
```

### Known accepted-but-bounded biases

- **Multi-leg fill price reflects bar-close chain quotes, not bar-open.** Bar-resolution backtests have no intra-bar chain snapshot. The `data_source_warning` already covers this for synthetic feeds; real-chain runs inherit the same caveat at one-bar granularity.
- **Touch exit on bar high/low is conservative — may overcount touches.** A bar that wicks through the touch boundary but closes well inside still triggers exit. Net effect: slightly more touch exits than a fill-quality-aware model would produce. Treated as protective bias, not a defect.

---

## v2.2 patch (shipped 2026-05-18)

Third review surfaced two issues. Both addressed. **258 tests passing (256 fast in ~2s, 2 slow gated by `-m slow`)**.

| # | Issue | Resolution |
|---|---|---|
| 1 | `MultiLegSimulator.open_trade` stamped `entry_time` and `LegFill.fill_time` with `signal.timestamp` (bar T), but the trade actually fills at bar T+1 using bar T+1's chain. Ledger / no-progress / duration analytics were off by one bar | `open_trade(..., fill_time=ts)` parameter added; engine passes the fill bar's timestamp. Test `test_multi_leg_entry_timing.py` extended to assert `trade.entry_time` and `fill.fill_time` both equal the T+1 bar timestamp and explicitly differ from `signal.timestamp` |
| 2 | `force_eod_exits` silently dropped multi-leg trades on the floor when chain snapshot was unavailable at session-end | Trades now move to `closed_multi_leg_trades` with `exit_reason='EOD_NO_CHAIN'`, `gross_pnl=None`, `net_pnl=None`. A `multi_leg_eod_no_chain` warning event is emitted to the event log with `severity='warning'`. Two new tests cover both the missing-chain and chain-present paths |

The fill-timestamp fix is **silently correctness-critical** — without it, multi-leg trades appear in the ledger as if they entered at the signal-emission bar. This affects everything that uses `entry_time`: no-progress exits (computed from elapsed time since entry), trade durations, intraday P&L attribution, and any future backtest/replay parity check that compares timestamps.

The EOD missing-chain fix is **operationally critical** for the upcoming Phase 4.8 work — real historical chain data will have gaps, and silently disappearing trades from reports would be an extremely difficult class of bug to catch downstream.

---

---

## Phase 4.8a — Historical chain plumbing (shipped 2026-05-18)

Real chain data is still pending sourcing; this phase builds the *pipe* that will receive it. Decision: store snapshots as Parquet, one file per `(instrument, date)`, with an explicit `data_origin` declared in a manifest. Synthetic data is allowed through the same pipe so the end-to-end path is testable today; the `data_source_warning` only clears when the manifest declares `recorded` or `broker` origin.

**275 fast tests passing** (256 prior + 19 new). 2 slow E2E tests gated by `-m slow`.

### What landed

| Component | Path | Purpose |
|---|---|---|
| Schema definition | `src/feeds/chain_archive_schema.py` | Required columns, manifest dataclass, origin constants, path helpers |
| Loader | `HistoricalOptionChainFeed` in `src/feeds/option_chain_snapshot.py` | Lazy per-day Parquet loads, in-memory `{timestamp → ChainSnapshot}` cache, schema validation |
| Exporter | `src/feeds/chain_archive.py` | `export_chain_archive(feed, out_dir, underlying, bars, manifest)` + `export_from_ohlcv()` convenience wrapper |
| CLI: synthetic export | `tools/export_synthetic_chain.py` | Drive synthetic feed across an OHLCV file, write archive |
| CLI: archive audit | `tools/validate_chain_archive.py` | Schema compliance, sanity checks, cadence/gap reporting |
| `data_origin` plumbing | `OptionChainFeed.data_origin` property | Polymorphic; `ExperimentRunner` reads it to decide warning state |

### On-disk layout

```
<root>/
    _meta.yaml                          # schema_version, data_origin, generated_at, notes
    NIFTY/
        2024-01-02.parquet              # one file per session
        2024-01-03.parquet
    BANKNIFTY/
        ...
```

Per-file Parquet columns (one row per quote): `timestamp`, `spot`, `expiry`, `strike`, `option_type`, `bid`, `ask`, `last`, `iv`. Sorted on write by `(timestamp, strike, option_type)`.

Expected file size: ~375 timestamps × ~80 strikes×types ≈ 30k rows per session per instrument.

### `data_origin` and the warning lifecycle

| Origin value | Warning state | Use case |
|---|---|---|
| `synthetic` *(default for the exporter)* | warning ON | Generated by `tools/export_synthetic_chain.py`. Plumbing tests, not edge claims |
| `unknown` *(no manifest)* | warning ON | Defensive default |
| `recorded` | **warning OFF** | Real chain snapshots recorded from a live feed |
| `broker` | **warning OFF** | Real chain snapshots pulled from a broker archive |

The defensive default is the key piece: any new archive defaults to keeping the warning until someone *explicitly* writes `recorded` or `broker` into `_meta.yaml`. This prevents accidental loss of the warning when an archive's provenance is unclear.

### Usage

Generate a synthetic archive from existing OHLCV:

```
python3 tools/export_synthetic_chain.py \
    --ohlcv data/raw/NIFTY.csv \
    --underlying NIFTY \
    --out data/option_chain_snapshots \
    --atm-iv 0.15 \
    --daily-iv-variation
```

Audit it:

```
python3 tools/validate_chain_archive.py --root data/option_chain_snapshots
```

Run a backtest against it (set in config):

```yaml
option_chain_feed:
  type: historical
  historical:
    snapshot_dir: data/option_chain_snapshots
```

### What's intentionally NOT in 4.8a

- **No real broker integration.** That requires source-specific normalizers (Zerodha/Dhan/etc. each have different formats). Layer on top once the broker is chosen.
- **No parameter sweep tooling.** Deferred to 4.8b — once we can declare a real-data archive, sweeping the no-progress checkpoints, profit-target, and touch-distance becomes meaningful.
- **No replay-parity test against historical chain.** Should land in 4.8b alongside sweeps.

### Phase 4.8b — next, when real data is sourced

1. Source N expiry cycles of real chain snapshots (broker archive or recorded feed)
2. Write a per-broker normalizer that produces files matching the §4.8a schema
3. Set `data_origin: recorded` in the manifest
4. Run `pytest -m slow` + a parameter-sweep run; warning clears automatically
5. Event-day stress replay using `event_days.csv`

### Robustness — pyarrow dependency

Parquet-dependent test files use `pytest.importorskip("pyarrow")` at module level. Behavior:
- With `pyarrow` installed (`pip install -r requirements-dev.txt`) → all 280 tests run
- Without `pyarrow` → 22 Parquet-dependent tests skipped, remaining 258 still pass
- Production code (`HistoricalOptionChainFeed.__init__`) still raises a loud, actionable `ImportError` when called without pyarrow installed

---

**Next step after Phase 4.8a ship:** wait for real chain data, then Phase 4.8b.
Concretely:
1. Source N expiry cycles of NIFTY/BANKNIFTY option-chain snapshots (broker archive or recorded feed)
2. Implement `HistoricalOptionChainFeed.snapshot_at` (currently raises `NotImplementedError`)
3. Run the same `main.py --config` flow with `option_chain_feed.type: historical` — `data_source_warning` clears automatically and only then are P&L numbers admissible as edge evidence
4. Parameter sweep on `no_progress.checkpoints`, `profit_target.pct_of_max_profit`, `touch_exit.distance_pct_of_wing`, `wing_width_pct_of_spot`
5. Event-day stress replay using the loaded `event_days.csv`

Out of scope: Phase 5 / live paper. Not next.
