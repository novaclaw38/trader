//!
//! MT5 Rust↔ZMQ Bridge
//!
//! A high-performance bridge between Rust and MetaTrader 5 via ZeroMQ.
//!
//! Architecture:
//! - `bridge` — ZMQ REQ/REP socket pair for synchronous RPC (order placement, queries)
//! - `subscriber` — ZMQ SUB socket for real-time tick streaming from MQL5 EA
//! - `publisher` — ZMQ PUB socket for publishing processed data
//! - `protocol` — Shared message types (JSON-serialized)
//! - `error` — Error types for all bridge operations
//!
//! Design principles:
//! - All message IDs are UUID-tracked for request/response correlation
//! - Circuit breaker for fault isolation
//! - Automatic reconnect with exponential backoff
//! - Heartbeat-based liveness detection
//! - All message IDs are UUID-tracked for request/response correlation
//!

pub mod bridge;
pub mod error;
pub mod protocol;
pub mod publisher;
pub mod subscriber;

pub use bridge::{BridgeState, Mt5Bridge};
pub use error::BridgeError;
pub use protocol::{
    BridgeCommand, BridgeResponse, OrderRequest, OrderResult as RustOrderResult, TickData,
};
pub use publisher::Publisher;
pub use subscriber::{TickMessage, TickSubscriber};

pub use std::result::Result;
