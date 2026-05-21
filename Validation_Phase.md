# 📘 Stocks / FnO Strategy Lab — Design Doc (V1)

---

## 1. Objective

Build a validation-first intraday strategy lab for:

- NIFTY
- BANKNIFTY

### Goal

- Identify and validate robust, rule-based strategies
- Avoid AI complexity initially
- Move from backtest → paper → live

---

## 2. Scope (V1)

### Included

- ORB (Opening Range Breakout)
- VWAP Pullback Continuation
- Gap Behaviour (Continuation / Fill)
- Realistic backtesting
- Deterministic rules

### Not Included

- Options strategies
- Expiry day logic
- AI/LLM decision making
- Tick-level trading
- Multi-asset expansion

---

## 3. Core Design Philosophy

- Simple → Test → Validate → Improve
- Deterministic over AI
- Structure-based trading
- No-trade is valid
- Backtest = Live behavior

---

## 4. Data Model

### Base Data

- Resolution: 1-minute OHLCV
- Instruments: NIFTY, BANKNIFTY

Each candle:
timestamp
open
high
low
close
volume

---

### Derived Features

- VWAP (resets daily)
- Opening Range (5 / 10 / 15 min)
- Gap %
- Volatility
- VWAP slope
- Distance from VWAP

---

## 5. Strategy Definitions

---

### 🔵 Strategy 1 — ORB

#### Intuition

Opening balance breaks → trend begins

#### Setup

- Opening Range: 09:15–09:30
- OR_high = max(high)
- OR_low  = min(low)

#### Entry

- Long → 1-min close > OR_high
- Short → 1-min close < OR_low
- Entry → next candle open

#### Stop

- Opposite side of OR

#### Target

- 2R

#### Constraints

- Max 1 trade per direction
- No entry after 12:00

#### Avoid When

- Very small OR
- Frequent re-entry into range
- Late breakout

---

### 🟢 Strategy 2 — VWAP Pullback

#### Intuition

Trend → pullback to value → continuation

#### Setup

- Price above VWAP → bullish
- Price below VWAP → bearish

#### Entry

- After pullback
- Close above VWAP → long
- Close below VWAP → short
- Entry → next candle open

#### Stop

- Below pullback low (long)
- Above pullback high (short)

#### Target

- 2R

#### Constraints

- Max 2 trades per day
- No trades after 13:30

#### Invalidation

- Price breaks VWAP and stays
- Pullback low breaks
- VWAP chop

---

### 🟡 Strategy 3 — Gap Behaviour

#### Intuition

Overnight imbalance → continuation or correction

#### Setup

- Gap % = (today open - yesterday close) / yesterday close
- Trade only if gap above threshold

#### Observation Phase

- First 15 minutes define range

---

#### Case A — Gap Continuation

**Entry**

- Break above OR_high → long

**Stop**

- Below OR_low

**Target**

- 2R

---

#### Case B — Gap Fill

**Entry**

- Break below OR_low → short

**Stop**

- Above OR_high

**Target**

- Previous day close OR 2R

---

#### Constraints

- Only 1 trade per day

---

## 6. Strategy Interaction Rules

- Multiple strategies allowed
- Max 1 active trade per strategy
- Max 3–4 trades per day

---

## 7. Risk Model

### Per Trade

- Fixed risk = 1R

### Daily Limit

- Max 3–4 trades

### Exit Rules

- Target hit
- Stop hit
- End of day exit

---

## 8. Backtesting Assumptions

### Execution

- Signal on candle close
- Entry on next candle open

### Include

- Slippage
- Brokerage
- Transaction costs

### Constraints

- No lookahead bias
- Deterministic execution

---

## 9. Metrics to Track

- Total PnL
- Win rate
- Average win
- Average loss
- Expectancy
- Profit factor
- Max drawdown
- Consecutive losses

### Segmentation

- NIFTY vs BANKNIFTY
- Time of day
- Gap size
- OR width
- Volatility

---

## 10. Robustness Testing

- OR duration (5/10/15 min)
- Stop loss variation
- Target variation
- Gap threshold variation
- Time filters
- Slippage sensitivity

---

## 11. System Architecture
Market Data (1-min candles)
↓
Feature Engine (VWAP, OR, gap)
↓
Strategy Engine (ORB / VWAP / Gap)
↓
Trade Simulator
↓
Metrics Engine
↓
Analysis Layer


---

## 12. Trade Log Schema
date
instrument
strategy
entry_time
entry_price
stop_price
target_price
exit_price
exit_reason (SL / TARGET / TIME)
R_multiple
gap_percent
or_width
vwap_distance

---

## 13. Execution Phases

- Phase 1 → Backtest
- Phase 2 → Paper trading
- Phase 3 → Small capital live
- Phase 4 → Scale

---

## 14. Known Risks

- Strategy may not have edge
- Overfitting
- Regime dependency
- Execution mismatch

---

## 15. Future Scope

- AI-based strategy filtering
- Premarket intelligence
- Regime detection
- Options layer

---

## 16. Final Principle

**Prove edge first. Then optimize. Then scale. Then add intelligence.**
