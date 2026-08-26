#!/usr/bin/env python
"""
MT5 Technical Indicators
========================
Fast indicator pipeline using pandas-ta, computed on bar DataFrames from DataFetcher.

Key features:
- EMA, RSI, MACD, ATR, Bollinger Bands, VWAP
- Vectorized computation for performance
- Configurable parameters per strategy
- Built-in signal detection (crossover, threshold breaches)

Usage:
    from mt5_indicators import IndicatorPipeline
    pipe = IndicatorPipeline()
    df = fetcher.fetch_bars("EURUSD", mt5.TIMEFRAME_M1, count=500)
    df = pipe.compute_all(df)
    signals = pipe.detect_ema_crossover(df, fast=9, slow=21)
"""
import pandas as pd
import pandas_ta as ta
import numpy as np
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class IndicatorPipeline:
    """
    Computes technical indicators on OHLCV DataFrames.

    Expects DataFrames with columns: time, open, high, low, close, tick_volume
    (as produced by DataFetcher.fetch_bars).
    """

    def compute_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Computes all standard indicators on the DataFrame.

        Adds columns: ema_9, ema_21, ema_50, ema_200, rsi_14,
        macd, macd_signal, macd_hist, bb_upper, bb_middle, bb_lower,
        atr_14, vwap, vwma_20
        """
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["tick_volume"]

        # EMAs
        df["ema_9"] = ta.ema(close, length=9)
        df["ema_21"] = ta.ema(close, length=21)
        df["ema_50"] = ta.ema(close, length=50)
        df["ema_200"] = ta.ema(close, length=200)

        # RSI
        df["rsi_14"] = ta.rsi(close, length=14)

        # MACD (default: 12, 26, 9)
        macd_result = ta.macd(close, fast=12, slow=26, signal=9)
        df["macd"] = macd_result["MACD_12_26_9"]
        df["macd_signal"] = macd_result["MACS_12_26_9"]
        df["macd_hist"] = macd_result["MACH_12_26_9"]

        # Bollinger Bands (default: 20, 2 std)
        bb_result = ta.bbands(close, length=20, std=2)
        df["bb_upper"] = bb_result["BBU_20_2.0"]
        df["bb_middle"] = bb_result["BBM_20_2.0"]
        df["bb_lower"] = bb_result["BBL_20_2.0"]
        df["bb_width"] = bb_result["BBB_20_2.0"]  # Bandwidth %

        # ATR
        df["atr_14"] = ta.atr(high, low, close, length=14)

        # VWAP (intraday — recalculate per row)
        df["vwap"] = ta.vwap(high, low, close, volume)

        # Volume-weighted moving average
        df["vwma_20"] = ta.vwma(close, volume, length=20)

        logger.info(f"Indicators computed on {len(df)} bars")
        return df

    def detect_ema_crossover(
        self,
        df: pd.DataFrame,
        fast_col: str = "ema_9",
        slow_col: str = "ema_21",
        lookback: int = 2,
    ) -> pd.DataFrame:
        """
        Detects EMA crossover signals (fast crossing slow).

        Returns DataFrame with signal column:
        -  1 = bullish crossover (fast crosses above slow)
        - -1 = bearish crossover (fast crosses below slow)
         0 = no signal
        """
        df = df.copy()
        fast = df[fast_col]
        slow = df[slow_col]

        # Crossover: current fast above slow, previous fast below slow
        df["signal"] = 0
        df.loc[(fast > slow) & (fast.shift(lookback) < slow.shift(lookback)), "signal"] = 1
        df.loc[(fast < slow) & (fast.shift(lookback) > slow.shift(lookback)), "signal"] = -1

        crossover_count = int(df["signal"].abs().sum())
        logger.info(f"EMA crossover detected {crossover_count} signals")
        return df

    def detect_vwap_pullback(
        self,
        df: pd.DataFrame,
        tolerance: float = 0.001,
    ) -> pd.DataFrame:
        """
        Detects when price pulls back to VWAP within a tolerance (for scalping).

        Returns DataFrame with signal column:
        -  1 = price crossed above VWAP (bullish pullback entry)
        - -1 = price crossed below VWAP (bearish pullback entry)
         0 = no signal
        """
        df = df.copy()
        close = df["close"]
        vwap = df["vwap"]

        df["signal"] = 0
        df.loc[close > vwap, "signal"] = 1
        df.loc[close < vwap, "signal"] = -1

        # Only signal on the cross
        df["signal"] = df["signal"] * (df["signal"].diff() != 0).astype(int)

        logger.info(f"VWAP pullback detected {int(df['signal'].abs().sum())} signals")
        return df

    def detect_mean_reversion(
        self,
        df: pd.DataFrame,
        bb_col: str = "bb_lower",
        bb_upper_col: str = "bb_upper",
        oversold_rsi: float = 30.0,
        overbought_rsi: float = 70.0,
    ) -> pd.DataFrame:
        """
        Mean reversion signals: price at Bollinger Band edge + RSI extreme.

        Returns DataFrame with signal column:
        -  1 = oversold (price at lower BB + RSI < 30)
        - -1 = overbought (price at upper BB + RSI > 70)
         0 = no signal
        """
        df = df.copy()

        df["signal"] = 0
        # Oversold: close at or below lower BB and RSI below threshold
        df.loc[
            (df["close"] <= df[bb_col]) & (df["rsi_14"] < oversold_rsi),
            "signal"
        ] = 1
        # Overbought: close at or above upper BB and RSI above threshold
        df.loc[
            (df["close"] >= df[bb_upper_col]) & (df["rsi_14"] > overbought_rsi),
            "signal"
        ] = -1

        logger.info(f"Mean reversion detected {int(df['signal'].abs().sum())} signals")
        return df

    def detect_breakout(
        self,
        df: pd.DataFrame,
        lookback: int = 20,
        volume_confirmation: bool = True,
    ) -> pd.DataFrame:
        """
        Detects breakouts above recent resistance or below recent support.

        Returns DataFrame with signal column:
        -  1 = breakout above resistance
        - -1 = breakdown below support
         0 = no signal
        """
        df = df.copy()
        high = df["high"]
        low = df["low"]
        close = df["close"]

        resistance = high.shift(1).rolling(lookback).max()
        support = low.shift(1).rolling(lookback).min()

        df["signal"] = 0
        df.loc[close > resistance, "signal"] = 1
        df.loc[close < support, "signal"] = -1

        if volume_confirmation:
            avg_vol = df["tick_volume"].shift(1).rolling(lookback).mean()
            df.loc[df["tick_volume"] < avg_vol * 0.8, "signal"] = 0

        logger.info(f"Breakout detected {int(df['signal'].abs().sum())} signals")
        return df

    def compute_spread_percentile(
        self,
        symbol_info: dict,
        spread_history: pd.Series,
        percentile: float = 90.0,
    ) -> bool:
        """
        Checks if current spread exceeds the given percentile of historical spreads.

        Returns True if spread is acceptable (below threshold), False if too wide.

        Args:
            symbol_info: dict from DataFetcher.get_symbol_info()
            spread_history: Series of historical spread values (points)
            percentile: Max allowed spread percentile (default 90th)
        """
        if symbol_info is None or "spread_points" not in symbol_info:
            return False

        current_spread = symbol_info["spread_points"]
        if len(spread_history) < 10:
            # Not enough data — allow if spread < 3x median
            median_spread = np.median(spread_history) if len(spread_history) > 0 else 0
            return current_spread <= median_spread * 3 if median_spread > 0 else True

        threshold = np.percentile(spread_history, percentile)
        is_acceptable = current_spread <= threshold
        if not is_acceptable:
            logger.warning(
                f"Spread filter: current={current_spread}pts, "
                f"threshold(p{percentile})={threshold:.0f}pts"
            )
        return is_acceptable


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    import sys
    sys.path.insert(0, "/home/rebawn/trader")

    from mt5_data import DataFetcher
    from mt5_connection import MT5Manager

    mgr = MT5Manager()
    if not mgr.ensure_connection():
        print("ERROR: MT5 not connected")
        exit(1)

    fetcher = DataFetcher()
    pipe = IndicatorPipeline()

    print("\n=== Fetching EURUSD M1 data ===")
    df = fetcher.fetch_bars("EURUSD", mt5.TIMEFRAME_M1, count=200)
    if df is None:
        print("No data fetched")
        mgr.shutdown()
        exit(1)

    print(f"\nBars fetched: {len(df)}")

    print("\n=== Computing indicators ===")
    df = pipe.compute_all(df)

    print(f"\nColumns: {list(df.columns)}")
    print(f"\nLatest values:\n{df[['time', 'close', 'ema_9', 'ema_21', 'rsi_14', 'macd', 'bb_upper', 'vwap']].tail(5).to_string()}")

    print("\n=== EMA Crossover Signals ===")
    df_sig = pipe.detect_ema_crossover(df, fast_col="ema_9", slow_col="ema_21")
    cross_signals = df_sig[df_sig["signal"] != 0]
    if len(cross_signals) > 0:
        print(cross_signals[["time", "close", "ema_9", "ema_21", "signal"]].to_string())
    else:
        print("No crossover signals in this window")

    print("\n=== Mean Reversion Signals ===")
    df_mr = pipe.detect_mean_reversion(df)
    mr_signals = df_mr[df_mr["signal"] != 0]
    if len(mr_signals) > 0:
        print(mr_signals[["time", "close", "rsi_14", "bb_lower", "bb_upper", "signal"]].to_string())
    else:
        print("No mean reversion signals in this window")

    mgr.shutdown()
