from typing import Optional
from src.core.models import Trade, Signal, BarEvent
import uuid

class BacktestSimulator:
    """Conservatively simulates trade execution for backtesting."""

    def __init__(self, slippage_per_side: float = 0.0, brokerage: float = 0.0):
        self.slippage = slippage_per_side
        self.brokerage = brokerage

    def process_signal_for_entry(self, signal: Signal, bar_event: BarEvent, qty: int = 1) -> Optional[Trade]:
        """
        Fills a queued signal at the open of the current bar (next bar after signal).
        Target is computed from the actual fill price so that R is consistent.
        Policy: same-bar fill-and-exit is allowed (conservative stop-first rule applies).
        """
        raw_entry = bar_event.candle.open
        risk = abs(raw_entry - signal.stop_price)

        if signal.direction == 'LONG':
            entry_price = raw_entry + self.slippage
        elif signal.direction == 'SHORT':
            entry_price = raw_entry - self.slippage
        else:
            entry_price = raw_entry

        # Honour an explicit target_price when the strategy sets one (e.g. VWAP
        # reversion targets the live VWAP level, not a fixed R multiple).
        # Existing strategies set target_price=0.0 as a placeholder, so this
        # branch only activates for strategies that provide a real target.
        if signal.target_price != 0.0:
            target_price = signal.target_price
        else:
            # Risk is anchored to the raw open so slippage doesn't inflate the target
            target_r = signal.metadata.get('target_r', 2.0)
            if signal.direction == 'LONG':
                target_price = entry_price + risk * target_r
            else:
                target_price = entry_price - risk * target_r

        trade = Trade(
            trade_id=str(uuid.uuid4()),
            strategy_name=signal.strategy_name,
            instrument=signal.instrument,
            direction=signal.direction,
            entry_time=bar_event.candle.timestamp,
            entry_price=entry_price,
            stop_price=signal.stop_price,
            target_price=target_price,
            exit_time=None,
            exit_price=None,
            exit_reason=None,
            qty=qty,
            gross_pnl=0.0,
            net_pnl=0.0,
            r_multiple=None,
            runtime_mode=bar_event.runtime_mode,
            metadata=signal.metadata
        )
        return trade
        
    def check_exits(self, trade: Trade, bar_event: BarEvent, is_eod: bool = False) -> bool:
        """Checks target/stop conditions and EOD constraints. Returns True if trade closed."""
        if trade.exit_time is not None:
            return True
            
        high = bar_event.candle.high
        low = bar_event.candle.low
        close = bar_event.candle.close
        timestamp = bar_event.candle.timestamp
        
        exit_price = None
        exit_reason = None
        
        if is_eod:
            exit_price = close
            exit_reason = 'EOD'
        else:
            hit_stop = False
            hit_target = False
            
            if trade.direction == 'LONG':
                if low <= trade.stop_price:
                    hit_stop = True
                if high >= trade.target_price:
                    hit_target = True
            elif trade.direction == 'SHORT':
                if high >= trade.stop_price:
                    hit_stop = True
                if low <= trade.target_price:
                    hit_target = True
                    
            if hit_stop and hit_target:
                # Conservative assumption: hit stop first
                exit_price = trade.stop_price
                exit_reason = 'STOP'
            elif hit_stop:
                exit_price = trade.stop_price
                exit_reason = 'STOP'
            elif hit_target:
                exit_price = trade.target_price
                exit_reason = 'TARGET'
                
        if exit_price is not None:
            if trade.direction == 'LONG':
                actual_exit = exit_price - self.slippage
            else:
                actual_exit = exit_price + self.slippage
                
            trade.exit_price = actual_exit
            trade.exit_time = timestamp
            trade.exit_reason = exit_reason
            
            if trade.direction == 'LONG':
                trade.gross_pnl = (trade.exit_price - trade.entry_price) * trade.qty
            else:
                trade.gross_pnl = (trade.entry_price - trade.exit_price) * trade.qty
                
            trade.net_pnl = trade.gross_pnl - self.brokerage
            risk_amount = abs(trade.entry_price - trade.stop_price)
            if risk_amount > 0:
                trade.r_multiple = trade.gross_pnl / (risk_amount * trade.qty)
            return True
            
        return False
