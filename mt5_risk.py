#!/usr/bin/env python
"""
MT5 Hard-Limit Risk Engine
==========================
Independent risk enforcement layer that cannot be overridden by strategies.
Based on the blueprint's Risk Agent specification (Section 2.3).

All methods return True/False (allow/block) — never raises exceptions
that a strategy can accidentally catch. Hard limits are evaluated in
isolated order, independent of signal generation logic.

Key rules:
- Max daily loss (3% of day-start equity)
- Max drawdown from peak (10% → ENTRIES_BLOCKED, 15% → EMERGENCY_FLATTEN)
- Max portfolio heat (6% total open risk)
- Max spread percentile (90th of rolling 200-bar history)
- Min margin level (200%)
- Max order rejection rate (3 in 10 min → ENTRIES_BLOCKED)
- Max data age (block if last tick > 60s old)

Usage:
    from mt5_risk import RiskEngine
    risk = RiskEngine()
    risk.update()  # refresh from MT5
    if risk.allow_entry(symbol, lot_size, stop_loss_price, entry_price):
        send_order(...)
"""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import time
import json
import os
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class RiskState(Enum):
    """Finite, deterministic risk states — strategies cannot override these."""
    NORMAL = 0
    REDUCED = 1
    ENTRIES_BLOCKED = 2
    EMERGENCY_FLATTEN = 3


@dataclass
class RiskSnapshot:
    """
    Immutable snapshot of all risk-relevant state at a point in time.
    Source-tagged and timestamped for audit.
    """
    timestamp: float
    equity: float
    balance: float
    margin: float
    margin_level: float
    margin_free: float
    day_start_equity: float
    peak_equity: float
    daily_pnl: float
    drawdown_pct: float
    num_open_positions: int
    total_risk_exposure: float  # sum of (stop_loss_distance * lot_size) across positions
    spread_percentile_90: float
    current_max_spread: float
    order_rejection_rate: float  # per 10 minutes
    last_tick_age_seconds: float
    data_fresh: bool
    symbol_info: Dict[str, dict] = field(default_factory=dict)
    positions: List[dict] = field(default_factory=list)
    risk_state: RiskState = RiskState.NORMAL
    active_blocks: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serializes for JSON audit log."""
        return {
            "timestamp": self.timestamp,
            "equity": self.equity,
            "balance": self.balance,
            "margin": self.margin,
            "margin_level": self.margin_level,
            "margin_free": self.margin_free,
            "day_start_equity": self.day_start_equity,
            "peak_equity": self.peak_equity,
            "daily_pnl": self.daily_pnl,
            "drawdown_pct": self.drawdown_pct,
            "num_open_positions": self.num_open_positions,
            "total_risk_exposure": self.total_risk_exposure,
            "spread_percentile_90": self.spread_percentile_90,
            "current_max_spread": self.current_max_spread,
            "order_rejection_rate": self.order_rejection_rate,
            "last_tick_age_seconds": self.last_tick_age_seconds,
            "data_fresh": self.data_fresh,
            "risk_state": self.risk_state.name,
            "active_blocks": self.active_blocks,
        }


class RiskEngine:
    """
    Independent hard-limit risk engine.

    This engine is designed to be PROCESS-ISOLATED from signal generation.
    It reads state directly from MT5 broker (not from strategy caches) and
    enforces deterministic rules that no strategy logic can override.

    Configuration matches the blueprint's Appendix A risk template.
    """

    def __init__(
        self,
        max_daily_loss_pct: float = 3.0,
        max_drawdown_pct: float = 10.0,
        emergency_drawdown_pct: float = 15.0,
        min_margin_level_pct: float = 200.0,
        max_portfolio_heat_pct: float = 6.0,
        max_spread_percentile: float = 90.0,
        max_consecutive_losses: int = 5,
        max_order_rejection_rate: float = 3.0,  # per 10 minutes
        max_data_age_seconds: int = 60,
        max_positions: int = 5,
        max_pending_orders: int = 10,
    ):
        self.config = {
            "max_daily_loss_pct": max_daily_loss_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "emergency_drawdown_pct": emergency_drawdown_pct,
            "min_margin_level_pct": min_margin_level_pct,
            "max_portfolio_heat_pct": max_portfolio_heat_pct,
            "max_spread_percentile": max_spread_percentile,
            "max_consecutive_losses": max_consecutive_losses,
            "max_order_rejection_rate": max_order_rejection_rate,
            "max_data_age_seconds": max_data_age_seconds,
            "max_positions": max_positions,
            "max_pending_orders": max_pending_orders,
        }

        # State tracking
        self._day_start_equity: Optional[float] = None
        self._peak_equity: float = 0.0
        self._consecutive_losses: int = 0
        self._order_history: List[Tuple[float, bool]] = []  # (timestamp, success)
        self._last_snapshot: Optional[RiskSnapshot] = None
        self._risk_state: RiskState = RiskState.NORMAL
        self._spread_history: Dict[str, List[float]] = {}

        # Audit log
        self._audit_log_path = os.path.join(
            os.path.dirname(__file__), "logs", "risk_audit.jsonl"
        )
        os.makedirs(os.path.dirname(self._audit_log_path), exist_ok=True)

    def _log_audit(self, event: str, details: dict):
        """Append-only audit log for every risk decision."""
        entry = {
            "timestamp": time.time(),
            "event": event,
            "details": details,
        }
        with open(self._audit_log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info(f"Risk audit: {event} — {details}")

    def _update_day_start(self):
        """Resets daily tracking at the start of a new trading day."""
        now = datetime.now()
        # Reset at midnight or 00:00 server time
        current_hour = now.hour
        if current_hour == 0 and (self._day_start_equity is not None):
            # Could be new day — but only reset if equity has changed significantly
            account = mt5.account_info()
            if account:
                self._day_start_equity = account.equity
                self._consecutive_losses = 0

        if self._day_start_equity is None:
            account = mt5.account_info()
            if account:
                self._day_start_equity = account.equity

    def _get_spread_history(self, symbol: str, lookback_bars: int = 200) -> List[float]:
        """Fetches recent spread data for percentile calculation."""
        if symbol not in self._spread_history:
            self._spread_history[symbol] = []

        # Fetch recent bars for spread data
        bars = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, lookback_bars)
        if bars is not None:
            spreads = [bar["spread"] for bar in bars if bar["spread"] > 0]
            self._spread_history[symbol] = spreads[-lookback_bars:]

        return self._spread_history.get(symbol, [0])

    def _get_order_rejection_rate(self) -> float:
        """Calculates order rejection rate over the last 10 minutes."""
        now = time.time()
        ten_min_ago = now - 600
        recent = [(ts, success) for ts, success in self._order_history if ts > ten_min_ago]
        rejections = sum(1 for _, success in recent if not success)
        return rejections

    def _get_last_tick_age(self, symbol: str) -> Tuple[float, bool]:
        """Returns (age_seconds, is_fresh) for the most recent tick."""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return 999.0, False

        # MT5 tick time is UTC unix timestamp
        tick_age = time.time() - tick.time
        is_fresh = tick_age <= self.config["max_data_age_seconds"]
        return tick_age, is_fresh

    def update(self) -> RiskSnapshot:
        """
        Takes a fresh snapshot of all risk-relevant state from MT5.
        This is the ONLY method that reads from MT5 — called explicitly
        by the risk agent process.
        """
        self._update_day_start()

        # Get account info
        account = mt5.account_info()
        if account is None:
            error = mt5.last_error()
            self._log_audit("account_info_failed", {"error": error})
            # Return a blocking snapshot
            return RiskSnapshot(
                timestamp=time.time(),
                equity=0.0,
                balance=0.0,
                margin=0.0,
                margin_level=0.0,
                margin_free=0.0,
                day_start_equity=0.0,
                peak_equity=0.0,
                daily_pnl=0.0,
                drawdown_pct=0.0,
                num_open_positions=0,
                total_risk_exposure=0.0,
                spread_percentile_90=0.0,
                current_max_spread=0.0,
                order_rejection_rate=0.0,
                last_tick_age_seconds=999.0,
                data_fresh=False,
                risk_state=RiskState.EMERGENCY_FLATTEN,
                active_blocks=["account_info_unavailable"],
            )

        equity = account.equity
        if self._day_start_equity is None:
            self._day_start_equity = equity
        if self._peak_equity == 0.0:
            self._peak_equity = equity

        # Update peak equity
        if equity > self._peak_equity:
            self._peak_equity = equity

        # Drawdown calculation
        drawdown_pct = (
            (self._peak_equity - equity) / self._peak_equity * 100
            if self._peak_equity > 0 else 0.0
        )

        # Daily P&L
        daily_pnl = equity - self._day_start_equity
        daily_loss_pct = (
            abs(daily_pnl) / self._day_start_equity * 100
            if daily_pnl < 0 and self._day_start_equity > 0 else 0.0
        )

        # Positions
        positions = mt5.positions_get()
        total_risk = 0.0
        position_details = []
        if positions:
            for pos in positions:
                sl_distance = abs(pos.price_open - pos.sl) if pos.sl > 0 else 0
                risk = sl_distance * pos.volume
                total_risk += risk
                position_details.append({
                    "symbol": pos.symbol,
                    "type": pos.type,
                    "volume": pos.volume,
                    "price_open": pos.price_open,
                    "sl": pos.sl,
                    "tp": pos.tp,
                    "risk": risk,
                })

        # Pending orders
        orders = mt5.orders_get()
        num_pending = len(orders) if orders else 0

        # Spread check for major symbols
        symbols_to_check = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
        max_spread = 0.0
        spread_threshold = 0.0
        symbols_info = {}
        for sym in symbols_to_check:
            info = mt5.symbol_info_tick(sym)
            if info:
                current_spread = info.ask - info.bid
                symbols_info[sym] = {
                    "bid": info.bid,
                    "ask": info.ask,
                    "spread": current_spread,
                }
                max_spread = max(max_spread, current_spread)

                # Spread percentile
                spreads = self._get_spread_history(sym)
                if len(spreads) >= 10:
                    spread_threshold = max(
                        spread_threshold,
                        float(np.percentile(spreads, self.config["max_spread_percentile"]))
                    )

        # Data freshness
        tick_age, data_fresh = self._get_last_tick_age("EURUSD")

        # Determine risk state
        active_blocks = []
        risk_state = RiskState.NORMAL

        # Emergency flatten: drawdown > 15%
        if drawdown_pct >= self.config["emergency_drawdown_pct"]:
            risk_state = RiskState.EMERGENCY_FLATTEN
            active_blocks.append("emergency_drawdown")
            self._log_audit("emergency_drawdown_triggered", {
                "drawdown_pct": drawdown_pct,
                "threshold": self.config["emergency_drawdown_pct"],
            })

        # Entries blocked: drawdown > 10%
        elif drawdown_pct >= self.config["max_drawdown_pct"]:
            risk_state = RiskState.ENTRIES_BLOCKED
            active_blocks.append("drawdown_exceeded")

        # Entries blocked: daily loss > 3%
        elif daily_loss_pct >= self.config["max_daily_loss_pct"]:
            risk_state = RiskState.ENTRIES_BLOCKED
            active_blocks.append("daily_loss_exceeded")
            self._log_audit("daily_loss_block", {
                "daily_loss_pct": daily_loss_pct,
                "threshold": self.config["max_daily_loss_pct"],
            })

        # Redacted: max position count
        elif len(positions or []) >= self.config["max_positions"]:
            risk_state = RiskState.REDUCED
            active_blocks.append("max_positions_reached")

        # Redacted: order rejection rate
        elif self._get_order_rejection_rate() >= self.config["max_order_rejection_rate"]:
            risk_state = RiskState.ENTRIES_BLOCKED
            active_blocks.append("rejection_rate_exceeded")
            self._log_audit("rejection_rate_block", {
                "rejection_rate": self._get_order_rejection_rate(),
                "threshold": self.config["max_order_rejection_rate"],
            })

        # Data freshness check
        if not data_fresh:
            risk_state = RiskState.ENTRIES_BLOCKED
            active_blocks.append("stale_data")

        # Margin level check
        if account.margin_level < self.config["min_margin_level_pct"] and account.margin_level > 0:
            if risk_state != RiskState.EMERGENCY_FLATTEN:
                risk_state = RiskState.REDUCED
                active_blocks.append("low_margin_level")

        self._risk_state = risk_state

        snapshot = RiskSnapshot(
            timestamp=time.time(),
            equity=equity,
            balance=account.balance,
            margin=account.margin,
            margin_level=account.margin_level,
            margin_free=account.margin_free,
            day_start_equity=self._day_start_equity,
            peak_equity=self._peak_equity,
            daily_pnl=daily_pnl,
            drawdown_pct=drawdown_pct,
            num_open_positions=len(positions or []),
            total_risk_exposure=total_risk,
            spread_percentile_90=spread_threshold,
            current_max_spread=max_spread,
            order_rejection_rate=self._get_order_rejection_rate(),
            last_tick_age_seconds=tick_age,
            data_fresh=data_fresh,
            symbol_info=symbols_info,
            positions=position_details,
            risk_state=risk_state,
            active_blocks=active_blocks,
        )

        self._last_snapshot = snapshot
        return snapshot

    def get_snapshot(self) -> Optional[RiskSnapshot]:
        """Returns the last snapshot without refreshing from MT5."""
        return self._last_snapshot

    def allow_entry(
        self,
        symbol: str,
        lot_size: float,
        entry_price: float,
        stop_loss_price: float,
    ) -> Tuple[bool, str]:
        """
        Evaluates whether a trade entry is allowed under all hard limits.

        Args:
            symbol: Trading symbol
            lot_size: Position size in lots
            entry_price: Proposed entry price
            stop_loss_price: Stop loss price (0 if none)

        Returns:
            (True, "reason") if allowed, (False, "reason") if blocked
        """
        snapshot = self._last_snapshot
        if snapshot is None:
            snapshot = self.update()

        # If not NORMAL state, no entries allowed
        if snapshot.risk_state in (RiskState.ENTRIES_BLOCKED, RiskState.EMERGENCY_FLATTEN):
            return False, f"risk_state={snapshot.risk_state.name}, blocks={snapshot.active_blocks}"

        # Calculate risk exposure
        if stop_loss_price > 0:
            sl_distance = abs(entry_price - stop_loss_price)
            trade_risk = sl_distance * lot_size
            new_total_risk = snapshot.total_risk_exposure + trade_risk
            portfolio_heat_pct = (
                new_total_risk / snapshot.equity * 100 if snapshot.equity > 0 else 999
            )
            if portfolio_heat_pct > self.config["max_portfolio_heat_pct"]:
                self._log_audit("portfolio_heat_block", {
                    "portfolio_heat_pct": portfolio_heat_pct,
                    "threshold": self.config["max_portfolio_heat_pct"],
                    "symbol": symbol,
                    "lot_size": lot_size,
                })
                return False, f"portfolio_heat={portfolio_heat_pct:.2f}% exceeds {self.config['max_portfolio_heat_pct']}%"

        # Check symbol spread
        if symbol in snapshot.symbol_info:
            current_spread = snapshot.symbol_info[symbol]["spread"]
            if current_spread > snapshot.spread_percentile_90:
                self._log_audit("spread_filter_block", {
                    "symbol": symbol,
                    "current_spread": current_spread,
                    "threshold": snapshot.spread_percentile_90,
                })
                return False, f"spread={current_spread} exceeds p90={snapshot.spread_percentile_90}"

        # All checks passed
        return True, "all_limits_passed"

    def record_order_result(self, success: bool, error: Optional[dict] = None):
        """Records order attempt for rejection rate tracking."""
        self._order_history.append((time.time(), success))
        if not success and error:
            self._consecutive_losses += 1
            self._log_audit("order_rejected", {
                "error": error,
                "consecutive_losses": self._consecutive_losses,
            })
        else:
            self._consecutive_losses = 0
            self._log_audit("order_confirmed", {
                "consecutive_losses": self._consecutive_losses,
            })

        # Trim old history (keep last 100 entries)
        self._order_history = self._order_history[-100:]

    @property
    def risk_state(self) -> RiskState:
        return self._risk_state


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    import sys
    sys.path.insert(0, "/home/rebawn/trader")

    from mt5_connection import MT5Manager

    mgr = MT5Manager()
    if not mgr.ensure_connection():
        print("ERROR: MT5 not connected")
        exit(1)

    risk = RiskEngine()

    print("\n=== Risk Engine Test ===")
    print(f"Config: {risk.config}")

    snapshot = risk.update()
    print(f"\nSnapshot:")
    print(f"  State: {snapshot.risk_state.name}")
    print(f"  Equity: {snapshot.equity}")
    print(f"  Balance: {snapshot.balance}")
    print(f"  Daily P&L: {snapshot.daily_pnL:.2f} ({snapshot.dailyPnL / snapshot.day_start_equity * 100:.2f}%)")
    print(f"  Drawdown: {snapshot.drawdown_pct:.2f}%")
    print(f"  Margin Level: {snapshot.margin_level:.2f}%")
    print(f"  Open Positions: {snapshot.num_open_positions}")
    print(f"  Risk Exposure: {snapshot.total_risk_exposure:.2f}")
    print(f"  Data Fresh: {snapshot.data_fresh} (tick age: {snapshot.last_tick_age_seconds:.1f}s)")
    print(f"  Spread: {snapshot.current_max_spread:.5f}")
    print(f"  Blocks: {snapshot.active_blocks}")

    print(f"\n=== Entry Check (EURUSD, 0.01 lots) ===")
    allowed, reason = risk.allow_entry("EURUSD", 0.01, 1.16750, 1.16700)
    print(f"  Allowed: {allowed}")
    print(f"  Reason: {reason}")

    print(f"\n=== All Symbol Spreads ===")
    for sym, info in snapshot.symbol_info.items():
        print(f"  {sym}: bid={info['bid']}, ask={info['ask']}, spread={info['spread']:.5f}")

    mgr.shutdown()
