#!/usr/bin/env python
"""
MT5 Live Trading Loop
=====================
Signal → Risk → Execution pipeline for live trading.
Designed for short positions on M1 timeframe, with full safety enforcement.

Architecture:
1. MarketDataLoop — fetches bars/ticks on interval, computes indicators
2. SignalGenerator — runs EMA crossover + VWAP filter strategy
3. RiskAgent — independent hard-limit checks (no strategy override)
4. OrderExecutor — sends orders with retry, slippage tracking
5. AuditLogger — records every decision, reasoning, and outcome

Usage:
    python mt5_live.py --symbol EURUSD --timeframe M1 --interval 10
"""
import MetaTrader5 as mt5
import time
import logging
import json
import os
import argparse
import signal
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict
from enum import Enum

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))

from mt5_connection import MT5Manager
from mt5_data import DataFetcher, TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_H1
from mt5_indicators import IndicatorPipeline
from mt5_risk import RiskEngine, RiskState
from mt5_orders import OrderExecutor, OrderResult

logger = logging.getLogger(__name__)

# Trading config file path
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "live_trading.json")


@dataclass
class TradingSignal:
    """Represents a trading signal from the strategy."""
    symbol: str
    timestamp: float
    signal_type: str  # "BUY" or "SELL" (close position)
    strength: float  # 0.0 to 1.0 confidence score
    entry_price: float
    sl_price: float
    tp_price: float
    volume: float
    reasoning: str
    indicators: Dict[str, float]


@dataclass
class TradeRecord:
    """Full audit record of a trade attempt and its outcome."""
    signal: TradingSignal
    risk_check: Dict[str, Any]
    risk_passed: bool
    risk_reason: str
    order_result: Optional[OrderResult]
    timestamp: float
    latency_ms: float
    status: str  # "executed", "blocked_by_risk", "failed", "skipped"


class SignalGenerator:
    """
    Generates trading signals based on EMA crossover + VWAP filter.
    """

    def __init__(self, fast_ema: int = 9, slow_ema: int = 21, vwap_filter: bool = True):
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.vwap_filter = vwap_filter
        self.indicators = IndicatorPipeline()

    def generate_signal(
        self,
        df: pd.DataFrame,
        symbol: str,
    ) -> Optional[TradingSignal]:
        """
        Generates a signal from the most recent bar data.

        Returns None if no clear signal.
        """
        import pandas as pd

        if len(df) < self.slow_ema + 5:
            return None

        # Compute indicators
        df = self.indicators.compute_all(df)

        # Most recent and previous values
        latest = df.iloc[-1]
        prev = df.iloc[-2]

        # EMA crossover detection
        fast_prev = prev["ema_9"]
        slow_prev = prev["ema_21"]
        fast_curr = latest["ema_9"]
        slow_curr = latest["ema_21"]

        bullish_cross = fast_prev < slow_prev and fast_curr > slow_curr
        bearish_cross = fast_prev > slow_prev and fast_curr < slow_curr

        if not (bullish_cross or bearish_cross):
            return None

        # VWAP filter
        if self.vwap_filter:
            close_price = latest["close"]
            vwap = latest["vwap"]
            if bullish_cross and close_price <= vwap:
                return None  # Don't buy if price below VWAP
            if bearish_cross and close_price >= vwap:
                return None  # Don't sell if price above VWAP

        # Determine direction
        signal_type = "BUY" if bullish_cross else "SELL"

        # Calculate SL/TP based on ATR
        atr = latest["atr_14"]
        entry_price = close_price

        if signal_type == "BUY":
            sl_price = entry_price - atr * 1.5
            tp_price = entry_price + atr * 3.0
        else:
            sl_price = entry_price + atr * 1.5
            tp_price = entry_price - atr * 3.0

        return TradingSignal(
            symbol=symbol,
            timestamp=time.time(),
            signal_type=signal_type,
            strength=1.0 if abs(fast_curr - slow_curr) > atr else 0.7,
            entry_price=entry_price,
            sl_price=sl_price,
            tp_price=tp_price,
            volume=0.01,  # fixed for demo
            reasoning=f"EMA({self.fast_ema}) crossover EMA({self.slow_ema}) on {symbol}",
            indicators={
                "ema_9": float(fast_curr),
                "ema_21": float(slow_curr),
                "vwap": float(latest["vwap"]),
                "atr_14": float(atr),
                "rsi_14": float(latest["rsi_14"]),
                "close": float(close_price),
            },
        )


class LiveTrader:
    """
    Main live trading loop. Orchestrates:
    1. Data fetch on interval
    2. Signal generation
    3. Risk check (independent)
    4. Order execution
    5. Audit logging
    """

    def __init__(
        self,
        symbol: str = "EURUSD",
        timeframe: str = "M1",
        interval: int = 10,  # seconds between signal checks
        max_risk_per_trade_pct: float = 0.5,
        enabled: bool = False,  # safety: default OFF
    ):
        self.symbol = symbol
        self.timeframe = self._resolve_timeframe(timeframe)
        self.interval = interval
        self.enabled = enabled

        # Components
        self.mt5_mgr = MT5Manager()
        self.fetcher = DataFetcher()
        self.signal_gen = SignalGenerator()
        self.risk_engine = RiskEngine(
            max_daily_loss_pct=3.0,
            max_drawdown_pct=10.0,
            emergency_drawdown_pct=15.0,
            min_margin_level_pct=200.0,
            max_portfolio_heat_pct=6.0,
            max_spread_percentile=90.0,
            max_positions=5,
        )
        self.executor = OrderExecutor(risk_engine=self.risk_engine)

        # State
        self.running = False
        self.trade_history: List[TradeRecord] = []
        self._last_signal_time: float = 0.0
        self._min_signal_interval: float = 60.0  # don't re-signal within 60s of last

        # Audit log
        self._audit_log_path = os.path.join(
            os.path.dirname(__file__), "logs", "live_trading.jsonl"
        )
        os.makedirs(os.path.dirname(self._audit_log_path), exist_ok=True)

    def _resolve_timeframe(self, tf: str) -> int:
        """Maps string timeframe to MT5 constant."""
        mapping = {
            "M1": TIMEFRAME_M1,
            "M5": TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30,
            "H1": TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }
        return mapping.get(tf.upper(), TIMEFRAME_M1)

    def _log(self, event: str, details: dict):
        """Append to live trading audit log."""
        entry = {
            "timestamp": time.time(),
            "datetime": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "details": details,
        }
        with open(self._audit_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info(f"{event}: {details}")

    def _check_existing_position(self) -> Optional[dict]:
        """Checks if we already have a position in the symbol."""
        positions = mt5.positions_get(symbol=self.symbol)
        if positions:
            return positions[0]._asdict()
        return None

    def _run_cycle(self) -> Optional[TradeRecord]:
        """
        Executes one full trading cycle:
        fetch data → generate signal → check risk → execute order → log.
        """
        start_time = time.time()

        # 1. Ensure MT5 connection
        if not self.mt5_mgr.ensure_connection():
            self._log("connection_lost", {"attempting_reconnect": True})
            return None

        # 2. Refresh risk snapshot
        snapshot = self.risk_engine.update()
        if snapshot.risk_state in (RiskState.ENTRIES_BLOCKED, RiskState.EMERGENCY_FLATTEN):
            self._log("risk_blocks_entry", {
                "state": snapshot.risk_state.name,
                "blocks": snapshot.active_blocks,
            })
            # Emergency flatten if needed
            if snapshot.risk_state == RiskState.EMERGENCY_FLATTEN:
                self.executor.emergency_flatten()
                self._log("emergency_flatten_executed", {"symbol": self.symbol})
            return None

        # 3. Check for existing position
        existing_pos = self._check_existing_position()
        if existing_pos:
            # Already in a position — we could add TP/SL management here
            self._log("position_exists", {
                "symbol": self.symbol,
                "volume": existing_pos["volume"],
                "open_price": existing_pos["price_open"],
            })
            return None

        # 4. Fetch recent bars for signal generation
        df = self.fetcher.fetch_bars(self.symbol, self.timeframe, count=100, force_refresh=True)
        if df is None:
            self._log("data_fetch_failed", {"symbol": self.symbol})
            return None

        # 5. Generate signal
        signal = self.signal_gen.generate_signal(df, self.symbol)
        if signal is None:
            # No signal — log at debug level
            logger.debug(f"No signal for {self.symbol} at {datetime.now().strftime('%H:%M:%S')}")
            return None

        # Rate limit: don't generate signals too frequently
        if time.time() - self._last_signal_time < self._min_signal_interval:
            logger.debug(f"Signal throttled (last: {time.time() - self._last_signal_time:.0f}s ago)")
            return None

        self._last_signal_time = time.time()
        self._log("signal_generated", {
            "symbol": signal.symbol,
            "type": signal.signal_type,
            "entry_price": signal.entry_price,
            "sl": signal.sl_price,
            "tp": signal.tp_price,
            "reasoning": signal.reasoning,
            "indicators": signal.indicators,
        })

        # 6. Execute order (risk check is internal to execute_order)
        order_result = self.executor.execute_order(
            symbol=signal.symbol,
            order_type=signal.signal_type,
            volume=signal.volume,
            price=None,  # market order
            sl=signal.sl_price,
            tp=signal.tp_price,
            max_retries=3,
        )

        latency_ms = (time.time() - start_time) * 1000

        # 7. Determine risk check result
        if not order_result.success and "Blocked by risk" in order_result.retcode_description:
            risk_passed = False
            risk_reason = order_result.retcode_description
        else:
            risk_passed = True
            risk_reason = "all_limits_passed"

        record = TradeRecord(
            signal=signal,
            risk_check=snapshot.to_dict(),
            risk_passed=risk_passed,
            risk_reason=risk_reason,
            order_result=order_result,
            timestamp=time.time(),
            latency_ms=latency_ms,
            status="executed" if order_result.success else (
                "blocked_by_risk" if not risk_passed else "failed"
            ),
        )

        # 8. Log result
        self._log("trade_cycle_complete", {
            "status": record.status,
            "latency_ms": record.latency_ms,
            "risk_passed": record.risk_passed,
            "order_success": order_result.success,
            "retcode": order_result.retcode,
            "slippage": order_result.slippage,
            "reasoning": signal.reasoning,
        })

        self.trade_history.append(record)
        return record

    def run(self, max_cycles: int = 0, dry_run: bool = True):
        """
        Main trading loop.

        Args:
            max_cycles: Maximum number of cycles (0 = infinite)
            dry_run: If True, generate signals but don't execute orders
        """
        if dry_run:
            self._log("dry_run_mode", {"enabled": True})
            logger.warning("DRY RUN MODE — no orders will be sent to MT5")

        self.running = True
        self._log("trader_started", {
            "symbol": self.symbol,
            "timeframe": str(self.timeframe),
            "interval": self.interval,
            "dry_run": dry_run,
            "max_cycles": max_cycles if max_cycles > 0 else "infinite",
        })

        cycle = 0
        try:
            while self.running:
                if max_cycles > 0 and cycle >= max_cycles:
                    break

                cycle += 1

                if self.enabled and not dry_run:
                    record = self._run_cycle()
                else:
                    # Even in dry run, fetch data and generate signals
                    self.mt5_mgr.ensure_connection()
                    self.risk_engine.update()
                    df = self.fetcher.fetch_bars(self.symbol, self.timeframe, count=100, force_refresh=True)
                    if df:
                        signal = self.signal_gen.generate_signal(df, self.symbol)
                        if signal:
                            self._log("dry_run_signal", {
                                "type": signal.signal_type,
                                "entry_price": signal.entry_price,
                                "sl": signal.sl_price,
                                "tp": signal.tp_price,
                                "reasoning": signal.reasoning,
                            })
                        else:
                            logger.debug(f"[{datetime.now().strftime('%H:%M:%S')}] No signal")

                time.sleep(self.interval)

        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        """Stops the trading loop and cleans up."""
        self.running = False
        self._log("trader_stopped", {
            "cycles_completed": len(self.trade_history),
            "trades_executed": sum(1 for t in self.trade_history if t.status == "executed"),
            "trades_blocked": sum(1 for t in self.trade_history if t.status == "blocked_by_risk"),
        })
        self.mt5_mgr.shutdown()


def main():
    parser = argparse.ArgumentParser(description="MT5 Live Trading Bot")
    parser.add_argument("--symbol", default="EURUSD", help="Trading symbol (default: EURUSD)")
    parser.add_argument("--timeframe", default="M1", help="Timeframe (default: M1)")
    parser.add_argument("--interval", type=int, default=10, help="Seconds between cycles (default: 10)")
    parser.add_argument("--cycles", type=int, default=0, help="Max cycles (0=infinite, default: 0)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Dry run mode (no orders)")
    parser.add_argument("--live", action="store_true", help="Enable live trading (overrides dry-run)")
    parser.add_argument("--log-level", default="INFO", help="Log level")

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(os.path.dirname(__file__), "logs", "live_trading.log")),
        ],
    )

    trader = LiveTrader(
        symbol=args.symbol,
        timeframe=args.timeframe,
        interval=args.interval,
        enabled=args.live,
    )

    # Set up graceful shutdown
    def signal_handler(sig, frame):
        print("\nShutdown signal received...")
        trader.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Run
    trader.run(max_cycles=args.cycles, dry_run=not args.live)


if __name__ == "__main__":
    main()
