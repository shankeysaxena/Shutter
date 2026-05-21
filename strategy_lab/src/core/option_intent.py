"""
OptionIntent — intermediate abstraction between underlying signal and option execution.

Strategies understand market behaviour (direction, stop, target). They should not
know about expiry, strike, or option-chain liquidity. OptionIntent carries the
market view; OptionsTranslationLayer converts it into a concrete MultiLegSignal.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

DIRECTION_BULLISH      = 'BULLISH'
DIRECTION_BEARISH      = 'BEARISH'

STRUCTURE_LONG_OPTION  = 'LONG_OPTION'
STRUCTURE_DEBIT_SPREAD = 'DEBIT_SPREAD'


@dataclass
class OptionIntent:
    strategy_name: str
    instrument: str               # NIFTY | BANKNIFTY
    timestamp: datetime
    direction: str                # BULLISH | BEARISH
    preferred_structure: str      # LONG_OPTION | DEBIT_SPREAD
    underlying_entry: float
    underlying_stop: Optional[float]
    underlying_target: Optional[float]
    max_hold_minutes: int
    metadata: Dict = field(default_factory=dict)
