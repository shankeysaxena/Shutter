from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, Dict, List

from src.core.option_models import ChainSnapshot, MultiLegTrade, MultiLegSignal

@dataclass
class Candle:
    timestamp: datetime
    instrument: str
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass
class FeatureSnapshot:
    session_date: date
    minute_index: int
    prior_close: Optional[float]
    vwap: Optional[float]
    vwap_distance: Optional[float]
    above_vwap: bool
    below_vwap: bool
    or_high: Optional[float]
    or_low: Optional[float]
    or_width: Optional[float]
    or_ready: bool
    gap_pct: Optional[float]
    gap_direction: Optional[str]
    session_high_so_far: Optional[float]
    session_low_so_far: Optional[float]
    # ATR-normalised VWAP deviation — added by IntradayATRFeature.
    # None when ATR hasn't warmed up yet (first ~14 bars of each session).
    intraday_atr: Optional[float] = None
    vwap_atr_distance: Optional[float] = None

@dataclass
class BarEvent:
    candle: Candle
    features: FeatureSnapshot
    is_bar_closed: bool
    runtime_mode: str   # backtest / replay / live_paper

@dataclass
class Signal:
    strategy_name: str
    instrument: str
    timestamp: datetime
    direction: str
    entry_type: str
    stop_price: float
    target_price: float
    metadata: Dict = field(default_factory=dict)

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
    exit_time: Optional[datetime]
    exit_price: Optional[float]
    exit_reason: Optional[str]
    qty: int
    gross_pnl: float
    net_pnl: float
    r_multiple: Optional[float]
    runtime_mode: str
    metadata: Dict = field(default_factory=dict)

@dataclass
class EngineState:
    instrument: str
    session_date: date
    open_trades: List[Trade]
    closed_trades: List[Trade]
    queued_signals: List[Signal]
    per_strategy_day_trade_count: Dict[str, int]
    # Parallel ledger for multi-leg (options) strategies. Default empty so
    # callers that build EngineState the old way still work unchanged.
    open_multi_leg_trades: List[MultiLegTrade] = field(default_factory=list)
    closed_multi_leg_trades: List[MultiLegTrade] = field(default_factory=list)
    queued_multi_leg_signals: List[MultiLegSignal] = field(default_factory=list)

@dataclass
class StrategyContext:
    bar_event: BarEvent
    engine_state: EngineState
    strategy_config: Dict
    # Set by the runtime when an option-chain feed is configured.
    chain_snapshot: Optional[ChainSnapshot] = None
    # Set by AllocationGatedStrategy — the allocator's SessionState for the
    # current bar. Strategies that need the compression range read it from here.
    # None when not running through the allocator (e.g. direct backtest).
    market_state: Optional[object] = None
