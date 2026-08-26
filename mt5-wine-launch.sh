#!/bin/bash
# MT5 on Linux via Wine - Launch Script
# MetaTrader 5 terminal + Python 3.13 + MetaTrader5 package
# Wine prefix: ~/.mt5

set -e

export WINEPREFIX=${WINEPREFIX:-~/.mt5}
export WINEDEBUG=${WINEDEBUG:-all}
export DISPLAY=${DISPLAY:-:0}

# Check if MT5 terminal is already running
if pgrep -f "terminal64.exe" > /dev/null 2>&1; then
    echo "MT5 terminal is already running."
else
    echo "Starting MT5 terminal..."
    wine "$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe" &
    sleep 10
fi

# Check if running
if pgrep -f "terminal64.exe" > /dev/null 2>&1; then
    echo "MT5 terminal is running."
    echo "To run Python: wine '$WINEPREFIX/drive_c/Program Files/Python313/python.exe' <script.py>"
else
    echo "ERROR: MT5 terminal failed to start."
    exit 1
fi
