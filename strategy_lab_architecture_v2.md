# 📘 Stocks / FnO Strategy Lab — System Architecture (V2)

## Context

We are building a **validation-first intraday trading research and live paper-trading system** for:

- NIFTY
- BANKNIFTY

This system should support both:

1. **Conservative historical backtesting**
2. **Live paper trading in real market conditions**

without duplicating strategy logic.

The architecture must remain:

- simple
- deterministic
- reproducible
- research-focused first
- extensible toward live paper trading

---

# 1. System Goal

Build a Python framework that can:

1. ingest and validate 1-minute OHLCV data
2. compute derived features like VWAP, Opening Range, and Gap %
3. run deterministic strategies
4. simulate realistic execution in backtests
5. run the same strategy logic in live paper trading
6. evaluate strategy performance
7. produce auditable trade logs and experiment reports

This is **not yet a real-money production trading platform**.

The immediate goals are:

- prove whether the strategy has edge
- reduce backtest bias as much as possible
- validate behavior in live market through paper trading
- only then consider capital deployment

---

# 2. High-Level Architecture

The architecture should be built around a **shared event-driven core** and **multiple runtimes**.

```text
                    ┌─────────────────────────────┐
                    │      Shared Strategy Core   │
                    │-----------------------------│
                    │ Data Models                 │
                    │ Feature Calculations        │
                    │ Strategy Rules              │
                    │ Signal Objects              │
                    │ Risk Rules                  │
                    │ Position State Machine      │
                    │ Metrics Logic               │
                    └──────────────┬──────────────┘
                                   │
             ┌─────────────────────┼─────────────────────┐
             │                     │                     │
             v                     v                     v
┌─────────────────────┐  ┌─────────────────────┐  ┌─────────────────────┐
│ Historical Backtest │  │ Historical Replay   │  │ Live Paper Runtime  │
│ Runtime             │  │ Runtime             │  │                     │
│---------------------│  │---------------------│  │---------------------│
│ Past candles        │  │ Past candles fed    │  │ Live websocket/feed │
│ sequentially        │  │ bar-by-bar as live  │  │ Live bar builder    │
│ Conservative fills  │  │ Same runtime logic  │  │ Paper execution     │
│ Metrics + reports   │  │ Signal validation   │  │ Session monitor     │
└─────────────────────┘  └─────────────────────┘  └─────────────────────┘
```

This is the core redesign.

We are not separating research and live trading into totally different systems.

Instead, we are creating:

- one shared core
- three runtimes
  - backtest
  - replay
  - live paper

That way:

- strategy rules remain identical
- only the source of bar events changes
- validation becomes much stronger
- transition from backtest to live paper becomes natural

---

# 3. Recommended Core Design Principle

Use a **bar-by-bar event-driven engine** from day one.

That means strategies do not operate on giant vectorized logic directly.

Instead, they receive:

- current bar
- historical context already known so far
- feature snapshot
- active position state
- day/session state

This is important because:

- it reduces lookahead bias
- it matches real trading flow
- it works for both backtest and live paper trading
- it makes debugging easier
- it keeps one consistent mental model

So the design principle is:

- **vectorized preprocessing is allowed for offline features**
- **signal generation and execution should be event-driven**

---

# 4. Core Modules

## A. Data Pipeline

Responsible for:

- loading raw 1-minute OHLCV data
- validating schema and session integrity
- standardizing timestamps and instruments
- sessionizing historical data
- preparing prior-day reference values
- supporting both offline and live inputs

### Responsibilities

- read CSV / parquet / database extract
- standardize columns
- validate data quality
- remove duplicates
- ensure proper market-hour alignment
- split data into trading sessions
- provide prior close / previous session data

### Output

For historical runtimes:
- clean per-session bars

For live runtime:
- standardized bar events after aggregation

## B. Feature Engine

Responsible for computing all derived fields centrally.

### Core features

- VWAP (daily reset)
- Opening Range high/low
- Gap %
- OR width
- VWAP distance
- cumulative session range
- prior day close
- session minute index
- above/below VWAP flags

### Important principle

Features must be computed in a shared layer, not inside strategy classes.

That prevents:

- duplicated formula logic
- inconsistent feature interpretation
- drift between backtest and live modes

### Output

A feature snapshot attached to each bar event.

## C. Strategy Engine

Responsible for:

- reading current market state
- determining whether a valid setup exists
- generating a signal object if rules are met

Strategies should only decide:

- whether to trade
- direction
- stop logic
- target logic
- strategy metadata

Strategies should not directly perform:

- pnl accounting
- ledger updates
- brokerage adjustments
- paper order placement
- portfolio summaries

These belong to runtime execution layers.

### V1/V2 strategies

- ORBStrategy
- VWAPPullbackStrategy
- GapBehaviorStrategy

### Output

A `Signal` object or `None`.

## D. Trade Simulator / Paper Execution Layer

This layer differs slightly by runtime.

### In Backtest Runtime

Responsible for:

- entry at next candle open
- stop / target / EOD handling
- slippage and cost application
- trade ledger generation
- conservative intrabar assumptions

### In Replay Runtime

Responsible for:

- processing the same rules as if live
- validating runtime behavior
- simulating entries and exits identically to backtest mode
- ensuring event-driven correctness

### In Live Paper Runtime

Responsible for:

- receiving live bar-close signals
- placing paper entries
- tracking open paper positions
- logging exits
- recording operational events
- validating whether real-time behavior matches research assumptions

### Output

A normalized trade ledger format across all runtimes.

## E. Metrics Engine

Responsible for:

- trade-level statistics
- session-level summaries
- equity curve generation
- drawdown analysis
- segmentation by market conditions
- comparing backtest vs replay vs live paper results

### Output

- metrics tables
- experiment summaries
- audit reports
- plots
- comparative runtime evaluation

## F. Runtime Orchestrator

Responsible for selecting and running the runtime mode.

Supported modes:

- `backtest`
- `replay`
- `live_paper`

### Responsibilities

- initialize config
- initialize data/feed source
- load strategies
- feed bars into shared engine
- collect trades
- persist results
- trigger metrics/report generation

This becomes the primary entry point of the system.

---

# 5. Recommended Folder Structure

```text
strategy_lab/
│
├── config/
│   ├── base.yaml
│   ├── costs.yaml
│   ├── instruments.yaml
│   ├── runtime.yaml
│   └── strategies/
│       ├── orb.yaml
│       ├── vwap_pullback.yaml
│       └── gap_behavior.yaml
│
├── data/
│   ├── raw/
│   ├── interim/
│   ├── processed/
│   └── live_cache/
│
├── notebooks/
│   ├── exploratory_analysis.ipynb
│   ├── strategy_review.ipynb
│   ├── replay_debug.ipynb
│   └── live_paper_review.ipynb
│
├── reports/
│   ├── trades/
│   ├── metrics/
│   ├── plots/
│   ├── replay/
│   └── live_paper/
│
├── logs/
│   ├── engine/
│   ├── replay/
│   └── live/
│
├── src/
│   ├── core/
│   │   ├── models.py
│   │   ├── enums.py
│   │   ├── events.py
│   │   ├── state.py
│   │   └── utils.py
│   │
│   ├── data/
│   │   ├── loader.py
│   │   ├── validator.py
│   │   ├── cleaner.py
│   │   ├── sessionizer.py
│   │   └── live_bar_builder.py
│   │
│   ├── features/
│   │   ├── base.py
│   │   ├── intraday.py
│   │   ├── vwap.py
│   │   ├── opening_range.py
│   │   ├── gap.py
│   │   └── snapshot.py
│   │
│   ├── strategies/
│   │   ├── base.py
│   │   ├── orb.py
│   │   ├── vwap_pullback.py
│   │   └── gap_behavior.py
│   │
│   ├── execution/
│   │   ├── base.py
│   │   ├── simulator.py
│   │   ├── paper_executor.py
│   │   ├── risk.py
│   │   └── position_manager.py
│   │
│   ├── runtimes/
│   │   ├── backtest.py
│   │   ├── replay.py
│   │   └── live_paper.py
│   │
│   ├── feeds/
│   │   ├── historical_feed.py
│   │   ├── replay_feed.py
│   │   ├── websocket_feed.py
│   │   └── broker_adapter.py
│   │
│   ├── analytics/
│   │   ├── metrics.py
│   │   ├── drawdown.py
│   │   ├── segmentation.py
│   │   ├── comparison.py
│   │   └── reporting.py
│   │
│   ├── backtest/
│   │   ├── runner.py
│   │   ├── experiment.py
│   │   └── parameter_sweep.py
│   │
│   ├── monitoring/
│   │   ├── logger.py
│   │   ├── alerts.py
│   │   └── heartbeat.py
│   │
│   └── cli/
│       └── main.py
│
├── tests/
│   ├── test_features.py
│   ├── test_orb.py
│   ├── test_vwap_pullback.py
│   ├── test_gap_behavior.py
│   ├── test_simulator.py
│   ├── test_replay_runtime.py
│   ├── test_live_paper_runtime.py
│   └── test_metrics.py
│
├── pyproject.toml
└── README.md
```

---

# 6. Core Domain Models

The system should revolve around a few strongly defined models.

## Candle

```python
@dataclass
class Candle:
    timestamp: datetime
    instrument: str
    open: float
    high: float
    low: float
    close: float
    volume: float
```

## FeatureSnapshot

```python
@dataclass
class FeatureSnapshot:
    session_date: date
    minute_index: int
    prior_close: float | None
    vwap: float | None
    vwap_distance: float | None
    above_vwap: bool
    below_vwap: bool
    or_high: float | None
    or_low: float | None
    or_width: float | None
    or_ready: bool
    gap_pct: float | None
    gap_direction: str | None
    session_high_so_far: float | None
    session_low_so_far: float | None
```

## BarEvent

This is the most important model for the new architecture.

```python
@dataclass
class BarEvent:
    candle: Candle
    features: FeatureSnapshot
    is_bar_closed: bool
    runtime_mode: str   # backtest / replay / live_paper
```

## Signal

```python
@dataclass
class Signal:
    strategy_name: str
    instrument: str
    timestamp: datetime
    direction: str
    entry_type: str
    stop_price: float
    target_price: float
    metadata: dict
```

## Trade

```python
@dataclass
class Trade:
    trade_id: str
    strategy_name: str
    instrument: str
    direction: str
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    exit_time: datetime | None
    exit_price: float | None
    exit_reason: str | None
    qty: int
    gross_pnl: float
    net_pnl: float
    r_multiple: float | None
    runtime_mode: str
    metadata: dict
```

## EngineState

```python
@dataclass
class EngineState:
    instrument: str
    session_date: date
    open_trades: list[Trade]
    closed_trades: list[Trade]
    queued_signals: list[Signal]
    per_strategy_day_trade_count: dict[str, int]
```

## StrategyContext

```python
@dataclass
class StrategyContext:
    bar_event: BarEvent
    engine_state: EngineState
    strategy_config: dict
```

This makes strategy logic independent from whether bars come from:

- historical files
- replay feed
- live websocket aggregation

---

# 7. Strategy Interface

Use one common strategy contract.

```python
class BaseStrategy(ABC):
    name: str

    @abstractmethod
    def generate_signal(self, ctx: StrategyContext) -> Signal | None:
        pass

    def explain_no_signal(self, ctx: StrategyContext) -> str:
        “””Returns a RejectionReason constant. Called by BarEngine when signal is None.”””
        return 'no_signal'

    def reset(self) -> None:
        “””Clear internal state. Called by runtimes at instrument boundaries.
        Stateless strategies can leave this as a no-op.”””
```

Important principles:

- same strategy class for all runtimes
- no separate “live version” of a strategy
- no separate “backtest version” of a strategy
- only runtime behavior changes, not strategy semantics
- stateful strategies (e.g. VWAP Pullback) key internal state by `(instrument, session_date)` to prevent cross-instrument contamination
- `reset()` is called automatically when a runtime is instantiated, ensuring no state bleeds between runs

---

# 8. Feature Engine Design

## Feature computation flow

### Historical Backtest / Replay

1. load session dataframe
2. compute session metadata
3. compute rolling / cumulative intraday features
4. attach feature snapshots to each bar event

### Live Paper

1. receive ticks or broker candles
2. aggregate into finalized 1-minute bars
3. update session cumulative state
4. compute feature snapshot incrementally
5. emit finalized `BarEvent`

## Suggested feature columns

```text
timestamp
instrument
open
high
low
close
volume

session_date
minute_index
prior_close

vwap
vwap_distance
above_vwap
below_vwap

or_high
or_low
or_width
or_ready

gap_pct
gap_direction
gap_bucket

session_high_so_far
session_low_so_far
range_so_far
```

## Key design rule

The formula for features must be identical across:

- backtest
- replay
- live paper

Otherwise the comparison becomes meaningless.

---

# 9. Strategy Logic Placement

A critical separation of concerns:

## Strategies should handle

- setup recognition
- signal generation
- stop and target determination
- setup metadata

## Execution layers should handle

- next bar entry
- stop/target/EOD exits
- position lifecycle
- cost application
- trade logging
- paper fill assumptions

This keeps strategies:

- interpretable
- debuggable
- reusable

and keeps execution:

- deterministic
- testable
- runtime-specific when needed

---

# 10. Runtime Design

The whole architecture now supports three runtimes.

## A. Historical Backtest Runtime

Purpose:

- prove or reject edge across long history
- perform conservative simulation
- generate reports and metrics

### Rules

- signal only on bar close
- entry at next bar open
- conservative stop/target resolution
- include slippage and costs
- force EOD exits

## B. Historical Replay Runtime

Purpose:

- validate the event-driven engine
- simulate live-like behavior on old data
- ensure no hidden dependence on full future dataframe

### Rules

- feed one bar at a time
- same strategy engine
- same position lifecycle
- same logging style as live paper mode

Replay mode acts like a bridge between backtest and live.

## C. Live Paper Runtime

Purpose:

- validate strategies under real market timing and real operational conditions
- still avoid real capital risk

### Responsibilities

- consume live feed or websocket stream
- build 1-minute bars
- finalize bar-close events
- run strategies on finalized bars only
- execute paper entries/exits
- maintain live session state
- log operational and market events
- record differences between expected and observed runtime behavior

---

# 11. Execution Model

The execution model must remain explicit and consistent.

## Entry assumption

- signal generated only after candle close
- actual entry at next candle open or next valid bar open

## Backtest / Replay exit assumption

If both stop and target are touched in same candle:
- assume stop first

This is intentionally conservative.

## Live Paper exit assumption

Since real bar progression happens in time:

- exits are based on actual live bar development and finalized paper rules
- if intrabar data is not available, continue using conservative bar-resolution logic

## Costs

Include:

- slippage per side
- brokerage
- exchange/transaction charges if modeled
- optional instrument-specific cost presets

The point is not perfect tax-accounting.

The point is to avoid fantasy results.

---

# 12. Risk Model Module

Keep V1/V2 risk model simple and deterministic.

## Inputs

- fixed risk per trade
- fixed lot mode or fixed quantity mode
- max trades per day
- max simultaneous trades
- max active trade per strategy
- optional instrument-specific lot size

## Outputs

- quantity
- entry permission
- risk metadata
- strategy-level and day-level constraint validation

For early testing, a simple fixed-lot mode is preferable.

That prevents position sizing complexity from masking strategy quality.

---

# 13. Strategy-Specific Design Notes

## ORB Strategy

### Concept

Opening range breaks and directional continuation.

### Needs

- OR only finalized after 09:30
- breakout confirmed on candle close
- entry next candle open
- stop on opposite side of OR
- no entry after 12:00
- one trade per direction or per rule set

### Metadata to store

- OR width
- breakout candle close strength
- gap context
- entry time bucket

## VWAP Pullback Strategy

### Concept

Trend, pullback to VWAP, then continuation.

### Needs

- bias from price vs VWAP
- pullback detection
- recapture / reject logic
- invalidation on chop or VWAP break-and-hold
- limited trades per day
- no entry after 13:30

### Strong recommendation

Implement as an explicit state machine:

- `trend_established`
- `pullback_detected`
- `recapture_confirmed`
- `invalidated`

This prevents hidden discretion.

## Gap Behavior Strategy

### Concept

Large overnight gap leads to either continuation or fill.

### Needs

- gap threshold filter
- first 15-minute observation range
- continuation branch
- fill branch

### Recommendation

Model as one strategy with two setup types:

- `gap_continuation`
- `gap_fill`

That keeps shared context together and simplifies reporting.

---

# 14. Metrics Engine Design

You want multi-level outputs.

## A. Trade Ledger

One row per trade.

Suggested columns:

- runtime_mode
- strategy
- instrument
- date
- entry_time
- exit_time
- direction
- entry_price
- stop_price
- target_price
- exit_price
- exit_reason
- gross_pnl
- net_pnl
- r_multiple
- gap_bucket
- OR_width_bucket
- entry_hour

## B. Strategy Summary

Grouped by:

- runtime
- strategy
- instrument
- month
- day type
- time bucket

## C. Comparative Runtime Report

Useful because now we have multiple runtimes.

Compare:

- backtest results
- replay results
- live paper results

This helps identify:

- hidden simulator optimism
- strategy degradation in live conditions
- operational anomalies

---

# 15. Reporting Outputs

For every run, save:

## 1. Trade CSV

Full trade ledger.

## 2. Summary JSON

Machine-readable summary metrics.

## 3. Segment CSVs

Breakdowns by:

- strategy
- instrument
- gap bucket
- OR width
- hour
- runtime mode

## 4. Equity Curve Plot

Simple cumulative PnL chart.

## 5. Drawdown Report

Peak-to-trough analysis.

## 6. Runtime Comparison Report

Backtest vs replay vs live paper comparison.

## 7. Engine Logs

Especially important for replay and live paper runtime.

These logs should help answer:

- why signal was generated
- why it was rejected
- why trade was exited
- whether runtime issues occurred

---

# 16. Experiment Configuration

Use YAML configs for everything.

Example:

```yaml
experiment_name: orb_vwap_gap_v2

runtime:
  mode: backtest   # backtest / replay / live_paper

instruments:
  - NIFTY
  - BANKNIFTY

date_range:
  start: 2024-01-01
  end: 2025-12-31

costs:
  slippage_per_side: 2.0
  brokerage_per_trade: 20.0

risk:
  mode: fixed_lot
  lot_size:
    NIFTY: 1
    BANKNIFTY: 1
  max_total_trades_per_day: 4

strategies:
  orb:
    enabled: true
    opening_range_start: "09:15"
    opening_range_end: "09:30"
    no_entry_after: "12:00"
    target_r: 2.0

  vwap_pullback:
    enabled: true
    no_entry_after: "13:30"
    max_trades_per_day: 2
    target_r: 2.0

  gap_behavior:
    enabled: true
    gap_threshold_pct: 0.005
    opening_range_end: "09:30"
    target_r: 2.0

live_paper:
  use_websocket: true
  paper_execution_only: true
  heartbeat_seconds: 5
  bar_close_delay_seconds: 1
```

This makes both research and runtime modes reproducible.

---

# 17. Backtest / Replay / Live Flow

## Common engine flow

```text
initialize config
initialize runtime
initialize strategies
initialize state

for each incoming finalized bar:
    1. update session features/state
    2. process existing open trades
    3. process queued next-bar entries
    4. ask each strategy for signal
    5. validate constraints
    6. queue valid signal for next-bar entry
    7. log everything

on session end:
    8. force EOD exits
    9. finalize ledger
   10. compute metrics / save reports
```

This is the unified event loop.

Only the source of incoming bars changes.

---

# 18. Recommended Internal Build Sequence

## Phase 1 — Shared Foundation ✅

- core models
- data loader
- sessionizer
- feature engine
- event model
- engine state model

## Phase 2 — Backtest Runtime ✅

- conservative simulator
- ORB strategy
- trade ledger
- metrics engine

## Phase 3 — Replay Runtime ✅

- replay feed
- bar-by-bar historical runner
- shared BarEngine (single core for backtest + replay)
- runtime logs
- compare replay vs backtest

## Phase 4 — Add Remaining Strategies ✅

- VWAP Pullback (state machine: IDLE → TREND → PULLBACK → SIGNAL_READY → USED/INVALIDATED)
- Gap Behavior (continuation and fill cases, gap threshold, cutoff time)
- RejectionReason constants and explain_no_signal() on all strategies
- config validation, session validation (warn on bar count / missing minutes)
- IntradaySession feature (session_high_so_far / session_low_so_far)
- Instrument-isolated VWAP state keyed by (instrument, session_date)
- Strategy reset hook (reset_strategies() called at runtime boundaries)
- Composite-key comparison in comparison utility

## Phase 4.5 — Research Workflow Hardening

Most important missing layer before serious research runs.

- Experiment runner: structured invocation with named experiment folders
- Output persistence: trade ledger, event log, summary saved to disk per run
- Comparison artifact saving: backtest vs replay diff reports persisted alongside results
- Session validation policy: `warn / fail / skip` as explicit config options (not just advisory prints)
- Run metadata capture: timestamp, config hash, instrument list, date range recorded with each run

## Phase 5 — Live Paper Runtime

Build in this order:

1. `src/data/live_bar_builder.py` — assembles ticks into 1-minute bars
2. `src/execution/paper_executor.py` — simulates fills against live prices
3. `src/feeds/websocket_feed.py` — consumes real-time bar stream
4. `src/runtimes/live_paper.py` — wires the above through BarEngine
5. `src/monitoring/` — heartbeat, alert hooks, structured run logger

## Phase 6 — Comparative Validation

- compare backtest, replay, live paper
- full trade-level diff reports at scale
- identify deviations before real capital
- tune assumptions only if justified
- avoid parameter overfitting

This sequence preserves scientific discipline while still aligning with your live market concern.

---

# 19. Testing Strategy

Testing becomes even more important now.

## Unit Tests

For:

- VWAP calculation
- OR calculation
- gap calculation
- bar finalization
- next-bar entry logic
- stop/target resolution
- forced EOD exits
- per-strategy trade limits
- runtime state transitions

## Scenario Tests

Synthetic datasets for:

- clean ORB winner
- ORB false breakout
- VWAP pullback continuation
- VWAP chop invalidation
- gap continuation
- gap fill
- both stop and target in same bar
- live bar aggregation edge case
- replay consistency with backtest
- signal generated but rejected due to constraints

## Runtime Consistency Tests

Very important for this design:

- same historical input through backtest and replay should yield comparable results
- same feature formulas should produce same signals across runtimes
- trade lifecycle should remain deterministic where assumptions match

---

# 20. Recommended Libraries

Keep the stack minimal.

- `pandas` for historical data
- `numpy` for numeric logic
- `pyyaml` for config
- `dataclasses` for models
- `pathlib` for file handling
- `matplotlib` for simple plots
- `pytest` for testing

Optional later:

- `polars` for faster data processing
- `pydantic` for config validation
- `websockets` or broker SDK for live feed
- `sqlite` or lightweight DB for persistent live paper logs

But V1/V2 should remain lightweight.

---

# 21. What Not To Build Yet

Even after redesigning for live paper trading, still avoid these for now:

- real money order execution
- options chain complexity
- greeks-based dynamic decisions
- highly optimized multi-parameter brute force search
- AI-based live signal generation
- multi-broker abstraction layer
- portfolio optimization engine
- distributed microservice deployment
- cloud-native infra too early
- dashboard-heavy ops tooling before core validation

The system should still prioritize:

- edge validation
- runtime correctness
- auditability

---

# 22. Architecture Summary Diagram

```text
                          ┌──────────────────────────┐
                          │   Raw Historical Data    │
                          └────────────┬─────────────┘
                                       │
                                       v
                          ┌──────────────────────────┐
                          │   Data Loader/Cleaner    │
                          └────────────┬─────────────┘
                                       │
                                       v
                          ┌──────────────────────────┐
                          │   Session Builder        │
                          └────────────┬─────────────┘
                                       │
                         ┌─────────────┴─────────────┐
                         │ Shared Feature Engine     │
                         │ VWAP / OR / Gap / etc.    │
                         └─────────────┬─────────────┘
                                       │
                                       v
                         ┌───────────────────────────┐
                         │ Shared Strategy Engine    │
                         │ ORB / VWAP / Gap          │
                         └─────────────┬─────────────┘
                                       │
                         ┌─────────────┼─────────────┐
                         │             │             │
                         v             v             v
              ┌────────────────┐ ┌────────────────┐ ┌──────────────────┐
              │ Backtest       │ │ Replay         │ │ Live Paper       │
              │ Runtime        │ │ Runtime        │ │ Runtime          │
              │----------------│ │----------------│ │------------------│
              │ historical bars│ │ hist bars live │ │ live websocket   │
              │ conservative   │ │ same engine    │ │ paper execution  │
              │ simulator      │ │ debug runtime  │ │ session monitor  │
              └──────┬─────────┘ └──────┬─────────┘ └────────┬─────────┘
                     │                  │                    │
                     └────────────┬─────┴────────────┬──────┘
                                  │                  │
                                  v                  v
                          ┌──────────────────────────┐
                          │ Trades / Metrics / Logs  │
                          └────────────┬─────────────┘
                                       │
                                       v
                          ┌──────────────────────────┐
                          │ Reports / Comparisons    │
                          └──────────────────────────┘
```

---

# 23. Final Recommended Design Choice

If one design direction must be locked, it should be this:

**Build a shared event-driven strategy core, and support three runtimes from the architecture level: backtest, replay, and live paper.**

This gives you:

- conservative research validation
- reduced backtest bias
- faster debugging
- easier transition to live market validation
- a single source of truth for strategy logic

This is the right compromise between:

- pure offline research
- and prematurely building a full live system first

---

# 24. Current Status and Next Concrete Step

Phases 1–4 are complete. The system has:

- all three strategies (ORB, VWAP Pullback, Gap Behavior)
- two working runtimes (backtest, replay) sharing a single BarEngine
- 123 passing tests
- structured rejection logging, config validation, session validation
- instrument-isolated strategy state and runtime-boundary resets

**Immediate next: Phase 4.5 — Research Workflow Hardening**

Build in this order:

1. `src/backtest/experiment.py` — experiment runner with named output folders
2. `src/analytics/reporting.py` — persist trade CSV, summary JSON, comparison artifact
3. Session validation policy in `Sessionizer` — `warn / fail / skip` config option
4. Run metadata capture — timestamp, config hash, git ref saved alongside results
5. `main.py` → evolve into a proper experiment CLI

**Then Phase 5 — Live Paper Runtime**

1. `src/data/live_bar_builder.py`
2. `src/execution/paper_executor.py`
3. `src/feeds/websocket_feed.py`
4. `src/runtimes/live_paper.py`
5. `src/monitoring/` (heartbeat, alerts, logger)

That way, every strategy enters a stronger validation pipeline before capital is touched.

---

# Final Principle

**First prove edge conservatively. Then validate behavior in replay. Then validate in live paper market. Then deploy small capital. Then optimize. Then scale.**
