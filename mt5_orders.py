#!/usr/bin/env python
"""
MT5 Order Execution Module
==========================
Safe order execution with full error handling, result logging, and
slippage tracking. Integrates with RiskEngine for pre-trade validation.

Key features:
- Pre-trade risk check enforcement
- Full error code mapping (MT5 trade return codes)
- Slippage calculation and logging
- Per-trade audit trail
- Retry logic for recoverable errors
- Partial fill handling

Usage:
    from mt5_risk import RiskEngine
    from mt5_orders import OrderExecutor

    risk = RiskEngine()
    executor = OrderExecutor(risk)
    result = executor.execute_order(
        symbol="EURUSD",
        order_type=mt5.ORDER_TYPE_BUY,
        volume=0.01,
        price=None,  # market order
        sl=1.16700,
        tp=1.16800,
    )
"""
import MetaTrader5 as mt5
import time
import json
import os
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import IntEnum

logger = logging.getLogger(__name__)


class TradeRetcode(IntEnum):
    """
    MT5 trade return codes — mapped from official MT5 documentation.
    https://www.mql5.com/en/docs/trading/trading_trade_result
    """
    TRADE_RETCODE_REQUOTE = 0
    TRADE_RETCODE_DONE = 1
    TRADE_RETCODE_ACCEPTED = 2
    TRADE_RETCODE_INVALID = 3
    TRADE_RETCODE_ERROR = 4
    TRADE_RETCODE_CURRENCY_DISABLED = 5
    TRADE_RETCODE_PRICE_INVALID = 6
    TRADE_RETCODE_INVALID_STOPS = 7
    TRADE_RETCODE_TRADE_DISABLED = 8
    TRADE_RETCODE_TRADE_CONTEXT_BUSY = 9
    TRADE_RETCODE_TRADE_PRICE = 10
    TRADE_RETCODE_PRICE_CHANGED = 11
    TRADE_RETCODE_TRADE_CLOSE_INCOMPLETE = 12
    TRADE_RETCODE_TRADE_CLOSE_OFFLINE = 13
    TRADE_RETCODE_TRADE_CLOSE_DISABLED = 14
    TRADE_RETCODE_TRADE_CLOSE_REJECTED = 15
    TRADE_RETCODE_TRADE_REQUOTE = 16
    TRADE_RETCODE_TRADE_TIMEOUT = 17
    TRADE_RETCODE_INVALID_EXPIRATION = 18
    TRADE_RETCODE_ORDER_CHANGED = 19
    TRADE_RETCODE_PRICE_OFF = 20
    TRADE_RETCODE_INVALID_PRICE = 21
    TRADE_RETCODE_INVALID_TRADE_VOLUME = 22
    TRADE_RETCODE_MARKUP_CALL = 23
    TRADE_RETCODE_NOT_ENOUGH_MONEY = 24
    TRADE_RETCODE_PRICE_CHANGED_TRADE_REQUOTE = 25
    TRADE_RETCODE_TRADE_CONTEXT_BUSY_REQUOTE = 26
    TRADE_RETCODE_TRADE_BUY_ONLY = 27
    TRADE_RETCODE_TRADE_SELL_ONLY = 28
    TRADE_RETCODE_TRADE_CLOSE_ONLY = 29
    TRADE_RETCODE_TRADE_CLOSE_ALL = 30
    TRADE_RETCODE_TRADE_CLOSE_ALL_CANCELLED = 31
    TRADE_RETCODE_TRADE_CONTEXT_BUSY = -10009
    TRADE_RETCODE_INVALID_STOPS = -10016


RET_CODE_MESSAGES = {
    TradeRetcode.TRADE_RETCODE_REQUOTE: "Requote — price changed, request new quote",
    TradeRetcode.TRADE_RETCODE_DONE: "Order completed successfully",
    TradeRetcode.TRADE_RETCODE_ACCEPTED: "Order accepted",
    TradeRetcode.TRADE_RETCODE_INVALID: "Invalid request",
    TradeRetcode.TRADE_RETCODE_ERROR: "General error",
    TradeRetcode.TRADE_RETCODE_TRADE_CONTEXT_BUSY: "Trade context is busy",
    TradeRetcode.TRADE_RETCODE_INVALID_STOPS: "Invalid stop levels",
    TradeRetcode.TRADE_RETCODE_TRADE_DISABLED: "Trading is disabled",
    TradeRetcode.TRADE_RETCODE_PRICE_INVALID: "Invalid price",
    TradeRetcode.TRADE_RETCODE_TRADE_PRICE: "No connection",
    TradeRetcode.TRADE_RETCODE_TRADE_TIMEOUT: "Trade timeout",
    TradeRetcode.TRADE_RETCODE_NOT_ENOUGH_MONEY: "Not enough money",
    TradeRetcode.TRADE_RETCODE_INVALID_TRADE_VOLUME: "Invalid volume",
    TradeRetcode.TRADE_RETCODE_TRADE_BUY_ONLY: "Buy-only restriction",
    TradeRetcode.TRADE_RETCODE_TRADE_SELL_ONLY: "Sell-only restriction",
    TradeRetcode.TRADE_RETCODE_TRADE_CLOSE_ONLY: "Close-only restriction",
}


@dataclass
class OrderResult:
    """Result of an order execution attempt."""
    success: bool
    order_type: str
    symbol: str
    volume: float
    entry_price: float
    sl: Optional[float]
    tp: Optional[float]
    slippage: float
    retcode: int
    retcode_description: str
    deal: Optional[Dict[str, Any]]
    order: Optional[Dict[str, Any]]
    comment: str
    timestamp: float
    retry_count: int = 0
    error_details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "order_type": self.order_type,
            "symbol": self.symbol,
            "volume": self.volume,
            "entry_price": self.entry_price,
            "sl": self.sl,
            "tp": self.tp,
            "slippage": self.slippage,
            "retcode": self.retcode,
            "retcode_description": self.retcode_description,
            "deal": self.deal,
            "order": self.order,
            "comment": self.comment,
            "timestamp": self.timestamp,
            "retry_count": self.retry_count,
            "error_details": self.error_details,
        }


class OrderExecutor:
    """
    Executes trades with full error handling, logging, and risk enforcement.
    """

    # Retryable return codes — safe to retry after a brief delay
    RETRYABLE_RETCODES = {
        TradeRetcode.TRADE_RETCODE_REQUOTE,
        TradeRetcode.TRADE_RETCODE_TRADE_CONTEXT_BUSY,
        TradeRetcode.TRADE_RETCODE_TRADE_PRICE,
        TradeRetcode.TRADE_RETCODE_PRICE_CHANGED,
    }

    # Maximum retry attempts for recoverable errors
    MAX_RETRIES = 3
    RETRY_DELAY = 1.0  # seconds
    MAX_DEVIATION = 20  # points for market orders

    def __init__(self, risk_engine=None, magic: int = 234000):
        self.risk = risk_engine
        self.magic = magic
        self._audit_log_path = os.path.join(
            os.path.dirname(__file__), "logs", "order_audit.jsonl"
        )
        os.makedirs(os.path.dirname(self._audit_log_path), exist_ok=True)

    def _log_audit(self, event: str, details: dict):
        """Append to order audit log."""
        entry = {
            "timestamp": time.time(),
            "event": event,
            "details": details,
        }
        with open(self._audit_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def _get_current_price(
        self, order_type: str, symbol: str
    ) -> Optional[float]:
        """Gets the current market price for order execution."""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None

        if order_type == "BUY":
            return tick.ask
        elif order_type == "SELL":
            return tick.bid
        else:
            return None

    def execute_order(
        self,
        symbol: str,
        order_type: str,
        volume: float,
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        comment: str = "ai-agent-signal",
        max_retries: int = 3,
        deviation: int = 20,
    ) -> OrderResult:
        """
        Executes a trade order with full error handling.

        Args:
            symbol: Trading symbol (e.g., "EURUSD")
            order_type: "BUY" or "SELL"
            volume: Lot size (e.g., 0.01)
            price: Entry price (None for market order)
            sl: Stop loss price
            tp: Take profit price
            comment: Order comment for audit trail
            max_retries: Maximum retry attempts for recoverable errors
            deviation: Max price deviation in points

        Returns:
            OrderResult with full execution details
        """
        self._log_audit("order_attempt", {
            "symbol": symbol,
            "order_type": order_type,
            "volume": volume,
            "price": price,
            "sl": sl,
            "tp": tp,
        })

        # Pre-trade risk check
        if self.risk:
            self.risk.update()  # Refresh risk snapshot
            entry_price_for_risk = price if price else self._get_current_price(order_type, symbol)
            if entry_price_for_risk is None:
                entry_price_for_risk = 0.0

            allowed, reason = self.risk.allow_entry(symbol, volume, entry_price_for_risk, sl or 0.0)
            if not allowed:
                self._log_audit("order_blocked_by_risk", {
                    "reason": reason,
                    "symbol": symbol,
                    "volume": volume,
                })
                return OrderResult(
                    success=False,
                    order_type=order_type,
                    symbol=symbol,
                    volume=volume,
                    entry_price=0.0,
                    sl=sl,
                    tp=tp,
                    slippage=0.0,
                    retcode=TradeRetcode.TRADE_RETCODE_INVALID,
                    retcode_description=f"Blocked by risk engine: {reason}",
                    deal=None,
                    order=None,
                    comment=comment,
                    timestamp=time.time(),
                    error_details={"risk_block_reason": reason},
                )

        # Map order type string to MT5 constant
        type_map = {
            "BUY": mt5.ORDER_TYPE_BUY,
            "SELL": mt5.ORDER_TYPE_SELL,
            "BUY_LIMIT": mt5.ORDER_TYPE_BUY_LIMIT,
            "SELL_LIMIT": mt5.ORDER_TYPE_SELL_LIMIT,
            "BUY_STOP": mt5.ORDER_TYPE_BUY_STOP,
            "SELL_STOP": mt5.ORDER_TYPE_SELL_STOP,
        }

        if order_type not in type_map:
            return OrderResult(
                success=False,
                order_type=order_type,
                symbol=symbol,
                volume=volume,
                entry_price=0.0,
                sl=sl,
                tp=tp,
                slippage=0.0,
                retcode=TradeRetcode.TRADE_RETCODE_INVALID,
                retcode_description=f"Invalid order type: {order_type}",
                deal=None,
                order=None,
                comment=comment,
                timestamp=time.time(),
            )

        # Get execution price for market orders
        if price is None:
            price = self._get_current_price(order_type, symbol)
            if price is None:
                return OrderResult(
                    success=False,
                    order_type=order_type,
                    symbol=symbol,
                    volume=volume,
                    entry_price=0.0,
                    sl=sl,
                    tp=tp,
                    slippage=0.0,
                    retcode=TradeRetcode.TRADE_RETCODE_PRICE_INVALID,
                    retcode_description="Could not get current price",
                    deal=None,
                    order=None,
                    comment=comment,
                    timestamp=time.time(),
                )

        # Build the request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": type_map[order_type],
            "price": price,
            "deviation": deviation,
            "magic": self.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
            "sl": sl if sl else 0.0,
            "tp": tp if tp else 0.0,
        }

        # Execute with retries
        last_result = None
        for attempt in range(max_retries + 1):
            self._log_audit("order_send", {
                "attempt": attempt + 1,
                "request": request,
            })

            result = mt5.order_send(request)
            last_result = result

            if result is None:
                error = mt5.last_error()
                self._log_audit("order_send_failed", {
                    "attempt": attempt + 1,
                    "error": {"code": error[0], "description": error[1]},
                })
                if attempt < max_retries:
                    time.sleep(self.RETRY_DELAY)
                    continue
                return OrderResult(
                    success=False,
                    order_type=order_type,
                    symbol=symbol,
                    volume=volume,
                    entry_price=price,
                    sl=sl,
                    tp=tp,
                    slippage=0.0,
                    retcode=TradeRetcode.TRADE_RETCODE_ERROR,
                    retcode_description=f"order_send returned None: {error}",
                    deal=None,
                    order=None,
                    comment=comment,
                    timestamp=time.time(),
                    retry_count=attempt,
                    error_details={"code": error[0], "description": error[1]},
                )

            # Check result
            retcode = result.retcode
            retcode_desc = result.comment or ""

            if retcode == TradeRetcode.TRADE_RETCODE_DONE:
                # Success
                deal_price = result.deal
                slippage = 0.0
                if result.ask and result.bid:
                    if order_type == "BUY":
                        slippage = price - result.price
                    else:
                        slippage = result.price - price

                self._log_audit("order_executed", {
                    "symbol": symbol,
                    "order_type": order_type,
                    "volume": volume,
                    "price": result.price,
                    "slippage": slippage,
                    "retcode": retcode,
                    "retcode_desc": retcode_desc,
                    "deal": result.deal,
                    "order": result.order,
                })

                if self.risk:
                    self.risk.record_order_result(True)

                return OrderResult(
                    success=True,
                    order_type=order_type,
                    symbol=symbol,
                    volume=volume,
                    entry_price=result.price,
                    sl=result.sl if result.sl else sl,
                    tp=result.tp if result.tp else tp,
                    slippage=slippage,
                    retcode=retcode,
                    retcode_description=retcode_desc,
                    deal=result.deal,
                    order=result.order,
                    comment=comment,
                    timestamp=time.time(),
                    retry_count=attempt,
                )

            # Check if retryable
            if retcode in self.RETRYABLE_RETCODES and attempt < max_retries:
                self._log_audit("order_retrying", {
                    "attempt": attempt + 1,
                    "retcode": retcode,
                    "retcode_desc": retcode_desc,
                    "delay": self.RETRY_DELAY,
                })
                time.sleep(self.RETRY_DELAY)
                continue

            # Non-retryable or max retries reached
            self._log_audit("order_failed", {
                "symbol": symbol,
                "retcode": retcode,
                "retcode_desc": retcode_desc,
                "request": request,
            })

            if self.risk:
                self.risk.record_order_result(False, {
                    "retcode": retcode,
                    "description": retcode_desc,
                })

            return OrderResult(
                success=False,
                order_type=order_type,
                symbol=symbol,
                volume=volume,
                entry_price=price,
                sl=sl,
                tp=tp,
                slippage=0.0,
                retcode=retcode,
                retcode_description=retcode_desc,
                deal=None,
                order=None,
                comment=comment,
                timestamp=time.time(),
                retry_count=attempt,
                error_details={"retcode": retcode, "description": retcode_desc},
            )

        # Should never reach here
        return OrderResult(
            success=False,
            order_type=order_type,
            symbol=symbol,
            volume=volume,
            entry_price=price,
            sl=sl,
            tp=tp,
            slippage=0.0,
            retcode=TradeRetcode.TRADE_RETCODE_ERROR,
            retcode_description="Max retries exhausted",
            deal=None,
            order=None,
            comment=comment,
            timestamp=time.time(),
            retry_count=max_retries,
        )

    def close_position(
        self,
        position_ticket: int,
        comment: str = "ai-agent-close",
    ) -> OrderResult:
        """
        Closes an open position by ticket number.

        Args:
            position_ticket: MT5 position ticket
            comment: Audit trail comment
        """
        # Get position details
        positions = mt5.positions_get(ticket=position_ticket)
        if not positions:
            return OrderResult(
                success=False,
                order_type="CLOSE",
                symbol="",
                volume=0.0,
                entry_price=0.0,
                sl=None,
                tp=None,
                slippage=0.0,
                retcode=TradeRetcode.TRADE_RETCODE_INVALID,
                retcode_description="Position not found",
                deal=None,
                order=None,
                comment=comment,
                timestamp=time.time(),
            )

        pos = positions[0]
        symbol = pos.symbol
        volume = pos.volume

        # Determine close direction
        if pos.type == mt5.POSITION_TYPE_BUY:
            order_type = "SELL"
            price = mt5.symbol_info_tick(symbol).bid
        else:
            order_type = "BUY"
            price = mt5.symbol_info_tick(symbol).ask

        # Build close request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_SELL if order_type == "SELL" else mt5.ORDER_TYPE_BUY,
            "price": price,
            "deviation": 20,
            "magic": self.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
            "position": position_ticket,
        }

        result = mt5.order_send(request)
        if result is None:
            err = mt5.last_error()
            return OrderResult(
                success=False,
                order_type="CLOSE",
                symbol=symbol,
                volume=volume,
                entry_price=0.0,
                sl=None,
                tp=None,
                slippage=0.0,
                retcode=TradeRetcode.TRADE_RETCODE_ERROR,
                retcode_description=f"order_send returned None: {err}",
                deal=None,
                order=None,
                comment=comment,
                timestamp=time.time(),
            )

        return OrderResult(
            success=result.retcode == TradeRetcode.TRADE_RETCODE_DONE,
            order_type="CLOSE",
            symbol=symbol,
            volume=volume,
            entry_price=result.price if result else 0.0,
            sl=None,
            tp=None,
            slippage=0.0,
            retcode=result.retcode,
            retcode_description=result.comment,
            deal=result.deal,
            order=result.order,
            comment=comment,
            timestamp=time.time(),
        )

    def emergency_flatten(self, comment: str = "emergency_flatten") -> List[OrderResult]:
        """
        Immediately closes all open positions.
        Used when risk state is EMERGENCY_FLATTEN.
        """
        results = []
        positions = mt5.positions_get()
        if not positions:
            logger.info("Emergency flatten: no positions to close")
            return results

        for pos in positions:
            result = self.close_position(pos.ticket, comment=comment)
            results.append(result)
            if result.success:
                logger.warning(f"Emergency close: {pos.symbol} closed (ticket={pos.ticket})")
            else:
                logger.error(f"Emergency close FAILED: {pos.symbol} (ticket={pos.ticket}): {result.retcode_description}")

        return results

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Returns all open positions as dicts."""
        positions = mt5.positions_get()
        if not positions:
            return []
        return [pos._asdict() for pos in positions]


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    import sys
    sys.path.insert(0, "/home/rebawn/trader")

    from mt5_connection import MT5Manager
    from mt5_risk import RiskEngine

    mgr = MT5Manager()
    if not mgr.ensure_connection():
        print("ERROR: MT5 not connected")
        exit(1)

    risk = RiskEngine()
    executor = OrderExecutor(risk_engine=risk)

    print("\n=== Order Executor Test ===")
    print(f"Magic number: {executor.magic}")

    # Show open positions
    positions = executor.get_open_positions()
    print(f"\nOpen positions: {len(positions)}")
    for pos in positions:
        print(f"  {pos['symbol']}: volume={pos['volume']}, price_open={pos['price_open']}, type={pos['type']}")

    # Test a small market order (0.01 lots — minimal risk on demo)
    print("\n=== Sending test BUY order (EURUSD, 0.01 lots) ===")
    result = executor.execute_order(
        symbol="EURUSD",
        order_type="BUY",
        volume=0.01,
        sl=1.16700,
        tp=1.16800,
    )
    print(f"  Success: {result.success}")
    print(f"  Price: {result.entry_price}")
    print(f"  Slippage: {result.slippage}")
    print(f"  Retcode: {result.retcode} ({result.retcode_description})")
    print(f"  Retries: {result.retry_count}")
    if result.deal:
        print(f"  Deal ID: {result.deal}")
    if result.order:
        print(f"  Order ID: {result.order}")

    mgr.shutdown()
