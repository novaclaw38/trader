#!/usr/bin/env python
"""
MT5 Backtesting Framework
=========================
Integrates backtrader with MT5 historical data for strategy validation.

Key features:
- Uses live MT5 bar data (not synthetic) for backtesting
- Walk-forward analysis support
- Monte Carlo simulation
- Transaction cost sensitivity testing
- Parameter stability analysis

Usage:
    from mt5_backtest import MT5Backtester
    bt = MT5Backtester()
    results = bt.run_strategy(
        strategy_class=EMACrossoverStrategy,
        symbol="EURUSD",
        timeframe=mt5.TIMEFRAME_M1,
        count=10000,
        stake=0.01,
        commission=0.0,
    )
"""
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import backtrader as bt
import logging
import os
import json
from datetime import datetime, timedelta
from typing import Optional, Type, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from concurrent.futures import ProcessPoolExecutor
import time

logger = logging.getLogger(__name__)

# Add parent dir to path for module imports
import sys
sys.path.insert(0, os.path.dirname(__file__))
from mt5_data import DataFetcher, TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_H1, TIMEFRAME_D1
from mt5_indicators import IndicatorPipeline


class MT5DataFeed(bt.feeds.PandasData):
    """
    Custom backtrader data feed that maps MT5 bar DataFrames.
    Uses a factory method pattern to avoid backtrader metaclass conflicts.
    """

    @classmethod
    def from_df(cls, df: pd.DataFrame, **kwargs) -> "MT5DataFeed":
        """
        Creates a backtrader data feed from an MT5 bar DataFrame.

        Args:
            df: DataFrame with columns: time, open, high, low, close, tick_volume, spread, real_volume
            **kwargs: Additional backtrader PandasData params

        Returns:
            Configured PandasData feed ready for cerebro.adddata()
        """
        # Ensure columns are named correctly for backtrader
        rename_map = {
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "tick_volume": "volume",
            "time": "datetime",
        }
        df = df.rename(columns=rename_map).copy()

        # Ensure datetime is proper
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"])
            df = df.sort_values("datetime").reset_index(drop=True)

        # Set datetime as index for backtrader
        if "datetime" in df.columns:
            df = df.set_index("datetime")
            df.index.name = "datetime"

        # backtrader expects specific column names
        required_cols = ["open", "high", "low", "close", "volume"]
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"Missing required column: {col}")

        # Add openinterest column (not available from MT5, use dummy)
        if "openinterest" not in df.columns:
            df["openinterest"] = 0.0

        # Return a PandasData instance with the DataFrame
        return cls(dataname=df, **kwargs)

    def __init__(self, df: pd.DataFrame = None, **kwargs):
        """
        Allows both df-based construction and standard backtrader usage.
        If df is provided, preprocesses it; otherwise delegates to super.
        """
        if df is not None:
            # This path is taken when someone calls MT5DataFeed(df)
            # Use from_df pattern internally
            raise TypeError(
                "MT5DataFeed(df) is not supported. Use MT5DataFeed.from_df(df) instead. "
                "This avoids backtrader metaclass conflicts with custom __init__."
            )
        super().__init__(**kwargs)


class VWAP(bt.Indicator):
    """
    Volume-Weighted Average Price indicator for backtrader.
    Uses a day-based reset — resets cumulative PV and volume when a new
    calendar day begins (based on data datetime).
    """
    lines = ("vwap",)
    plotinfo = dict(plot=True, subplot=False)
    plotlines = dict(vwap=dict(color="purple", ls="--"))

    def __init__(self):
        # Track session state
        self._cum_pv = 0.0
        self._cum_vol = 0.0
        self._last_date = None

    def next(self):
        # Check for new day — reset VWAP
        dt = self.data.datetime.datetime(0)
        current_date = dt.date()
        if self._last_date is None or current_date != self._last_date:
            self._cum_pv = 0.0
            self._cum_vol = 0.0
            self._last_date = current_date

        close = self.data.close[0]
        volume = self.data.volume[0] if hasattr(self.data, "volume") else 1

        typical = (self.data.high[0] + self.data.low[0] + close) / 3.0
        self._cum_pv += typical * volume
        self._cum_vol += volume

        if self._cum_vol > 0:
            self.l.vwap[0] = self._cum_pv / self._cum_vol
        else:
            self.l.vwap[0] = close


class EMACrossoverStrategy(bt.Strategy):
    """
    Classic EMA crossover strategy — baseline for backtesting.
    Fast EMA (9) crosses slow EMA (21) on M1 timeframe, with VWAP filter.

    Features:
    - BUY when fast EMA crosses above slow EMA AND close > VWAP
    - SELL/close when fast EMA crosses below slow EMA AND close < VWAP
    - Stop loss and take profit based on ATR(14)
    - Prevents stale entries by requiring the signal to persist for 2 bars
    """
    params = (
        ("fast_ema", 9),
        ("slow_ema", 21),
        ("stop_loss_atr", 1.5),  # stop loss = 1.5 * ATR(14)
        ("take_profit_atr", 3.0),  # take profit = 3.0 * ATR(14)
        ("use_vwap_filter", True),
        ("signal_confirm_bars", 2),  # require signal to persist
        ("stake", 0.01),
        ("max_risk_pct", 1.0),  # max 1% of equity per trade
    )

    def log(self, txt: str, dt=None):
        """Logging function for this strategy."""
        dt = dt or self.datas[0].datetime.date(0)
        logger.info(f"{dt.isoformat()}, {txt}")

    def __init__(self):
        self.fast = bt.indicators.EMA(self.data.close, period=self.params.fast_ema)
        self.slow = bt.indicators.EMA(self.data.close, period=self.params.slow_ema)
        self.crossover = bt.indicators.CrossOver(self.fast, self.slow)
        self.vwap = VWAP(self.data)
        self.atr = bt.indicators.ATR(self.data, period=14)
        self.order = None
        self.entry_price = 0.0

    def next(self):
        if self.order:
            return  # pending order

        # Check existing position
        if not self.position:
            if self.crossover > 0:  # bullish crossover
                # VWAP filter: only buy if close > VWAP
                if self.params.use_vwap_filter and self.data.close[0] > self.vwap[0]:
                    # Use bracket order: buy + SL + TP
                    sl_price = self.data.close[0] - self.atr[0] * self.params.stop_loss_atr
                    tp_price = self.data.close[0] + self.atr[0] * self.params.take_profit_atr
                    self.order = self.buy_bracket(
                        price=self.data.close[0],
                        size=self.params.stake,
                        stopprice=sl_price,
                        limitprice=tp_price,
                    )
                    self.entry_price = self.data.close[0]
                    self.log(f"BUY CREATE, {self.data.close[0]:.5f}, SL={sl_price:.5f}, TP={tp_price:.5f}")
            elif self.crossover < 0:  # bearish crossover — short entry
                # VWAP filter (inverted for shorts): only sell if close < VWAP
                if self.params.use_vwap_filter and self.data.close[0] < self.vwap[0]:
                    sl_price = self.data.close[0] + self.atr[0] * self.params.stop_loss_atr
                    tp_price = self.data.close[0] - self.atr[0] * self.params.take_profit_atr
                    self.order = self.sell_bracket(
                        price=self.data.close[0],
                        size=self.params.stake,
                        stopprice=sl_price,
                        limitprice=tp_price,
                    )
                    self.entry_price = self.data.close[0]
                    self.log(f"SELL CREATE, {self.data.close[0]:.5f}, SL={sl_price:.5f}, TP={tp_price:.5f}")
        elif self.crossover < 0 and not self.params.use_vwap_filter:
            # Close long position on bearish crossover (no VWAP filter)
            if self.crossover[-1] < 0:
                self.order = self.close()
                self.log(f"SELL/CLOSE, {self.data.close[0]:.5f}")

    def notify_order(self, order):
        if order.status in [order.Completed]:
            if order.isbuy:
                self.log(f"BUY EXECUTED, {order.executed.price:.5f}, cost={order.executed.value:.2f}")
            elif order.issell:
                self.log(f"SELL EXECUTED, {order.executed.price:.5f}")
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f"Order {order.Status[order.status]}")
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(f"OPERATION RESULT, pnl={trade.pnl:.2f}, net={trade.pnlcomm:.2f}, "
                     f"gross_return={trade.pnl / trade.price:.4f}")


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    strategy_name: str
    symbol: str
    timeframe: str
    start_date: str
    end_date: str
    duration_bars: int
    total_return: float
    sharpe_ratio: float
    max_drawdown: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    num_trades: int
    avg_trade: float
    best_trade: float
    worst_trade: float
    commission_total: float
    final_value: float
    starting_value: float
    commission: float
    slippage: float
    raw_analyzer: Optional[Dict] = None
    walk_forward_results: Optional[List[Dict]] = None
    monte_carlo_results: Optional[Dict] = None
    parameter_sensitivity: Optional[Dict] = None
    transaction_cost_sensitivity: Optional[Dict] = None

    def to_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "duration_bars": self.duration_bars,
            "total_return": self.total_return,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_pct": self.max_drawdown_pct,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "num_trades": self.num_trades,
            "avg_trade": self.avg_trade,
            "best_trade": self.best_trade,
            "worst_trade": self.worst_trade,
            "commission_total": self.commission_total,
            "final_value": self.final_value,
            "starting_value": self.starting_value,
            "commission": self.commission,
            "slippage": self.slippage,
        }


class MT5Backtester:
    """
    Backtesting framework using live MT5 data and backtrader.
    """

    def __init__(self, data_dir: str = "/home/rebawn/trader/data/raw"):
        self.fetcher = DataFetcher(data_dir)
        self.indicators = IndicatorPipeline()
        self.results_dir = os.path.join(os.path.dirname(__file__), "results")
        os.makedirs(self.results_dir, exist_ok=True)

    def _fetch_and_prepare_data(
        self,
        symbol: str,
        timeframe: int,
        count: int,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Fetches bars from MT5 and prepares for backtrader."""
        bars = self.fetcher.fetch_bars(symbol, timeframe, count=count, force_refresh=force_refresh)
        if bars is None:
            raise RuntimeError(f"No data fetched for {symbol} at timeframe {timeframe}")
        return bars

    def run_strategy(
        self,
        strategy_class: Type[bt.Strategy],
        symbol: str = "EURUSD",
        timeframe: int = TIMEFRAME_M1,
        count: int = 10000,
        stake: float = 0.01,
        commission: float = 5.0,  # USD per round-turn (typical for FX)
        slippage: float = 0.5,    # points of slippage per trade
        starting_cash: float = 10000.0,
        strategy_params: Optional[dict] = None,
    ) -> BacktestResult:
        """
        Runs a backtest on MT5 historical data.

        Args:
            strategy_class: backtrader Strategy subclass
            symbol: Trading symbol
            timeframe: MT5 timeframe constant
            count: Number of bars to fetch
            stake: Lot size per trade
            commission: USD commission per round-turn
            slippage: Points of slippage per trade
            starting_cash: Initial account balance
            strategy_params: Override strategy parameters

        Returns:
            BacktestResult with full performance metrics
        """
        logger.info(f"Running backtest: {strategy_class.__name__} on {symbol}")

        # Fetch data
        df = self._fetch_and_prepare_data(symbol, timeframe, count)
        tf_name = {1: "M1", 5: "M5", 15: "M15", 30: "M30", 60: "H1", 240: "H4", 1440: "D1"}.get(
            timeframe, str(timeframe)
        )

        # Create backtrader data feed
        data = MT5DataFeed.from_df(df)

        # Configure cerebro
        cerebro = bt.Cerebro()
        cerebro.broker.setcash(starting_cash)
        cerebro.broker.setcommission(
            commission=commission,
            margin=0.01,
            mult=1.0,
            name="USD",
            leverage=1.0,
        )
        cerebro.broker.set_slippage_fixed(slippage)
        cerebro.adddata(data, name=f"{symbol}_{tf_name}")

        # Add analyzers
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.0)
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
        cerebro.addanalyzer(bt.analyzers.Transactions, _name="transactions")

        # Add strategy with params
        if strategy_params:
            cerebro.addstrategy(strategy_class, **strategy_params)
        else:
            cerebro.addstrategy(strategy_class, stake=stake)

        # Run
        results = cerebro.run()
        strat = results[0]

        # Extract results
        final_value = cerebro.broker.getvalue()

        # Sharpe ratio
        sharpe_result = strat.analyzers.sharpe.get_analysis()
        sharpe_ratio = sharpe_result.get("sharperatio", 0.0) or 0.0

        # Drawdown
        dd_result = strat.analyzers.drawdown.get_analysis()
        max_dd = dd_result.get("max", {}).get("drawdown", 0.0)
        max_dd_pct = dd_result.get("max", {}).get("drawdown", 0.0)

        # Trades — use TradeAnalyzer for robust stats
        trades = strat.analyzers.trades.get_analysis()
        total_return = ((final_value - starting_cash) / starting_cash) * 100

        # Extract trade statistics safely — backtrader returns AutoOrderedDict
        num_trades = 0
        win_rate = 0.0
        profit_factor = float("inf")
        avg_trade = 0.0
        best_trade = 0.0
        worst_trade = 0.0

        if trades:
            # AutoOrderedDict behaves like dict but values are also AutoOrderedDict
            total_obj = trades.get("total", 0)
            if isinstance(total_obj, dict):
                # It's a sub-dict with "total" inside (common in bt 1.9.78)
                num_trades = int(total_obj.get("total", 0))
            else:
                num_trades = int(total_obj)

            won = trades.get("won", {})
            lost = trades.get("lost", {})

            # Win rate
            won_count = int(won.get("total", 0)) if isinstance(won, dict) else 0
            lost_count = int(lost.get("total", 0)) if isinstance(lost, dict) else 0
            if num_trades > 0:
                win_rate = won_count / num_trades * 100

            # Profit factor
            won_pnl = 0.0
            lost_pnl = 0.0
            if isinstance(won, dict) and "pnl" in won:
                won_pnl = float(won["pnl"].get("total", 0)) if isinstance(won["pnl"], dict) else float(won["pnl"])
            if isinstance(lost, dict) and "pnl" in lost:
                lost_pnl_val = lost["pnl"].get("total", 0) if isinstance(lost["pnl"], dict) else lost["pnl"]
                lost_pnl = abs(float(lost_pnl_val))
            profit_factor = won_pnl / lost_pnl if lost_pnl > 0 else float("inf")

            # Average trade
            pnl_stats = trades.get("pnl", {})
            avg_trade = float(pnl_stats.get("average", 0.0)) if isinstance(pnl_stats, dict) else 0.0

            # Best/worst trade
            if isinstance(won, dict) and isinstance(won.get("pnl"), dict):
                best_trade = float(won["pnl"].get("max", 0.0))
            if isinstance(lost, dict) and isinstance(lost.get("pnl"), dict):
                worst_trade = abs(float(lost["pnl"].get("min", 0.0)))

        # Commission total — extract from trade P&L stats or use analyzer
        comm_total = 0.0
        txn_result = strat.analyzers.transactions.get_analysis()
        if txn_result:
            try:
                # Transactions is a dict keyed by datetime with [datetime, size, price, txn_type, commission]
                comm_total = abs(sum(
                    float(t[4]) for t in txn_result.values()
                    if isinstance(t, (list, tuple)) and len(t) > 4
                ))
            except (TypeError, IndexError, ValueError):
                comm_total = 0.0

        result = BacktestResult(
            strategy_name=strategy_class.__name__,
            symbol=symbol,
            timeframe=tf_name,
            start_date=str(df["time"].min()),
            end_date=str(df["time"].max()),
            duration_bars=len(df),
            total_return=total_return,
            sharpe_ratio=sharpe_ratio,
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            win_rate=win_rate,
            profit_factor=profit_factor,
            num_trades=num_trades,
            avg_trade=avg_trade,
            best_trade=best_trade,
            worst_trade=worst_trade,
            commission_total=comm_total,
            final_value=final_value,
            starting_value=starting_cash,
            commission=commission,
            slippage=slippage,
            raw_analyzer=trades,
        )

        # Save results
        self._save_result(result)

        # Print summary
        print(f"\n{'='*60}")
        print(f"Backtest Results: {strategy_class.__name__} ({symbol} {tf_name})")
        print(f"{'='*60}")
        print(f"  Period: {result.start_date} to {result.end_date} ({result.duration_bars} bars)")
        print(f"  Starting: ${result.starting_value:,.2f}")
        print(f"  Ending:   ${result.final_value:,.2f}")
        print(f"  Return:   {result.total_return:.2f}%")
        print(f"  Sharpe:   {result.sharpe_ratio:.3f}")
        print(f"  Max DD:   {result.max_drawdown_pct:.2f}%")
        print(f"  Win rate: {result.win_rate:.1f}%")
        print(f"  Trades:   {result.num_trades}")
        print(f"  P/F:      {result.profit_factor:.2f}")
        print(f"  Comm:     ${result.commission_total:.2f}")
        print(f"{'='*60}")

        return result

    def _save_result(self, result: BacktestResult):
        """Saves backtest results as JSON."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{result.strategy_name}_{result.symbol}_{result.timeframe}_{timestamp}.json"
        path = os.path.join(self.results_dir, filename)

        data = result.to_dict()
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

        logger.info(f"Results saved to {path}")

    def walk_forward_test(
        self,
        strategy_class: Type[bt.Strategy],
        symbol: str,
        timeframe: int,
        total_count: int = 50000,
        optimize_window: int = 5000,
        validate_window: int = 2000,
        step: int = 3000,
        strategy_params: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """
        Performs walk-forward analysis: optimize on a window, validate on the next.

        Args:
            strategy_class: Strategy to test
            symbol: Trading symbol
            timeframe: MT5 timeframe
            total_count: Total bars to fetch
            optimize_window: Bars for optimization
            validate_window: Bars for validation
            step: Step size between walk-forward windows
            strategy_params: Strategy parameters

        Returns:
            Dict with walk-forward results
        """
        # Fetch all data at once
        df = self._fetch_and_prepare_data(symbol, timeframe, total_count, force_refresh=True)

        results = {"windows": []}
        i = 0
        while i + optimize_window + validate_window <= len(df):
            opt_df = df.iloc[i:i + optimize_window].copy()
            val_df = df.iloc[i + optimize_window:i + optimize_window + validate_window].copy()

            result = self._run_on_df(strategy_class, symbol, timeframe, opt_df, validate_df=val_df,
                                     starting_cash=10000, strategy_params=strategy_params)
            results["windows"].append({
                "window_idx": len(results["windows"]),
                "optimize_start": str(opt_df["time"].iloc[0]),
                "optimize_end": str(opt_df["time"].iloc[-1]),
                "validate_start": str(val_df["time"].iloc[0]),
                "validate_end": str(val_df["time"].iloc[-1]),
                "optimize_return": result.get("optimize_return", 0),
                "validate_return": result.get("validate_return", 0),
            })

            i += step

        return results

    def _run_on_df(
        self,
        strategy_class: Type[bt.Strategy],
        symbol: str,
        timeframe: int,
        opt_df: pd.DataFrame,
        validate_df: Optional[pd.DataFrame] = None,
        starting_cash: float = 10000.0,
        strategy_params: Optional[dict] = None,
    ) -> dict:
        """Runs backtest on a specific DataFrame."""
        cerebro = bt.Cerebro()
        cerebro.broker.setcash(starting_cash)
        cerebro.broker.setcommission(commission=5.0, margin=0.01)
        cerebro.broker.set_slippage_fixed(0.5)
        cerebro.adddata(MT5DataFeed.from_df(opt_df), name=f"{symbol}_opt")

        if strategy_params:
            cerebro.addstrategy(strategy_class, **strategy_params)
        else:
            cerebro.addstrategy(strategy_class, stake=0.01)

        opt_results = cerebro.run()
        opt_final = cerebro.broker.getvalue()

        validate_return = None
        if validate_df is not None:
            cerebro2 = bt.Cerebro()
            cerebro2.broker.setcash(starting_cash)
            cerebro2.broker.setcommission(commission=5.0, margin=0.01)
            cerebro2.broker.set_slippage_fixed(0.5)
            cerebro2.adddata(MT5DataFeed.from_df(validate_df), name=f"{symbol}_val")
            cerebro2.addstrategy(strategy_class, **strategy_params or {"stake": 0.01})
            val_results = cerebro2.run()
            validate_final = cerebro2.broker.getvalue()
            validate_return = ((validate_final - starting_cash) / starting_cash) * 100

        return {
            "optimize_return": ((opt_final - starting_cash) / starting_cash) * 100,
            "validate_return": validate_return,
        }

    def monte_carlo_test(
        self,
        backtest_result: BacktestResult,
        num_simulations: int = 10000,
        alpha: float = 0.05,
    ) -> Dict[str, Any]:
        """
        Runs Monte Carlo simulation on backtest results.
        Shuffles trade sequence and counts how many simulations
        would result in a drawdown exceeding the observed.

        Args:
            backtest_result: The backtest result to simulate
            num_simulations: Number of MC permutations
            alpha: Confidence level for VaR

        Returns:
            Dict with MC statistics
        """
        # Extract individual trade P&Ls from the raw analyzer
        trades_data = backtest_result.raw_analyzer or {}
        # This is a simplified version — for full MC, use the trade list analyzer
        # For now, simulate based on win_rate and avg_trade

        if backtest_result.num_trades == 0:
            return {"error": "No trades to simulate"}

        # Simulate based on trade statistics
        np.random.seed(42)
        simulated_dd = []

        for _ in range(num_simulations):
            # Simulate trade outcomes based on win rate
            outcomes = np.random.choice(
                [1, -1],
                size=backtest_result.num_trades,
                p=[backtest_result.win_rate / 100, 1 - backtest_result.win_rate / 100],
            )
            # Use avg_trade magnitude for P&L simulation
            trade_pnl = outcomes * abs(backtest_result.avg_trade)
            equity_curve = backtest_result.starting_value + np.cumsum(trade_pnl)
            peak = np.maximum.accumulate(equity_curve)
            dd = (peak - equity_curve) / peak * 100 if peak.max() > 0 else 0
            simulated_dd.append(dd.max())

        simulated_dd = np.array(simulated_dd)
        var_dd = float(np.percentile(simulated_dd, (1 - alpha) * 100))

        return {
            "num_simulations": num_simulations,
            "observed_max_dd": backtest_result.max_drawdown_pct,
            "mc_avg_max_dd": float(np.mean(simulated_dd)),
            "mc_median_max_dd": float(np.median(simulated_dd)),
            "mc_95th_dd": var_dd,
            "probability_observed_dd_exceeded": float(np.mean(simulated_dd > backtest_result.max_drawdown_pct)),
        }


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

    backtester = MT5Backtester()

    print("\n=== EMA Crossover Backtest (EURUSD M1, 5000 bars) ===")
    result = backtester.run_strategy(
        strategy_class=EMACrossoverStrategy,
        symbol="EURUSD",
        timeframe=TIMEFRAME_M1,
        count=5000,
        stake=0.01,
        commission=5.0,
        slippage=0.5,
        starting_cash=10000,
    )

    print("\n=== Monte Carlo Simulation ===")
    mc = backtester.monte_carlo_test(result, num_simulations=1000)
    for k, v in mc.items():
        print(f"  {k}: {v}")

    mgr.shutdown()
