# MT5 Rust ZMQ Bridge

A high-performance Rust-to-MetaTrader 5 bridge using ZeroMQ for sub-millisecond IPC latency,
designed for scalping strategies where every microsecond matters.

## Architecture

The bridge implements a **REQ/REP + PUB/SUB** pattern over ZMQ, connecting Rust directly
to an MQL5 Expert Advisor running inside the MT5 terminal:

```
┌──────────────┐    ZMQ REQ/REP     ┌──────────────┐    MQL5 API    ┌──────────────┐
│   Rust App   │ ◄────────────────► │  MQL5 EA     │ ◄────────────► │   MT5 Terminal │ ◄─► Broker
│  (this crate)│   tcp://*:5555     │ (MT5_ZMQ_EA) │   MetaTrader5  │   (Wine/Linux) │
└──────────────┘                    └──────────────┘                 └──────────────┘
                                      ZMQ PUB/SUB                    
┌──────────────┐                      tcp://*:5556                    
│ TickSubscriber │ ◄─────────────────────────────────────────────────┐
└──────────────┘                                                     │
                                                                     ▼
                                                             Tick data stream
```

### Components

1. **`Mt5Bridge`** (`src/bridge.rs`) — ZMQ REQ/REP client for synchronous commands
   - Order placement, modification, cancellation
   - Tick, rate, and account info queries
   - Circuit breaker for fault isolation
   - Auto-reconnect with exponential backoff
   - Heartbeat-based liveness detection

2. **`TickSubscriber`** (`src/subscriber.rs`) — ZMQ SUB client for real-time tick streaming
   - Background thread receives and parses ticks
   - Bounded channel with backpressure
   - Topic-based subscription filtering

3. **`Publisher`** (`src/publisher.rs`) — ZMQ PUB socket for publishing processed data
   - Non-blocking sends with HWM limits

4. **`protocol`** (`src/protocol.rs`) — Shared message types (JSON-serialized)
   - `MessageEnvelope` / `ResponseEnvelope` with UUID tracking
   - Command enum: `Ping`, `PlaceOrder`, `ClosePosition`, `GetTicks`, `GetRates`, etc.

5. **`MT5_ZMQ_EA.mq5`** — MQL5 Expert Advisor that runs inside MT5
   - Binds ZMQ REP and PUB sockets
   - Translates JSON commands to MQL5 API calls
   - Publishes real-time ticks

## Installation

### Prerequisites

- **MetaTrader 5** running under Wine (see Phase 1 setup)
- **Rust toolchain** (1.75+)
- **libzmq3-dev** (system library)

### Step 1: Install Rust toolchain

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
rustc --version  # Should show 1.98.0 or newer
```

### Step 2: Install ZMQ system library

```bash
sudo apt install -y libzmq3-dev libssl-dev pkg-config
pkg-config --modversion libzmq  # Should show 4.3.x
```

### Step 3: Install MQL5 ZMQ binding

The bridge requires the `dingmaotu/mql-zmq` library for MQL5. Follow the
[Phase 1 documentation](../AI_AGENT_MT5_TRADING_BLUEPRINT.md) section on
MQL5 ZMQ binding installation:

```bash
# Clone the binding
cd /tmp
git clone --depth 1 https://github.com/dingmaotu/mql-zmq.git
# Copy include files to MT5 MQL5/Include/Zmq/
# Copy libzmq.dll to MT5 MQL5/Libraries/
```

### Step 4: Install MQL5 EA

```bash
# The EA is already placed at:
~/.mt5/drive_c/Program\ Files/MetaTrader\ 5/MQL5/Experts/MT5_ZMQ_EA.mq5

# In MT5 terminal:
# 1. Attach EA to a chart
# 2. Set ZMQ endpoints in EA inputs:
#    - REP endpoint: tcp://*:5555
#    - PUB endpoint: tcp://*:5556
```

### Step 5: Build the Rust bridge

```bash
cd rust-mt5-bridge
cargo build --release
```

### Step 6: Start the bridge binary

```bash
# Run the bridge binary directly
./target/release/mt5-bridge \
    --rep-endpoint tcp://localhost:5555 \
    --sub-endpoint tcp://localhost:5556 \
    --heartbeat-interval 5 \
    --reconnect-delay-ms 100 \
    --log-level info
```

Or integrate as a library in your Rust trading application:

```toml
[dependencies]
mt5-bridge = { path = "../rust-mt5-bridge" }
```

## Usage Examples

### Basic Bridge Connection

```rust
use mt5_bridge::{Mt5Bridge, BridgeCommand};

let mut bridge = Mt5Bridge::new("tcp://localhost:5555")?;
bridge.connect()?;

// Ping the EA to verify connectivity
let response = bridge.ping()?;
assert!(response.latency_ms < 5.0);

// Check connection state
assert_eq!(bridge.state(), BridgeState::Connected);
```

### Placing an Order

```rust
use mt5_bridge::{Mt5Bridge, OrderRequest, OrderType};
use std::time::Duration;

let mut bridge = Mt5Bridge::new("tcp://localhost:5555")?;
bridge.connect()?;

let order = OrderRequest {
    symbol: "EURUSD".to_string(),
    order_type: OrderType::Buy,
    volume: 0.01,
    price: None,        // Market order
    sl: Some(1.0790),   // Stop loss
    tp: Some(1.0810),   // Take profit
    deviation: 10,      // Max 10 points slippage
    magic: 12345,       // EA magic number
    comment: "RUST-BRIDGE".to_string(),
};

let result = bridge.place_order(order)?;
println!("Order placed: ticket={}", result.order_ticket);
```

### Receiving Real-Time Ticks

```rust
use mt5_bridge::TickSubscriber;
use zmq::Context;
use std::time::Duration;

let ctx = Context::new();
let mut subscriber = TickSubscriber::new(&ctx, "tcp://localhost:5556")?;
subscriber.subscribe("EURUSD");

// Wait for ticks (background thread handles reception)
std::thread::sleep(Duration::from_millis(500));

// Read from the channel
loop {
    match subscriber.recv_timeout(Duration::from_millis(100)) {
        Some(tick) => {
            println!("{}: bid={}, ask={}", 
                tick.data.symbol, tick.data.bid, tick.data.ask);
        }
        None => {
            println!("No ticks received");
        }
    }
}
```

### Risk-Limited Order Execution

```rust
use mt5_bridge::{Mt5Bridge, OrderRequest, OrderType};
use std::sync::Arc;

let mut bridge = Mt5Bridge::new("tcp://localhost:5555")?;
bridge.connect()?;

let order = OrderRequest {
    symbol: "EURUSD".to_string(),
    order_type: OrderType::Sell,
    volume: 0.03,
    price: None,
    sl: Some(1.0820),
    tp: Some(1.0800),
    deviation: 5,
    magic: 12345,
    comment: "SCALP-RUST".to_string(),
};

// place_order automatically enforces the circuit breaker
let result = bridge.place_order(order)?;
```

## Performance Targets

| Metric | Target | Phase 1 (Python) | Phase 2 (Rust) |
|--------|--------|-------------------|----------------|
| IPC latency | <1ms | 110ms | <1ms |
| Max round-trip (order→fill) | <5ms | N/A | <5ms |
| Tick throughput | 100k/sec | N/A | 100k/sec |
| Reconnect time | <1s | N/A | <500ms |

### Current Test Results

Integration tests validate the bridge against a mock ZMQ server:

```bash
cargo test -- --test-threads=1
# Running tests/bridge_tests.rs
# test test_command_roundtrip_account_info ... ok
# test test_tick_subscriber_receives_data ... ok
# test test_ping_latency_under_5ms ... ok
# test test_reconnect_after_disconnect ... ok
# test test_send_command_ping ... ok
# test test_send_command_not_connected ... ok
# test test_protocol_message_serialization ... ok
# test test_order_placement_roundtrip ... ok
# test test_error_on_unreachable_server ... ok
#
# test result: ok. 9 passed; 0 failed; 0.00s
```

## Protocol

### Message Format (JSON over ZMQ)

All messages are JSON-serialized. Each `MessageEnvelope` includes a UUID for correlation:

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "timestamp": "2024-01-15T10:30:00Z",
  "payload": {
    "cmd": "PLACE_ORDER",
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "order": {
      "symbol": "EURUSD",
      "order_type": "BUY",
      "volume": 0.01,
      "price": null,
      "sl": 1.0790,
      "tp": 1.0810
    }
  }
}
```

### Commands

| Command | Direction | Description |
|---------|-----------|-------------|
| `PING` | Rust → EA | Health check with latency measurement |
| `PLACE_ORDER` | Rust → EA | Place a new order |
| `CLOSE_POSITION` | Rust → EA | Close an existing position |
| `MODIFY_ORDER` | Rust → EA | Modify SL/TP on existing order |
| `GET_TICKS` | Rust → EA | Request historical tick data |
| `GET_RATES` | Rust → EA | Request historical OHLCV bars |
| `GET_ACCOUNT_INFO` | Rust → EA | Query account balance, margin, etc. |
| `GET_POSITION` | Rust → EA | Query position info for a symbol |
| `SUBSCRIBE_TICKS` | Rust → EA | Subscribe to tick stream for symbols |
| `UNSUBSCRIBE_TICKS` | Rust → EA | Stop subscribing to tick stream |

### Responses

| Response | Description |
|----------|-------------|
| `PONG` | Ping response with latency |
| `ORDER_RESULT` | Order result with ticket, retcode |
| `TICKS` | Batch of tick data |
| `RATES` | Batch of OHLCV candles |
| `ACCOUNT_INFO` | Full account state |
| `POSITION` | Position info |
| `ERROR` | Error with code and message |

## Error Handling

```rust
use mt5_bridge::{Mt5Bridge, BridgeError};

let result = bridge.place_order(order);
match result {
    Ok(order_result) => println!("Order placed: {}", order_result.order_ticket),
    Err(BridgeError::ZmqError(e)) => println!("ZMQ connection error: {}", e),
    Err(BridgeError::SerializationError(e)) => println!("JSON serialization error"),
    Err(BridgeError::ConnectionLost(msg)) => println!("Connection lost: {}", msg),
    Err(BridgeError::CircuitOpen { failures, .. }) => {
        println!("Circuit breaker open after {} failures", failures);
    }
    Err(BridgeError::HeartbeatTimeout) => println!("EA not responding"),
    Err(BridgeError::RemoteError { code, message }) => {
        println!("MT5 error {}: {}", code, message);
    }
    Err(e) => println!("Unexpected error: {:?}", e),
}
```

## Security Considerations

- **dry_run mode**: Risk engine enforces `dry_run=True` by default — orders are validated but not sent to MT5 without explicit opt-in
- **Hard limits**: Circuit breaker, max reconnect attempts (10), heartbeat timeout
- **No secrets in code**: All credentials should be passed via environment variables or config files
- **ZMQ does not encrypt**: Bind only to `localhost` or `127.0.0.1` — never to public interfaces

## Development

### Running Tests

```bash
cargo test -- --test-threads=1
```

Tests use a mock ZMQ server (`tests/test_server.rs`) that simulates the MQL5 EA,
so you don't need MT5 running to validate the Rust bridge.

### Formatting & Linting

```bash
cargo fmt   # Auto-format
cargo clippy  # Lint suggestions
```

### Release Build

```bash
cargo build --release
```

## Integration with Phase 1

The Rust bridge complements the existing Python setup:

| Component | Phase 1 (Python) | Phase 2 (Rust) |
|-----------|-------------------|----------------|
| Connection | MetaTrader5 package (IPC) | ZMQ bridge |
| Latency | ~110ms | <1ms |
| Tick stream | `mt5_live.py` | `TickSubscriber` |
| Risk engine | `mt5_risk.py` (5-state FSM) | Circuit breaker + limits |
| Order execution | `mt5_orders.py` | `Mt5Bridge::place_order()` |

To use both together, the Rust bridge can replace the Python MetaTrader5 calls:

```python
# Old (Phase 1): Python IPC to MT5 Wine
import MetaTrader5 as mt5
mt5.initialize(path="/home/rebawn/.mt5/drive_c/Program Files/MetaTrader 5/terminal64.exe")
mt5.order_send(request)

# New (Phase 2): Rust ZMQ bridge (call via FFI or separate process)
# Rust binary handles all MT5 communication
```
