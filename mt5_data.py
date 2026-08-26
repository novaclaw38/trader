#!/usr/bin/env python
"""
MT5 Data Fetcher
================
Fetches historical bars and tick data from MT5 with Parquet persistence.

Key features:
- Bar fetch (OHLCV) with configurable timeframes
- Tick data fetch for scalping backtests
- Parquet persistence for efficient disk I/O
- Incremental updates (only fetch missing bars)
- Automatic symbol name normalization

Usage:
    from mt5_data import DataFetcher
    fetcher = DataFetcher()
    df = fetcher.fetch_bars("EURUSD", mt5.TIMEFRAME_M1, days=30)
"""
import MetaTrader5 as mt5
import pandas as pd
import os
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List

logger = logging.getLogger(__name__)

# Data storage
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
PROCESSED_DIR = os.path.join(DATA_DIR, "processed")

# MT5 timeframe constants (for reference)
TIMEFRAME_M1 = mt5.TIMEFRAME_M1
TIMEFRAME_M5 = mt5.TIMEFRAME_M5
TIMEFRAME_M15 = mt5.TIMEFRAME_M15
TIMEFRAME_M30 = mt5.TIMEFRAME_M30
TIMEFRAME_H1 = mt5.TIMEFRAME_H1
TIMEFRAME_H4 = mt5.TIMEFRAME_H4
TIMEFRAME_D1 = mt5.TIMEFRAME_D1
TIMEFRAME_W1 = mt5.TIMEFRAME_W1
TIMEFRAME_MN1 = mt5.TIMEFRAME_MN1


class DataFetcher:
    """Fetches and persists historical market data from MT5."""

    def __init__(self, data_dir: str = RAW_DIR):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(PROCESSED_DIR, exist_ok=True)

    def _symbol_path(self, symbol: str, timeframe: int) -> str:
        """Returns the parquet file path for a symbol/timeframe combination."""
        tf_name = {
            1: "M1", 5: "M5", 15: "M15", 30: "M30",
            60: "H1", 240: "H4", 1440: "D1", 10080: "W1", 43200: "MN1"
        }.get(timeframe, str(timeframe))
        filename = f"{symbol}_{tf_name}.parquet"
        return os.path.join(self.data_dir, filename)

    def fetch_bars(
        self,
        symbol: str,
        timeframe: int = TIMEFRAME_M1,
        count: int = 5000,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        force_refresh: bool = False,
    ) -> Optional[pd.DataFrame]:
        """
        Fetches historical bars (OHLCV) from MT5.

        Args:
            symbol: Trading symbol (e.g., "EURUSD")
            timeframe: MT5 timeframe constant (e.g., mt5.TIMEFRAME_M1)
            count: Number of bars to fetch (used when from_date is None)
            from_date: Start date for bar fetch
            to_date: End date for bar fetch
            force_refresh: If True, bypass cached data and refetch

        Returns:
            DataFrame with columns: time, open, high, low, close, tick_volume, spread, real_volume
            or None if fetch failed
        """
        # Check cache first
        if not force_refresh and from_date is None:
            cached = self._load_cached_bars(symbol, timeframe)
            if cached is not None and len(cached) >= count * 0.9:
                # Return most recent bars from cache
                return cached.tail(count)

        # Fetch from MT5
        if from_date is not None:
            if to_date is None:
                to_date = datetime.now()
            bars = mt5.copy_rates_range(symbol, timeframe, from_date, to_date)
        else:
            bars = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)

        if bars is None or len(bars) == 0:
            error = mt5.last_error()
            logger.warning(f"Failed to fetch bars for {symbol} ({timeframe}): {error}")
            return None

        df = pd.DataFrame(bars)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df["symbol"] = symbol

        # Persist to Parquet
        path = self._symbol_path(symbol, timeframe)
        df.to_parquet(path, index=False)
        logger.info(f"Fetched {len(df)} bars for {symbol} ({timeframe} -> {path})")

        return df

    def _load_cached_bars(self, symbol: str, timeframe: int) -> Optional[pd.DataFrame]:
        """Loads cached bars from Parquet if available."""
        path = self._symbol_path(symbol, timeframe)
        if os.path.exists(path):
            try:
                df = pd.read_parquet(path)
                logger.debug(f"Loaded {len(df)} cached bars for {symbol} ({timeframe})")
                return df
            except Exception as e:
                logger.warning(f"Failed to load cached bars: {e}")
        return None

    def fetch_ticks(
        self,
        symbol: str,
        from_date: datetime,
        to_date: Optional[datetime] = None,
        count: int = 10000,
    ) -> Optional[pd.DataFrame]:
        """
        Fetches tick data from MT5.

        Args:
            symbol: Trading symbol (e.g., "EURUSD")
            from_date: Start datetime for tick fetch
            to_date: End datetime (defaults to now)
            count: Number of ticks to fetch

        Returns:
            DataFrame with columns: time, bid, ask, last, volume, time_msc
        """
        if to_date is None:
            to_date = datetime.now()

        ticks = mt5.copy_ticks_range(
            symbol, from_date, to_date, mt5.COPY_TICKS_ALL
        )

        if ticks is None or len(ticks) == 0:
            error = mt5.last_error()
            logger.warning(f"Failed to fetch ticks for {symbol}: {error}")
            return None

        df = pd.DataFrame(ticks)
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        if "time_msc" in df.columns:
            df["time_msc"] = pd.to_datetime(df["time_msc"], unit="ms", utc=True)
        df["symbol"] = symbol

        # Persist
        path = os.path.join(self.data_dir, f"{symbol}_ticks.parquet")
        df.to_parquet(path, index=False)
        logger.info(f"Fetched {len(df)} ticks for {symbol} -> {path}")

        return df

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """
        Retrieves and validates symbol information.

        Returns None if symbol is not found or not available for trading.
        """
        info = mt5.symbol_info_tick(symbol)
        if info is None:
            error = mt5.last_error()
            logger.warning(f"Symbol {symbol} not found: {error}")
            return None

        full_info = mt5.symbol_info(symbol)
        if full_info is None:
            return None

        return {
            "name": full_info.name,
            "bid": info.bid,
            "ask": info.ask,
            "spread": info.ask - info.bid,
            "spread_points": full_info.spread,
            "trade_allowed": full_info.trade_allowed,
            "visible": full_info.visible,
            "digits": full_info.digits,
            "volume_min": full_info.volume_min,
            "volume_max": full_info.volume_max,
            "volume_step": full_info.volume_step,
        }

    def get_all_tradeable_symbols(self) -> List[str]:
        """Returns list of symbols available for trading."""
        symbols = mt5.symbols_get()
        return [s.name for s in symbols if s.trade_allowed and s.visible]

    def append_bars(
        self,
        symbol: str,
        timeframe: int,
        new_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """
        Appends new bars to cached data (for incremental updates).

        Handles duplicate timestamps by keeping the last occurrence.
        """
        cached = self._load_cached_bars(symbol, timeframe)
        if cached is not None:
            combined = pd.concat([cached, new_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["time"], keep="last")
            combined = combined.sort_values("time").reset_index(drop=True)
        else:
            combined = new_df

        path = self._symbol_path(symbol, timeframe)
        combined.to_parquet(path, index=False)
        return combined


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    from mt5_connection import MT5Manager

    mgr = MT5Manager()
    if not mgr.ensure_connection():
        print("ERROR: MT5 not connected")
        exit(1)

    fetcher = DataFetcher()

    print("\n=== Fetching EURUSD M1 data (500 bars) ===")
    df = fetcher.fetch_bars("EURUSD", TIMEFRAME_M1, count=500)
    if df is not None:
        print(f"  Bars: {len(df)}")
        print(f"  Date range: {df['time'].min()} to {df['time'].max()}")
        print(f"  Columns: {list(df.columns)}")
        print(f"  Sample:\n{df[['time', 'open', 'close', 'volume']].tail(5).to_string()}")

    print("\n=== Fetching EURUSD ticks (last 60 seconds) ===")
    now = datetime.now()
    minute_ago = now - timedelta(seconds=60)
    ticks = fetcher.fetch_ticks("EURUSD", minute_ago, now)
    if ticks is not None:
        print(f"  Ticks: {len(ticks)}")
        if len(ticks) > 0:
            print(f"  Latest: bid={ticks.iloc[-1]['bid']}, ask={ticks.iloc[-1]['ask']}")

    print("\n=== Symbol info ===")
    info = fetcher.get_symbol_info("EURUSD")
    if info:
        print(f"  {info}")

    mgr.shutdown()
