#!/usr/bin/env python
"""
MT5 Connection Manager
======================
Handles MT5 terminal initialization, connection monitoring, and auto-recovery.

Key features:
- Lazy initialization (connects on first use)
- Health-check heartbeat every 5 seconds
- Auto-reconnect on COM errors or disconnections
- Terminal process restart when Wine session dies

Usage:
    from mt5_connection import MT5Manager
    mt5mgr = MT5Manager()
    if mt5mgr.ensure_connection():
        account = mt5.account_info()
"""
import MetaTrader5 as mt5
import time
import subprocess
import os
import signal
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Wine configuration
WINEPREFIX = os.environ.get("WINEPREFIX", os.path.expanduser("~/.mt5"))
WINEDEBUG = os.environ.get("WINEDEBUG", "-all")
MT5_TERMINAL_EXE = os.path.join(WINEPREFIX, "drive_c", "Program Files", "MetaTrader 5", "terminal64.exe")

# Connection health parameters
HEARTBEAT_INTERVAL = 5  # seconds between health checks
MAX_RECONNECT_ATTEMPTS = 3
RECONNECT_DELAY = 10  # seconds between reconnect attempts
RESTART_DELAY = 15  # seconds after terminal restart before retry


@dataclass
class ConnectionState:
    """Tracks the health and lifecycle state of the MT5 connection."""
    initialized: bool = False
    last_heartbeat: float = 0.0
    last_success: float = 0.0
    consecutive_failures: int = 0
    account_login: Optional[int] = None
    server: Optional[str] = None
    terminal_restart_count: int = 0
    last_error: Optional[tuple] = None


class MT5Manager:
    """
    Manages MT5 terminal lifecycle and connection health.

    The MT5 Python package uses Windows COM under Wine, which means
    connection failures can happen silently. This manager actively
    monitors and recovers.
    """

    def __init__(self):
        self.state = ConnectionState()
        self._last_check = 0.0

    def _start_terminal(self) -> bool:
        """Starts the MT5 terminal process under Wine if not already running."""
        # Check if terminal is already running
        try:
            result = subprocess.run(
                ["pgrep", "-f", "terminal64.exe"],
                capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.info("MT5 terminal already running")
                return True
        except FileNotFoundError:
            pass

        # Start the terminal
        env = os.environ.copy()
        env["WINEPREFIX"] = WINEPREFIX
        env["WINEDEBUG"] = WINEDEBUG
        env["DISPLAY"] = env.get("DISPLAY", ":0")

        logger.info(f"Starting MT5 terminal: {MT5_TERMINAL_EXE}")
        try:
            proc = subprocess.Popen(
                ["wine", MT5_TERMINAL_EXE],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            logger.info(f"Terminal process started (PID: {proc.pid})")
            # Wait for terminal to initialize
            time.sleep(10)
            return True
        except Exception as e:
            logger.error(f"Failed to start MT5 terminal: {e}")
            return False

    def _kill_terminal(self):
        """Kills the MT5 terminal process for a clean restart."""
        logger.warning("Killing MT5 terminal for restart...")
        try:
            subprocess.run(["pkill", "-f", "terminal64.exe"], timeout=10)
        except Exception:
            pass
        # Also try wineserver kill for clean Wine shutdown
        try:
            env = os.environ.copy()
            env["WINEPREFIX"] = WINEPREFIX
            subprocess.run(["wineserver", "-k"], env=env, timeout=10)
        except Exception:
            pass
        time.sleep(3)

    def _attempt_init(self) -> bool:
        """Attempts to initialize the MT5 connection."""
        try:
            if mt5.initialize():
                self.state.initialized = True
                self.state.consecutive_failures = 0
                info = mt5.account_info()
                if info:
                    self.state.account_login = info.login
                    self.state.server = info.server
                    self.state.last_success = time.time()
                    logger.info(f"MT5 connected: login={info.login}, server={info.server}")
                else:
                    # Terminal running but no account — still connected
                    self.state.last_success = time.time()
                    logger.info("MT5 terminal connected (no account logged in)")
                self.state.last_error = None
                return True
            else:
                err = mt5.last_error()
                self.state.last_error = err
                logger.warning(f"MT5 initialize failed: {err}")
                return False
        except Exception as e:
            self.state.last_error = (-1, str(e))
            logger.error(f"MT5 initialize exception: {e}")
            return False

    def _attempt_reconnect(self) -> bool:
        """Attempts to reconnect, restarting the terminal if needed."""
        # First try a simple re-initialize
        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            if self._attempt_init():
                return True
            logger.warning(f"Reconnect attempt {attempt + 1}/{MAX_RECONNECT_ATTEMPTS} failed")
            time.sleep(RECONNECT_DELAY)

        # If simple reconnect failed, restart the terminal
        logger.warning("Simple reconnect failed — restarting MT5 terminal...")
        self._kill_terminal()
        time.sleep(RESTART_DELAY)

        if self._start_terminal():
            self.state.terminal_restart_count += 1
            # Try to initialize again
            if self._attempt_init():
                return True

        return False

    def ensure_connection(self) -> bool:
        """
        Ensures MT5 is connected and healthy.

        Returns True if the connection is active, False if all recovery
        attempts have been exhausted.
        """
        # Skip checks within heartbeat interval
        if time.time() - self._last_check < HEARTBEAT_INTERVAL:
            return self.state.initialized

        self._last_check = time.time()

        if not self.state.initialized:
            return self._attempt_reconnect()

        # Health check: try a lightweight operation
        try:
            info = mt5.account_info()
            if info:
                self.state.last_heartbeat = time.time()
                self.state.last_success = time.time()
                self.state.consecutive_failures = 0
                return True
            else:
                err = mt5.last_error()
                if err[0] in (-10004, -10005):  # No IPC connection / terminal not running
                    logger.warning(f"IPC connection lost: {err}")
                    self.state.initialized = False
                    return self._attempt_reconnect()
                else:
                    logger.warning(f"Health check error: {err}")
                    return True  # Soft error, connection may still be OK
        except Exception as e:
            logger.error(f"Health check exception: {e}")
            self.state.initialized = False
            return self._attempt_reconnect()

    def shutdown(self):
        """Gracefully shuts down the MT5 connection."""
        if self.state.initialized:
            try:
                mt5.shutdown()
                logger.info("MT5 connection shut down")
            except Exception as e:
                logger.error(f"Error during shutdown: {e}")
        self.state.initialized = False

    def get_state(self) -> Dict[str, Any]:
        """Returns the current connection state for monitoring."""
        return {
            "initialized": self.state.initialized,
            "last_heartbeat": self.state.last_heartbeat,
            "last_success": self.state.last_success,
            "consecutive_failures": self.state.consecutive_failures,
            "account_login": self.state.account_login,
            "server": self.state.server,
            "terminal_restart_count": self.state.terminal_restart_count,
            "last_error": self.state.last_error,
            "uptime_seconds": time.time() - self.state.last_success if self.state.initialized else 0,
        }


# Module-level singleton for application use
_mt5_mgr: Optional[MT5Manager] = None


def get_mt5_manager() -> MT5Manager:
    """Returns a singleton MT5Manager instance."""
    global _mt5_mgr
    if _mt5_mgr is None:
        _mt5_mgr = MT5Manager()
    return _mt5_mgr


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mgr = MT5Manager()

    print("Testing MT5 connection manager...")
    if mgr.ensure_connection():
        print("Connection established!")
        print(f"State: {mgr.get_state()}")
        acct = mt5.account_info()
        if acct:
            print(f"Account: {acct.login} on {acct.server}")
            print(f"Balance: {acct.balance}, Equity: {acct.equity}")
        mgr.shutdown()
    else:
        print("Connection failed!")
        print(f"State: {mgr.get_state()}")
        exit(1)
