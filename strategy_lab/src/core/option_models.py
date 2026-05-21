"""
Option-aware data models for multi-leg options strategies.

Kept separate from src/core/models.py so the single-leg path used by
existing strategies (ORB, VWAP Pullback, Gap) is untouched. The two
ledgers (Trade vs MultiLegTrade) run in parallel on EngineState.
"""
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, Dict, List


@dataclass
class OptionLeg:
    """One leg of a multi-leg structure. Sign of position carried by `side`."""
    instrument: str          # underlying, e.g. "NIFTY"
    expiry: date
    strike: float
    option_type: str         # "CE" | "PE"
    side: str                # "BUY" | "SELL"
    qty: int                 # in lots; always positive


@dataclass
class ChainQuote:
    strike: float
    option_type: str         # "CE" | "PE"
    bid: float
    ask: float
    last: float
    iv: float                # implied vol as decimal (0.15 == 15%)


@dataclass
class ChainSnapshot:
    """A point-in-time snapshot of the option chain for one expiry."""
    timestamp: datetime
    underlying: str
    spot: float
    expiry: date
    quotes: List[ChainQuote]
    atm_iv: float            # IV of strike nearest spot, convenience field

    def quote(self, strike: float, option_type: str) -> Optional[ChainQuote]:
        """Look up a single quote. None if not present in this snapshot."""
        for q in self.quotes:
            if q.strike == strike and q.option_type == option_type:
                return q
        return None


@dataclass
class MultiLegSignal:
    """Emitted by a multi-leg strategy. Routed by the engine to MultiLegSimulator."""
    strategy_name: str
    instrument: str          # underlying
    timestamp: datetime
    structure_type: str      # e.g. "IRON_FLY"
    legs: List[OptionLeg]
    metadata: Dict = field(default_factory=dict)


@dataclass
class LegFill:
    """Per-leg fill record. Stored on MultiLegTrade for entry and exit."""
    leg: OptionLeg
    fill_price: float
    fill_time: datetime


@dataclass
class MultiLegTrade:
    """
    A complete multi-leg trade lifecycle: entry fills → optional exit fills.
    Mirrors the role of `Trade` for single-leg strategies but holds N legs.
    """
    trade_id: str
    strategy_name: str
    instrument: str
    structure_type: str
    entry_time: datetime
    entry_fills: List[LegFill]
    net_entry_credit: float          # positive = credit received at entry
    exit_time: Optional[datetime] = None
    exit_fills: List[LegFill] = field(default_factory=list)
    net_exit_debit: Optional[float] = None    # positive = debit paid to close
    exit_reason: Optional[str] = None
    gross_pnl: Optional[float] = None
    net_pnl: Optional[float] = None
    runtime_mode: str = 'backtest'
    metadata: Dict = field(default_factory=dict)
    lot_size: int = 1                # rupee-multiplier per leg unit (e.g. NIFTY=25)
