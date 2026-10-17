TRADER - MetaTrader 5 TRADING TOOLKIT
=====================================

Python modules for connecting to MetaTrader 5, pulling market data, computing indicators,
backtesting, managing risk and sending orders, plus a Rust bridge to the terminal.
AI_AGENT_MT5_TRADING_BLUEPRINT.md is a research blueprint for building an AI-agent-driven
trading system on top of MT5.

FILES
-----
  mt5_connection.py    Connect to the MT5 terminal
  mt5_data.py          Market data retrieval
  mt5_indicators.py    Technical indicators
  mt5_backtest.py      Backtesting
  mt5_live.py          Live trading loop
  mt5_orders.py        Order handling
  mt5_risk.py          Risk controls
  mt5-wine-launch.sh   Launch MT5 under Wine on Linux
  rust-mt5-bridge/     Rust bridge to MT5 (Cargo project with tests)
  data/, logs/, results/   Working data, logs and backtest results
  AI_AGENT_MT5_TRADING_BLUEPRINT.md   Architecture and roadmap for an AI-agent trading system

GETTING STARTED
---------------
The Python modules use the official MetaTrader5 package, which needs a running MT5
terminal (Windows, or Linux via Wine using mt5-wine-launch.sh). Test on a demo account.

RISK WARNING
------------
Automated trading can lose money. Use a demo account first, keep hard risk limits in place,
and never trade money you cannot afford to lose. This is not financial advice.
