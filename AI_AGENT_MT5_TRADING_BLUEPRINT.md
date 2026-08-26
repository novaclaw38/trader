# AI-Agent-Powered MetaTrader 5 Trading System: Technical Blueprint

**Version:** 1.0 | **Date:** 2026-08-25 | **Audience:** Financial technologists, algorithmic trading engineers, AI agent architects

This document is a comprehensive research synthesis and reference blueprint for building a production-grade trading bot powered by AI agents that monitor live trades via MetaTrader 5. It organizes proven methodologies, architectural patterns, and tooling across three strategy categories (high-frequency/scalping, swing trading, multi-strategy) to enable informed engineering decisions.

---

## Table of Contents

1. [Trading Fundamentals](#1-trading-fundamentals)
2. [AI Agent Architecture](#2-agent-architecture)
3. [MetaTrader 5 Integration](#3-mt5-integration)
4. [Web Application Stack](#4-web-application-stack)
5. [Implementation Roadmap](#5-implementation-roadmap)
6. [Resource Index](#6-resource-index)

---

## 1. Trading Fundamentals

### 1.1 Core Principles (Strategy-Agnostic)

The separation between professional systematic traders and retail traders is almost entirely **process**, not information. The following principles are non-negotiable in any automatable system:

| Principle | Implementation | Why It Matters |
|-----------|---------------|----------------|
| **Rule-based determinism** | Every entry, exit, sizing decision is encoded as explicit logic before any trade executes | Eliminates retrospective narrative bias ("I would have skipped that trade") |
| **Hard limits before signals** | Risk constraints run as an independent enforcement layer, not inside the strategy itself | Prevents strategies from rationalizing risk limit violations |
| **Broker state as source of truth** | Account balances, positions, and orders are read directly from MT5, never from internal strategy caches | Survives restarts, partial fills, manual intervention, and connection interruptions |
| **Explicit state enumeration** | Risk states are finite and deterministic (e.g., NORMAL → REDUCED → ENTRIES_BLOCKED → EMERGENCY_FLATTEN) | Removes ambiguity in enforcement; enables automated audit and testing |
| **Supplement, don't replace** | ML/AI anomaly detection supplements hard limits but never overrides them | Prevents model confidence scores from bypassing capital protection |

### 1.2 Strategy Category Comparison

#### A. High-Frequency / Scalping

| Parameter | Specification |
|-----------|---------------|
| **Hold time** | Seconds to minutes |
| **Trade frequency** | 10–50+ trades per day |
| **Typical win rate** | 55–65% |
| **Reward-to-risk ratio** | 1.5:1 to 3:1 |
| **Key risk constraint** | Spread cost dominates; max spread percentile filter required |
| **Execution tolerance** | < 500ms signal-to-fill; slippage must be monitored per trade |
| **Data granularity** | Tick data or 1-minute bars minimum |
| **Portfolio heat** | Individual trades small (0.25–0.5% equity); aggregate exposure capped at 2–3% |
| **Backtest minimum** | 2–3 years of tick/1M data; Monte Carlo validation recommended |
| **AI latency budget** | Inference must complete in < 100ms for real-time signal generation |

**Proven methodologies:**
- **EMA crossover with VWAP filter** — 9/21 EMA cross on M1/M5 with VWAP as trend direction filter (documented across multiple retail and institutional sources; backtested extensively)
- **Order block / liquidity sweep detection** — Identifies institutional order flow zones using fractal analysis; implemented in `carlosrod723/MQL5-Trading-Bot` with LSTM confirmation layer
- **Mean reversion on spread extremes** — Pairs trading or basket scalping when spread exceeds 2 standard deviations from rolling median
- **Momentum ignition** — Entry on volume spike + price level breakout with immediate partial profit targets

> **Confidence Level:** Medium-High. Scalping edges are real but decay faster than swing edges due to competition and spread costs. Live performance consistently underestimates slippage versus backtests. The 58% win rate / 1.8:1 R:R profile cited in systematic scalping research is achievable but requires institutional-grade execution infrastructure (co-location, direct market access, sub-millisecond tick data).

#### B. Swing Trading

| Parameter | Specification |
|-----------|---------------|
| **Hold time** | Hours to 3–5 days |
| **Trade frequency** | 2–10 trades per week |
| **Typical win rate** | 40–55% |
| **Reward-to-risk ratio** | 2:1 to 5:1 |
| **Key risk constraint** | Overnight/weekend gap risk; correlated position exposure |
| **Execution tolerance** | Minutes acceptable; fill quality matters less than signal accuracy |
| **Data granularity** | 1H, 4H, Daily bars |
| **Portfolio heat** | Per-trade risk 1–2% equity; total portfolio heat capped at 6% |
| **Backtest minimum** | 5+ years of daily data; regime analysis across bull/sideways/bear |
| **AI latency budget** | Minutes acceptable; multi-agent debate patterns viable |

**Proven methodologies:**
- **Higher-timeframe order block + multi-timeframe confluence** — Identify institutional zones on Daily/4H, execute on 1H entry triggers; `carlosrod723/MQL5-Trading-Bot` implements this with Fibonacci zone confirmation
- **Pullback to 200-EMA with RSI/MACD confluence** — Trend-continuation entry after corrective pullback; 200-EMA as dynamic support/resistance filter quantified across multiple backtest studies
- **Squeeze momentum + volatility expansion** — Bollinger Band/Keltner Channel squeeze as precursor to directional expansion; confirmed by MACD turning and RSI divergence
- **Overnight gap management** — Systematic rules for position sizing into market close, gap-fill statistics by symbol and day of week

> **Confidence Level:** High. Swing strategies have the most robust backtest-to-live track record. The longer hold period means execution latency matters less, and transaction costs (spread + commission) are smaller relative to expected move. The "SMC Swing" approach with order blocks and multi-timeframe confluence is widely validated across QuantifiedStrategies.com, TradeSim, and independent research.

#### C. Multi-Strategy

| Parameter | Specification |
|-----------|---------------|
| **Hold time** | Mixed (seconds to days across strategies) |
| **Trade frequency** | Variable; requires portfolio-level capacity management |
| **Key risk constraint** | Cross-strategy correlation; capital allocation optimization |
| **Architecture requirement** | Orchestrator + independent strategy agents + portfolio-level risk agent |
| **AI agent pattern** | Debate/ensemble with explicit signal aggregation protocol |
| **Data granularity** | Mixed; must normalize across timeframes |
| **Backtest requirement** | Regime-stratified backtests; correlation matrix validation |

**Proven methodologies:**
- **AlphaCrafter (arXiv:2605.05580)** — Three specialized agents (momentum, mean-reversion, volatility) with explicit policy specifications and verification mechanisms; demonstrated superior risk-adjusted returns on CSI 300 and S&P 500 with lower cross-model variance than prompt-only approaches
- **TradingAgents (arXiv:2412.20138, GitHub: TauricResearch/TradingAgents)** — Multi-role agent firm: fundamentals analyst, sentiment analyst, technical analyst, bull/bear researchers, trader, risk management, portfolio manager; uses LangGraph for state orchestration; debated decisions outperform single-agent baselines in cumulative return, Sharpe ratio, and max drawdown
- **Ensemble signal voting** — N independent signal generators vote; trade only executed when consensus threshold (e.g., 70%) is met; implemented with explicit disagreement logging for model drift detection

> **Confidence Level:** Medium-High. Multi-strategy systems have the strongest theoretical foundation (Modern Portfolio Theory, ensemble methods) and the strongest published results from frameworks like TradingAgents. The primary risk is implementation complexity — independent strategy correlation is often underestimated, and the orchestration layer becomes a single point of failure if not designed with isolation guarantees.

---

## 2. AI Agent Architecture

### 2.1 Architectural Patterns by Strategy Latency

The strategy category fundamentally determines the agent architecture:

| Pattern | Latency Budget | Best For | Framework |
|---------|---------------|----------|-----------|
| **Single-pass inference** | < 100ms | Scalping, market making | Custom state machine + LLM or lightweight model |
| **Debate/ensemble loop** | 30s – 5min | Swing trading, portfolio rebalancing | LangGraph multi-agent graph |
| **Continuous monitoring + scheduled rebalance** | Minutes to hours | Multi-strategy, multi-asset | LangGraph with checkpointing + cron triggers |
| **Human-in-the-loop approval** | Discretionary | Large position entry, regime change | TradingAgents-style portfolio manager gate |

### 2.2 Production Agent Architecture

Based on the MQL5 Risk Agent architecture (2026), AlphaCrafter (arXiv:2605.05580), and TradingAgents (arXiv:2412.20138), the recommended production architecture is:

```
┌─────────────────────────────────────────────────────────────────────┐
│                     AI AGENT TRADING SYSTEM                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                   ORCHESTRATOR (LangGraph)                   │   │
│  │  - Manages agent lifecycle, state transitions, checkpointing │   │
│  │  - Routes messages between agents based on market state     │   │
│  │  - Enforces execution frequency per strategy category        │   │
│  └──────────┬──────────────────────────────────────────────────┘   │
│             │                                                        │
│  ┌──────────▼──────────┐  ┌──────────────────┐  ┌──────────────┐  │
│  │  SIGNAL AGENTS      │  │  RESEARCH AGENTS │  │ RISK AGENT   │  │
│  │  - Technical Analyst│  │  - Bull Researcher│  │ (Independent)│  │
│  │  - Sentiment Anal.  │  │  - Bear Researcher│  │              │  │
│  │  - Fundamentals     │  │  - Debate Module  │  │              │  │
│  │  - Macro/News       │  │                   │  │              │  │
│  └──────────┬──────────┘  └─────────┬─────────┘  └──────┬───────┘  │
│             │                       │                    │           │
│  ┌──────────▼───────────────────────▼────────────────────▼───────┐  │
│  │                   TRADER AGENT                                │  │
│  │  - Synthesizes analyst/researcher output                     │  │
│  │  - Proposes trade: {symbol, direction, entry, SL, TP, size}  │  │
│  └──────────┬────────────────────────────────────────────────────┘  │
│             │                                                        │
│  ┌──────────▼────────────────────────────────────────────────────┐  │
│  │                   PORTFOLIO MANAGER / EXECUTION              │  │
│  │  - Receives trade proposals                                  │  │
│  │  - Queries Risk Agent for current risk state                 │  │
│  │  - Approves, modifies, or rejects based on hard limits       │  │
│  │  - Constructs MT5 trade request                              │  │
│  └──────────┬────────────────────────────────────────────────────┘  │
│             │                                                        │
│  ┌──────────▼────────────────────────────────────────────────────┐  │
│  │                   MT5 EXECUTION ADAPTER                       │  │
│  │  - Sends order via MT5 Python API or ZeroMQ bridge            │  │
│  │  - Validates fill, logs result, updates position tracker      │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.3 Risk Agent: Independent Enforcement Layer

The MQL5 blog (2026) establishes that the risk agent must be **process-isolated** from signal generation. This is the single most critical architectural decision in the entire system.

**Risk Agent Data Flow:**

```
BROKER STATE (MT5 account, positions, orders, deals, equity, margin)
    + PORTFOLIO STATE (exposure per symbol/strategy/correlation group)
    + MARKET STATE (spread, volatility, session, news)
    + SYSTEM STATE (order rejection rate, connection health, data freshness)
    → RISK SNAPSHOT (normalized, timestamped, source-tagged)
    → HARD-LIMIT CHECKS (deterministic rules, no ML)
    + OPTIONAL ANOMALY DETLEMENT (ML supplement only)
    → RISK STATE (NORMAL | REDUCED | ENTRIES_BLOCKED | EMERGENCY_FLATTEN)
    → ENFORCEMENT POLICY (codeable actions strategies cannot override)
    → MT5 ORDER AUTHORITY (execute or block)
```

**Hard Limit Examples (must be deterministic, never ML-overridable):**
- Maximum daily loss (e.g., 3% of day-start equity → ENTRIES_BLOCKED)
- Maximum drawdown from peak (e.g., 10% → ENTRIES_BLOCKED; 15% → EMERGENCY_FLATTEN)
- Maximum portfolio heat (e.g., 6% total open risk)
- Maximum spread percentile (e.g., reject if spread > 90th percentile of 200-bar history)
- Minimum margin level (e.g., block if margin level < 200%)
- Maximum order rejection rate (e.g., 3 rejections in 10 minutes → ENTRIES_BLOCKED)
- Maximum data age (e.g., block if last tick > 60s old)

**Anomaly Detection (supplement, never replacement):**
- Unusual combination of widened spreads + high volatility + rising rejection rates
- Concentration drift (portfolio increasingly correlated without explicit authorization)
- Regime shift detection (strategy performance degrades beyond expected variance)

> **Source Quality:** The risk agent architecture from the MQL5 blog (Michael Prescott Burney, 2026) is a practical framework, not yet independently validated. The principle of separating risk from signals is well-established in institutional trading (see: "The Risk Manager's Handbook" by Ronald L. Wheatley; Two Sigma, Citadel risk frameworks). The specific state machine and enforcement pattern is a solid engineering guide that should be adapted to your broker's margin rules.

### 2.4 Agent Communication Protocols

For production multi-agent systems, the communication layer must guarantee:
- **At-least-once delivery** (no signal or risk state change is lost)
- **Ordered delivery** within an agent's state sequence (causal consistency)
- **Audit persistence** (every agent output is logged with timestamps and reasoning)

| Pattern | Use Case | Implementation |
|---------|----------|----------------|
| **State graph** | Complex workflows with branching/parallelism | LangGraph (LangChain) |
| **Message queue** | Decoupled agents, async execution | Redis Streams, ZeroMQ, RabbitMQ |
| **Direct function call** | Tight-loop, low-latency agents (scalping) | In-process with async/await |
| **Event bus** | Broadcast market data to all agents | Redis Pub/Sub, NATS |

### 2.5 Framework Deep-Dive

#### TradingAgents (TauricResearch)
- **Repository:** https://github.com/TauricResearch/TradingAgents
- **Stars:** 15,000+ (as of mid-2026)
- **Architecture:** LangGraph-based multi-agent system
- **Roles:** Fundamentals Analyst, Sentiment Analyst, News Analyst, Technical Analyst, Bull/Bear Researchers, Trader, Risk Management, Portfolio Manager
- **Results:** Demonstrated superiority over baseline models in cumulative returns, Sharpe ratio, and max drawdown in published experiments
- **Strengths:** Most complete open-source implementation of the trading-firm model; active development (v0.3.1 as of July 2026); multi-provider LLM support (OpenAI, Gemini, Claude, DeepSeek, Qwen, Bedrock, local models)
- **Limitations:** Designed primarily for equities (Yahoo Finance data); MT5/forex integration requires custom adapter layer; research-purpose only (not production-validated)
- **Adaptation for MT5:** Replace Yahoo Finance data source with `MetaTrader5` Python package or `aiomql`; replace simulated exchange with MT5 execution adapter

#### AlphaCrafter (arXiv:2605.05580)
- **Approach:** Structured agent harness with programmable policy specifications, execution constraints, and explicit verification
- **Agents:** Momentum, Mean-Reversion, Volatility in daily-frequency rotation
- **Results:** Superior risk-adjusted returns on CSI 300 and S&P 500; substantially lower cross-model and cross-trial variance than unconstrained prompt execution
- **Key insight:** Harness-based agent design (constrained, verifiable) outperforms free-form LLM reasoning for quantitative trading
- **Recommendation:** Use this as the design philosophy for any agent that touches money — always constrain with explicit verification

#### LangGraph (LangChain)
- **Repository:** https://github.com/langchain-ai/langgraph
- **Best for:** Stateful multi-agent orchestration with checkpointing, conditional routing, and human-in-the-loop support
- **Production readiness:** 57.3% of organizations building LLM agents now run them in production (2025 State of Agent Engineering survey)
- **Key features:** Graph-based state management, checkpoint-based recovery, explicit reducer-driven state schemas, parallel execution with safety guarantees
- **Why it fits:** Trading agents are inherently stateful (positions, account state, risk state); LangGraph's checkpointing ensures recovery after crashes without replaying trades

---

## 3. MetaTrader 5 Integration

### 3.1 API Architecture Decision Matrix

| Integration Method | Latency | Complexity | Reliability | Best For |
|-------------------|---------|-----------|-------------|----------|
| **Official Python package** (`MetaTrader5`) | Medium (~50-200ms) | Low | High | Swing trading, data analysis, monitoring |
| **aiomql** (async wrapper) | Medium | Medium | High | Multi-strategy concurrent execution |
| **ZeroMQ bridge** (EA ↔ Python) | Low (~10-50ms) | High | Medium | Scalping, real-time tick streaming, EA extension |
| **WebSocket bridge** | Low (~10-50ms) | High | Medium | Cross-platform, cloud-hosted AI ↔ on-premise MT5 |
| **pymt5adapter** | Medium | Low | High | Drop-in replacement with better ergonomics |

### 3.2 Official Python Package

```python
import MetaTrader5 as mt5

# Initialize connection
if not mt5.initialize():
    print("initialize() failed")
    mt5.shutdown()

# Login
authorized = mt5.login(login=12345678, password="password", server="Broker-Demo")
if authorized:
    print(mt5.account_info())
else:
    print("Login failed:", mt5.last_error())

# Get data
rates = mt5.copy_rates_from_pos("EURUSD", mt5.TIMEFRAME_M1, 0, 1000)

# Send order
request = {
    "action": mt5.TRADE_ACTION_DEAL,
    "symbol": "EURUSD",
    "volume": 0.01,
    "type": mt5.ORDER_TYPE_BUY,
    "price": mt5.symbol_info_tick("EURUSD").ask,
    "deviation": 20,
    "magic": 234000,
    "comment": "AI agent signal",
    "type_time": mt5.ORDER_TIME_GTC,
    "type_filling": mt5.ORDER_FILLING_IOC,
}
result = mt5.order_send(request)
```

**Reference:** https://www.mql5.com/en/docs/python_metatrader5

### 3.3 aiomql (Async Framework)

**Repository:** https://github.com/Ichinga-Samuel/aiomql
**Stars:** 386+ (as of Nov 2024)
**Python:** 3.13+

This is the most complete open-source Python framework for MT5 bot development:

```python
from aiomql import Strategy, ForexSymbol, TimeFrame, Tracker, OrderType, Sessions, Trader, ScalpTrader, Bot

class EMAXOver(Strategy):
    ttf: TimeFrame
    tcc: int
    fast_ema: int
    slow_ema: int

    parameters = {
        "ttf": TimeFrame.H1,
        "tcc": 3000,
        "fast_ema": 34,
        "slow_ema": 55,
        "interval": TimeFrame.M15,
        "timeout": 3 * 60 * 60,
    }

    def __init__(self, *, symbol: ForexSymbol, params=None, trader=None, sessions=None, name="EMAXOver"):
        super().__init__(symbol=symbol, params=params, sessions=sessions, name=name)
        self.tracker = Tracker(snooze=self.interval.seconds)
        self.trader = trader or ScalpTrader(symbol=self.symbol)

    async def find_entry(self):
        candles = await self.symbol.copy_rates_from_pos(
            timeframe=self.ttf, count=self.tcc
        )
        candles.ta.ema(length=self.fast_ema, append=True)
        candles.ta.ema(length=self.slow_ema, append=True)
        # ... crossover detection logic ...

    async def trade(self):
        await self.find_entry()
        if self.tracker.order_type is None:
            await self.sleep(secs=self.tracker.snooze)
        else:
            await self.trader.place_trade(
                order_type=self.tracker.order_type, parameters=self.parameters
            )

# Wire up with multi-process execution
def run_forex():
    bot = Bot()
    symbols = [ForexSymbol(name=s) for s in ["EURUSD", "GBPUSD", "USDJPY"]]
    strategies = [EMAXOver(symbol=sym) for sym in symbols]
    bot.add_strategies(strategies)
    bot.execute()

if __name__ == "__main__":
    run_forex()
```

**Key features:**
- Async-first with auto-reconnection
- Bot orchestrator for multi-strategy concurrent execution
- Process pool execution for complete isolation between strategy categories
- Built-in risk management (RAM module), session management, position tracking
- JSON configuration; SQLite/CSV trade recording
- Community extensions: ScalpTrader, ForexSymbol, Chaos strategy

### 3.4 ZeroMQ Bridge (Low-Latency Path)

For scalping or any strategy requiring < 100ms MT5 round-trip:

**MQL5 Side (EA):**
```mql5
// JsonAPI.mq5 — loads ZeroMQ library
#include <Zmq/Zmq.mq5>
Context context("my_context");
Socket subscriber(context, ZMQ_SUBSCRIBE);
Socket publisher(context, ZMQ_PUB);

// Subscribe to Python commands
subscriber.connect("tcp://localhost:5555");
// Publish market data / order results
publisher.bind("tcp://localhost:5556");

// In OnTick():
ZmqMsg request;
subscriber.recv(request);
// Parse JSON, execute order, publish result
```

**Python Side:**
```python
import zmq
import json

context = zmq.Context()
# Send commands to MT5
command_socket = context.socket(zmq.PUB)
command_socket.bind("tcp://*:5555")
# Receive data from MT5
data_socket = context.socket(zmq.SUB)
data_socket.bind("tcp://*:5556")
data_socket.subscribe("")

# Send trade command
command = {
    "action": "ORDER_SEND",
    "symbol": "EURUSD",
    "volume": 0.01,
    "type": "BUY",
    "price": 1.0850,
    "sl": 1.0830,
    "tp": 1.0880,
    "magic": 234000,
}
command_socket.send_string(json.dumps(command))
```

**Reference Repos:**
- `aminch8/MT5-ZeroMQ` — MQL5 EA for ZeroMQ messaging brokerage
- `miya779/python3-mql5-zeromq-connector` — Python 3 to MT5 connector (forked from Darwinex)
- `parrondo/mql5_zmq_backtrader` — Full MQL5-ZMQ-Backtrader bridge

**SSRN Paper:** "SUM3API: Using Rust, ZeroMQ, and MQL5 to Extract, Communicate, and Externally Project Financial Data from MT5" (Rembrant Albeos, 2025) — demonstrates production-grade Rust-MQL5 bridge for institutional data pipelines.

### 3.5 WebSocket Bridge (Cloud ↔ On-Premise)

When the AI agent runs on cloud infrastructure and MT5 runs on a local workstation or VPS:

```python
# Python FastAPI WebSocket server (cloud)
from fastapi import FastAPI, WebSocket
import json

app = FastAPI()

@app.websocket("/ws/trading")
async def trading_socket(websocket: WebSocket):
    await websocket.accept()
    while True:
        # Receive signal from AI agent
        signal = await websocket.receive_json()
        # Forward to local MT5 WebSocket bridge
        mt5_result = await forward_to_mt5(signal)
        await websocket.send_json(mt5_result)
```

The MQL5 EA acts as a WebSocket client that connects to this server and executes orders on the terminal.

**Reference:** https://www.mql5.com/en/articles/16012 — "Integration of Broker APIs with Expert Advisors using MQL5 and Python"

### 3.6 Strategy-Specific Integration Requirements

| Requirement | Scalping | Swing | Multi-Strategy |
|-------------|----------|-------|-----------------|
| **Tick data** | Required (copy_ticks_from) | Optional (M15/H1 sufficient) | Mixed per strategy |
| **Order execution** | Immediate-or-cancel (IOC) preferred | Good-'til-cancelled (GTC) acceptable | Per-strategy config |
| **Slippage control** | Max 1-2 ticks | Max 5-10 ticks | Per-strategy config |
| **Reconnection** | Critical (< 5s recovery) | Important (< 30s recovery) | Per-strategy config |
| **Concurrent strategies** | Isolated processes, independent MT5 connections | Thread pool with shared connection | Process pool per category |
| **Account state polling** | Every tick or < 1s | Every 1-5 minutes | Per-strategy frequency |
| **Magic number** | Unique per strategy instance | Unique per strategy | Unique per strategy, prefix-coded by category |

---

## 4. Web Application Stack

### 4.1 Technology Recommendations

| Component | Primary Recommendation | Alternative |
|-----------|----------------------|-------------|
| **Backend API** | Python FastAPI | Django Channels |
| **Real-time transport** | WebSocket (native) | Server-Sent Events (SSE) for one-way |
| **Task queue** | Redis + Celery / RQ | BullMQ (if Node.js backend) |
| **State management** | Redis | PostgreSQL + LISTEN/NOTIFY |
| **Frontend** | React + TypeScript | Vue 3 + TypeScript |
| **State store (client)** | Zustand / Redux Toolkit | Jotai |
| **Charts** | TradingView Lightweight Charts | Recharts / ECharts |
| **UI framework** | Tailwind CSS + shadcn/ui | Chakra UI |
| **Database** | PostgreSQL (trades, logs) + Redis (state) | TimescaleDB for tick data |
| **Containerization** | Docker Compose | Kubernetes for multi-broker |
| **Hosting (MT5)** | Windows VPS (e.g., Contabo, ForexVPS) | Local workstation with port forwarding |

### 4.2 Real-Time Dashboard Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        REACT FRONTEND                               │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  TradingView Charts  │  Portfolio Overview  │  Risk Monitor  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                        WebSocket (JSON)                            │
└────────────────────────────┬────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────────┐
│                        FASTAPI BACKEND                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ REST API     │  │ WebSocket Mgr│  │ Task Queue (Celery/Redis)│  │
│  │ /api/trades  │  │ /ws/live     │  │ - Agent scheduling       │  │
│  │ /api/agents  │  │ /ws/risk     │  │ - Signal generation      │  │
│  │ /api/account │  │ /ws/orders   │  │ - Risk evaluation        │  │
│  └──────┬───────┘  └──────┬───────┘  └────────────┬─────────────┘  │
│         │                 │                        │                │
│  ┌──────▼─────────────────▼────────────────────────▼─────────────┐  │
│  │                    AGENT ORCHESTRATOR                          │  │
│  │  - LangGraph state machines for each strategy category        │  │
│  │  - Risk agent (independent process)                           │  │
│  │  - MT5 adapter layer (connection pool per strategy)           │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.3 FastAPI WebSocket Blueprint

```python
# backend/main.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from contextlib import asynccontextmanager
import asyncio
import json
from agent_orchestrator import AgentOrchestrator
from risk_agent import RiskAgent
from mt5_adapter import MT5Adapter

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize on startup
    app.state.mt5 = MT5Adapter()
    app.state.risk_agent = RiskAgent(app.state.mt5)
    app.state.orchestrator = AgentOrchestrator(app.state.mt5, app.state.risk_agent)
    await app.state.orchestrator.start()
    yield
    await app.state.orchestrator.shutdown()

app = FastAPI(lifespan=lifespan)

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active:
            await connection.send_json(message)

manager = ConnectionManager()

@app.websocket("/ws/live")
async def live_feed(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Receive control commands from frontend
            data = await websocket.receive_json()
            if data.get("action") == "get_positions":
                positions = await app.state.mt5.get_positions()
                await websocket.send_json({"type": "positions", "data": positions})
            elif data.get("action") == "emergency_flatten":
                await app.state.risk_agent.emergency_flatten()
                await websocket.send_json({"type": "status", "state": "EMERGENCY_FLATTEN"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# Background task: broadcast live updates
@app.on_event("startup")
async def start_broadcast():
    async def broadcast_loop():
        while True:
            snapshot = await app.state.risk_agent.get_snapshot()
            await manager.broadcast({"type": "risk_update", "data": snapshot})
            await asyncio.sleep(1)  # 1Hz for swing; increase for scalping
    asyncio.create_task(broadcast_loop())
```

### 4.4 Frontend State Management (React + WebSocket)

```typescript
// frontend/hooks/useTradingSocket.ts
import { useEffect, useRef, useCallback } from 'react';
import { useTradingStore } from '../store/tradingStore';

export function useTradingSocket() {
  const ws = useRef<WebSocket | null>(null);
  const { setRiskState, addTrade, updatePosition } = useTradingStore();

  useEffect(() => {
    ws.current = new WebSocket('ws://localhost:8000/ws/live');

    ws.current.onmessage = (event) => {
      const message = JSON.parse(event.data);
      switch (message.type) {
        case 'risk_update':
          setRiskState(message.data);
          break;
        case 'new_trade':
          addTrade(message.data);
          break;
        case 'position_update':
          updatePosition(message.data);
          break;
      }
    };

    return () => ws.current?.close();
  }, []);

  const sendCommand = useCallback((command: object) => {
    ws.current?.send(JSON.stringify(command));
  }, []);

  return { sendCommand };
}
```

### 4.5 Trading Dashboard UI Components

**Recommended open-source dashboard:**
- `galafis/trading-dashboard` — Professional React + Recharts + Tailwind CSS dashboard with interactive charts, portfolio tracking, and market analytics
- `naveennk045/Realtime-stock-price-streaming` — Real-time streaming via Upstox WebSocket v3 + FastAPI (adaptable to MT5)

**Essential views for an AI-agent trading system:**

1. **Live Risk Dashboard** — Current risk state (NORMAL/REDUCED/BLOCKED/EMERGENCY), daily P&L, margin level, open exposure by symbol/strategy
2. **Agent Monitor** — Per-agent status (running/idle/error), last action timestamp, current reasoning chain, confidence scores
3. **Position Manager** — All open positions with entry/SL/TP, floating P&L, strategy attribution, time-in-trade
4. **Signal Feed** — Real-time stream of agent-generated signals with approval/rejection status and risk agent response
5. **Performance Analytics** — Equity curve, drawdown profile, Sharpe/Sortino ratios, win rate by strategy/symbol/timeframe, Monte Carlo projection

---

## 5. Implementation Roadmap

### 5.1 Phase 1: Foundation (Weeks 1-3)

**Goal:** MT5 connection + data pipeline + basic risk framework

| Skill/Tool | Purpose |
|-----------|---------|
| Python 3.12+ | Primary development language |
| `MetaTrader5` Python package | Official MT5 API binding |
| `pandas` + `numpy` | Data manipulation |
| `pandas-ta` or `TA-Lib` | Technical indicator computation |
| `asyncio` | Async event loop for concurrent strategies |
| `pytest` + `pytest-asyncio` | Testing framework |
| `structlog` or `loguru` | Structured logging for audit trail |

**Deliverables:**
- [ ] MT5 login/connection module with auto-reconnect
- [ ] Historical data fetcher (bars + ticks) with Parquet persistence
- [ ] Technical indicator pipeline (EMA, RSI, MACD, ATR, Bollinger, VWAP)
- [ ] Hard-limit risk engine (daily loss, max positions, spread filter, margin level)
- [ ] Order send module with full error handling and result logging
- [ ] Backtesting framework (vectorbt or backtrader) for strategy validation

**Validation:** Backtest one simple strategy (e.g., EMA crossover) across 5+ years of daily data; verify Sharpe > 0.5 and max drawdown < 20% before proceeding.

### 5.2 Phase 2: Strategy Implementation (Weeks 4-6)

Choose your primary strategy category:

#### Option A: Scalping Focus

| Skill/Tool | Purpose |
|-----------|---------|
| `aiomql` | Async MT5 framework with ScalpTrader extension |
| `ZeroMQ` (pyzmq) | Low-latency bridge for tick streaming |
| MQL5 custom EA | Tick-level signal generation with Python fallback |
| `redis` | In-memory state store for sub-millisecond signal aggregation |

**Architecture:**
```
Python (aiomql) → MT5 EA (ZeroMQ publisher) → Python (ZeroMQ subscriber)
     → Signal Agent (<100ms) → Risk Agent (<10ms hard limit check) → Order execution
```

**Key constraint:** End-to-end latency must be < 500ms. If your ML model inference exceeds this, use a lightweight model (XGBoost, logistic regression) instead of LLM for the scalping signal, and reserve the LLM for pre-trade market regime classification (updated every 1-5 minutes, not per tick).

#### Option B: Swing Trading Focus

| Skill/Tool | Purpose |
|-----------|---------|
| `aiomql` | Strategy framework with session management |
| `LangGraph` | Multi-agent orchestration for analyst/researcher/trader roles |
| `sqlalchemy` + `alembic` | Database ORM and migration |
| LLM API (Claude/GPT/DeepSeek) | Agent reasoning, debate synthesis |

**Architecture:**
```
Scheduled trigger (every 15-60 min) → Analyst Agents (parallel)
    → Researcher Debate (bull/bear)
    → Trader Agent (proposes trade)
    → Risk Agent (evaluates proposal against hard limits)
    → Portfolio Manager (approve/modify/reject)
    → MT5 Execution Adapter → Order send
    → Result logging → WebSocket broadcast to dashboard
```

**Key constraint:** The entire pipeline must complete within your rebalancing interval (e.g., if checking every 15 minutes, the agent debate + execution must finish in < 12 minutes to leave buffer).

#### Option C: Multi-Strategy Focus

| Skill/Tool | Purpose |
|-----------|---------|
| `aiomql` Bot.process_pool() | Isolated process execution per strategy category |
| `LangGraph` | Per-category agent orchestration |
| `redis` | Inter-process state sharing |
| `celery` | Distributed task queue |
| `docker-compose` | Containerized strategy isolation |

**Architecture:**
```
Orchestrator (LangGraph)
    ├── Scalping Bot (Process 1: aiomql + ZeroMQ)
    ├── Swing Bot (Process 2: aiomql + LangGraph agents)
    ├── Momentum Bot (Process 3: daily-frequency agents)
    └── Risk Agent (Independent Process: reads MT5 state directly)
         └── Hard-limit enforcement across all strategies
```

**Key constraint:** Risk agent must have a global view of all positions across all strategies. Use a shared PostgreSQL database or Redis state store that every strategy writes to on every order event.

### 5.3 Phase 3: AI Agent Integration (Weeks 6-9)

| Skill/Tool | Purpose |
|-----------|---------|
| `langchain` + `langgraph` | Agent framework, state management, tool calling |
| `chromadb` or `qdrant` | Vector database for strategy memory and pattern retrieval |
| `pydantic` | Structured output validation for agent responses |
| `openai` / `anthropic` / `google-genai` | LLM providers for agent reasoning |
| `prometheus-client` + `grafana` | Observability and metrics |

**Agent Roles to Implement:**
1. **Market Analyst** — Processes OHLCV + indicator data, identifies patterns
2. **Sentiment Analyst** — Processes news feeds (Alpha Vantage, FRED, RSS) into sentiment score
3. **Risk Agent** — Independent hard-limit enforcement (no LLM; pure deterministic code)
4. **Portfolio Manager** — LLM-assisted approval/rejection with explicit reasoning logged
5. **Audit Agent** — Logs every decision, reasoning, and outcome for post-trade analysis

**LangGraph State Schema (Example):**
```python
from langgraph.graph import StateGraph
from typing import TypedDict, Literal

class TradingState(TypedDict):
    symbol: str
    signal: Literal["BUY", "SELL", "HOLD"]
    confidence: float
    risk_state: Literal["NORMAL", "REDUCED", "ENTRIES_BLOCKED", "EMERGENCY_FLATTEN"]
    trade_proposal: dict | None
    execution_result: dict | None
    reasoning: str

def should_execute(state: TradingState) -> str:
    if state["risk_state"] in ("ENTRIES_BLOCKED", "EMERGENCY_FLATTEN"):
        return "block"
    if state["signal"] == "HOLD":
        return "hold"
    return "execute"

graph = StateGraph(TradingState)
graph.add_node("analyst", run_analyst)
graph.add_node("risk_check", run_risk_agent)
graph.add_node("trader", run_trader)
graph.add_node("portfolio_mgr", run_portfolio_manager)
graph.add_node("execute", execute_trade)

graph.add_edge("analyst", "risk_check")
graph.add_conditional_edges("risk_check", should_execute, {
    "block": "hold",
    "execute": "trader",
    "hold": "hold"
})
graph.add_edge("trader", "portfolio_mgr")
graph.add_edge("portfolio_mgr", "execute")
```

### 5.4 Phase 4: Web Application (Weeks 9-12)

| Skill/Tool | Purpose |
|-----------|---------|
| `fastapi` + `uvicorn` | High-performance async web server |
| `websockets` | Real-time client-server communication |
| `react` + `typescript` | Frontend framework |
| `zustand` | Client-side state management |
| `tradingview-lightweight-charts` | Professional charting |
| `tailwindcss` + `shadcn/ui` | Modern UI components |
| `postgresql` | Persistent trade/audit storage |
| `redis` | Real-time state and pub/sub |
| `docker-compose` | Local development and deployment |

**API Endpoints:**
```
GET    /api/account              → MT5 account info
GET    /api/positions            → Open positions with P&L
GET    /api/trades               → Trade history with pagination
GET    /api/agents               → Agent status and configuration
POST   /api/agents/{id}/toggle   → Enable/disable agent
POST   /api/risk/emergency       → Trigger emergency flatten
GET    /api/risk/snapshot        → Current risk state
WS     /ws/live                  → Real-time position/risk/trade feed
WS     /ws/agents                → Real-time agent reasoning feed
```

### 5.5 Phase 5: Production Hardening (Weeks 12+)

| Concern | Implementation |
|---------|---------------|
| **Secrets management** | HashiCorp Vault or cloud KMS; never commit API keys or MT5 credentials |
| **Rate limiting** | Token bucket on order send; circuit breaker on repeated rejections |
| **Health checks** | `/health` endpoint verifying MT5 connection, agent heartbeat, data freshness |
| **Graceful degradation** | If MT5 disconnects, risk agent blocks all entries; if LLM API fails, agents fall back to last-known state |
| **Audit logging** | Append-only log of every signal, risk check, decision, and execution result |
| **Testing** | Unit tests for risk logic; integration tests with MT5 demo account; paper-trading phase |
| **Monitoring** | Prometheus metrics: signal latency, order fill latency, slippage, risk state changes, agent errors |
| **Disaster recovery** | Automated position reconciliation on restart; kill switch accessible via API and UI |

---

## 6. Resource Index

### GitHub Repositories

| Repository | Stars | Description |
|-----------|-------|-------------|
| [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) | 15,000+ | Multi-agent LLM financial trading framework; LangGraph-based; paper + code |
| [Ichinga-Samuel/aiomql](https://github.com/Ichinga-Samuel/aiomql) | 386+ | Async Python framework for MT5 bot development; strategy base class, risk management, multi-process |
| [triqbit/mt5-ai-xauusd-trader](https://github.com/triqbit/mt5-ai-xauusd-trader) | — | Institutional-grade MT5 AI/ML trading engine; DRL (PPO/Dreamer), LSTM, Transformers, Ensemble |
| [carlosrod723/MQL5-Trading-Bot](https://github.com/carlosrod723/MQL5-Trading-Bot) | — | MQL5 EA with SMC (order blocks, liquidity sweeps) + LSTM Python integration |
| [ilahuerta-IA/mt5_live_trading_bot](https://github.com/ilahuerta-IA/mt5_live_trading_bot) | — | Real-time MT5 strategy monitor with GUI and risk management |
| [jimtin/algorithmic_trading_bot](https://github.com/jimtin/algorithmic_trading_bot) | — | Python trading bot integrating MT5 + Binance |
| [Tzigger/MT5_trading_bot](https://github.com/Tzigger/MT5_trading_bot) | — | MT5 + GPT-4 integration for automated signals |
| [aminch8/MT5-ZeroMQ](https://github.com/aminch8/MT5-ZeroMQ) | — | MQL5 EA for ZeroMQ messaging |
| [miya779/python3-mql5-zeromq-connector](https://github.com/miya779/python3-mql5-zeromq-connector) | — | Python 3 to MT5 ZeroMQ connector |
| [parrondo/mql5_zmq_backtrader](https://github.com/parrondo/mql5_zmq_backtrader) | — | Full MQL5-ZMQ-Backtrader bridge |
| [galafis/trading-dashboard](https://github.com/galafis/trading-dashboard) | — | Professional React + Recharts + Tailwind trading dashboard |
| [naveennk045/Realtime-stock-price-streaming](https://github.com/naveennk045/Realtime-stock-price-streaming) | — | FastAPI + WebSocket real-time streaming |
| [Astralchemist/Expert-Advisor-trading-bot](https://github.com/Astralchemist/Expert-Advisor-trading-bot) | — | MQL5 EA with Python integration |

### Academic Papers

| Paper | Year | Key Finding |
|-------|------|-------------|
| [TradingAgents: Multi-Agents LLM Financial Trading Framework](https://arxiv.org/abs/2412.20138) | 2024 (updated 2025) | Multi-agent LLM system mirrors trading firm dynamics; debated decisions improve Sharpe ratio and max drawdown vs. single-agent baselines |
| [AlphaCrafter: Multi-Agent Framework for Cross-Sectional Quantitative Trading](https://arxiv.org/abs/2605.05580) | 2025 | Structured agent harness with programmable policy specifications outperforms unconstrained LLM reasoning in risk-adjusted returns and variance |
| [Orchestration Framework for Financial Agents: From Algorithmic to Agentic Trading](https://arxiv.org/abs/2512.02227) | 2025 | Maps trading system components to specialized agents (planner, orchestrator, alpha, risk, portfolio, backtest, execution, audit, memory); uses MCP for inter-agent communication |
| [LLM-Powered Multi-Agent System for Automated Crypto Portfolio Management](https://arxiv.org/abs/2501.00826) | 2025 | ReAct-style agents with rolling memory; 52-week backtest over top 15 crypto assets |
| [SUM3API: Rust, ZeroMQ, and MQL5 for Financial Data](https://papers.ssrn.com/sol3/Delivery.cfm/6143486.pdf) | 2025 | Production-grade MQL5-Rust bridge via ZeroMQ pub-sub and request-reply patterns |

### Documentation & Official Resources

| Resource | URL |
|----------|-----|
| MQL5 Python Integration Docs | https://www.mql5.com/en/docs/python_metatrader5 |
| LangGraph Documentation | https://www.langchain.com/langgraph |
| MetaTrader 5 Terminal | https://www.metatrader5.com/ |
| MQL5 Community (CodeBase + Articles) | https://www.mql5.com/en/code |

### Communities & Forums

| Community | URL | Focus |
|-----------|-----|-------|
| **QuantConnect Forum** | https://www.quantconnect.com/forum/ | Algorithmic trading development, LEAN engine, strategy debugging |
| **MQL5 Community** | https://www.mql5.com/en/forum | MQL5/MT4 EA development, broker API integration, trading automation |
| **TradingAgents Discord** | https://discord.com/invite/hk9PGKShPK | Multi-agent LLM trading framework community |
| **Elite Trader** | https://www.elitetrader.com/ | Institutional and professional trader discussion |
| **r/algotrading** | https://www.reddit.com/r/algotrading/ | Algorithmic trading discussion (mixed quality; verify claims) |
| **Quantra Community** | https://quantra.quantinsti.com/community | QuantInsti's algorithmic trading learning community |

### Trading Strategy Research Sources

| Source | URL | Quality |
|--------|-----|---------|
| QuantifiedStrategies.com | https://www.quantifiedstrategies.com/ | Systematic strategy backtests with exact entry/exit rules |
| TradeSim Blog | https://www.tradingsim.com/blog/ | Swing trading strategies with real entry rules |
| Jonathan Kinlay (Dover Management) | https://jonathankinlay.com/ | Professional quant research; institutional-grade strategy analysis |
| Alpha Vantage | https://www.alphavantage.co/ | Free tier financial data API (news, fundamentals, forex) |
| FRED (Federal Reserve Economic Data) | https://fred.stlouisfed.org/ | Macro-economic indicators for regime detection |

---

## Appendix A: Risk Management Configuration Template

```json
{
  "account": {
    "max_daily_loss_pct": 3.0,
    "max_drawdown_from_peak_pct": 10.0,
    "emergency_drawdown_pct": 15.0,
    "min_margin_level_pct": 200.0,
    "max_positions": 5,
    "max_pending_orders": 10
  },
  "per_strategy": {
    "max_risk_per_trade_pct": 1.0,
    "max_portfolio_heat_pct": 6.0,
    "max_correlated_exposure_pct": 4.0,
    "max_spread_percentile": 90,
    "max_slippage_ticks": 3,
    "max_consecutive_losses": 5,
    "max_order_rejections_per_10min": 3
  },
  "symbols": {
    "allowed": ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"],
    "blocked_during_news": ["XAUUSD"],
    "max_lot_per_symbol": 0.5
  },
  "sessions": {
    "allow_overnight": false,
    "allow_weekend": false,
    "news_buffer_minutes": 15
  },
  "kill_switch": {
    "enabled": true,
    "manual_disable": false,
    "emergency_flatten_on_disconnect_seconds": 30
  }
}
```

## Appendix B: Backtesting Validation Checklist

Before any strategy goes live, it must pass:

- [ ] **Minimum data:** 5+ years daily / 2+ years intraday / 1+ year tick
- [ ] **Walk-forward analysis:** Optimize on 60%, validate on 40%; never optimize on full dataset
- [ ] **Monte Carlo simulation:** 10,000+ random permutations of trade sequence; verify 95% VaR
- [ ] **Regime-stratified performance:** Bull / sideways / bear periods; reject if strategy only works in one regime
- [ ] **Transaction cost sensitivity:** Test with 2x expected spread + commission; verify still profitable
- [ ] **Slippage sensitivity:** Test with 2x and 5x expected slippage; verify drawdown remains acceptable
- [ ] **Parameter stability:** Vary key parameters ±20%; verify performance doesn't collapse
- [ ] **Out-of-sample verification:** Hold out most recent 6-12 months for final validation only
- [ ] **Paper trading:** Minimum 30-day paper trading period with exact same code as live

---

*This blueprint is a living document. As the AI agent landscape, MT5 API capabilities, and trading strategy research evolve, each section should be updated with new findings, validated implementations, and production learnings.*
