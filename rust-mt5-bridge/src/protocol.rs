//!
//! Protocol — shared message types for the MT5↔Rust ZMQ bridge.
//!
//! All messages are JSON-serialized over ZMQ. This ensures language
//! interoperability: the MQL5 side (MQL5-ZeroMQ binding) can send/receive
//! the same structured messages as the Rust side.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

/// Unique identifier for tracking request/response pairs.
pub type MsgId = String;

/// Timestamp type used throughout the protocol.
pub type Timestamp = DateTime<Utc>;

/// A single market tick from MT5.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TickData {
    pub symbol: String,
    pub bid: f64,
    pub ask: f64,
    pub last: Option<f64>,
    pub volume: i64,
    pub time: Timestamp,
    pub bid_5: Option<Vec<f64>>, // Level 2 bid prices
    pub ask_5: Option<Vec<f64>>, // Level 2 ask prices
    pub volume_5: Option<Vec<i64>>,
}

/// Request to place an order.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderRequest {
    pub symbol: String,
    pub order_type: String, // "BUY", "SELL", "BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP"
    pub volume: f64,
    pub price: Option<f64>, // None = market order
    pub sl: Option<f64>,    // stop loss
    pub tp: Option<f64>,    // take profit
    pub deviation: u32,     // max deviation in points
    pub magic: u32,         // EA magic number
    pub comment: String,
}

/// Result of an order placement.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderResult {
    pub success: bool,
    pub order_id: Option<u64>,
    pub retcode: i32,
    pub retcode_description: String,
    pub executed_price: Option<f64>,
    pub executed_volume: Option<f64>,
    pub slippage: Option<f64>,
    pub timestamp: Timestamp,
}

/// Commands the Rust side can send to the MQL5 EA.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "cmd")]
pub enum BridgeCommand {
    #[serde(rename = "PING")]
    Ping { id: MsgId },

    #[serde(rename = "PLACE_ORDER")]
    PlaceOrder { id: MsgId, order: OrderRequest },

    #[serde(rename = "GET_TICKS")]
    GetTicks {
        id: MsgId,
        symbol: String,
        count: Option<u32>,
    },

    #[serde(rename = "GET_RATES")]
    GetRates {
        id: MsgId,
        symbol: String,
        timeframe: String,
        count: Option<u32>,
    },

    #[serde(rename = "GET_POSITION")]
    GetPosition { id: MsgId, symbol: String },

    #[serde(rename = "GET_ACCOUNT_INFO")]
    GetAccountInfo { id: MsgId },

    #[serde(rename = "CLOSE_POSITION")]
    ClosePosition {
        id: MsgId,
        symbol: String,
        ticket: Option<u64>,
    },

    #[serde(rename = "MODIFY_ORDER")]
    ModifyOrder {
        id: MsgId,
        ticket: u64,
        sl: Option<f64>,
        tp: Option<f64>,
    },

    #[serde(rename = "SUBSCRIBE_TICKS")]
    SubscribeTicks { id: MsgId, symbols: Vec<String> },

    #[serde(rename = "UNSUBSCRIBE_TICKS")]
    UnsubscribeTicks { id: MsgId, symbols: Vec<String> },
}

/// Responses from the MQL5 EA back to Rust.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum BridgeResponse {
    #[serde(rename = "PONG")]
    Pong { id: MsgId, latency_ms: f64 },

    #[serde(rename = "ORDER_RESULT")]
    OrderResult { id: MsgId, result: OrderResult },

    #[serde(rename = "TICKS")]
    Ticks { id: MsgId, ticks: Vec<TickData> },

    #[serde(rename = "RATES")]
    Rates {
        id: MsgId,
        symbol: String,
        timeframe: String,
        rates: Vec<CandleData>,
    },

    #[serde(rename = "POSITION")]
    Position {
        id: MsgId,
        position: Option<PositionInfo>,
    },

    #[serde(rename = "ACCOUNT_INFO")]
    AccountInfo { id: MsgId, info: AccountInfo },

    #[serde(rename = "SUBSCRIBED")]
    Subscribed { id: MsgId, symbols: Vec<String> },

    #[serde(rename = "UNSUBSCRIBED")]
    Unsubscribed { id: MsgId, symbols: Vec<String> },

    #[serde(rename = "ERROR")]
    Error {
        id: MsgId,
        code: i32,
        message: String,
    },
}

/// A single price candle (bar).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CandleData {
    pub time: Timestamp,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub tick_volume: i64,
    pub spread: i32,
    pub real_volume: i64,
}

/// Current position info.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PositionInfo {
    pub ticket: u64,
    pub symbol: String,
    pub type_: String, // "BUY" or "SELL"
    pub volume: f64,
    pub price_open: f64,
    pub sl: f64,
    pub tp: f64,
    pub profit: f64,
    pub swap: f64,
    pub commission: f64,
    pub time: Timestamp,
    pub time_update: Timestamp,
    pub time_open: Timestamp,
}

/// Account information snapshot.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AccountInfo {
    pub login: i64,
    pub server: String,
    pub currency: String,
    pub balance: f64,
    pub equity: f64,
    pub margin: f64,
    pub margin_free: f64,
    pub margin_level: f64,
    pub assets: f64,
    pub liabilities: f64,
    pub profit: f64,
    pub account_trade_mode: String,
    pub account_trade_expert: bool,
    pub trade_allowed: bool,
    pub min_margin_level: f64,
}

/// Wrapper for a single ZMQ message with request ID tracking.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MessageEnvelope {
    pub id: MsgId,
    pub timestamp: Timestamp,
    pub payload: BridgeCommand,
}

/// Wrapper for a response.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResponseEnvelope {
    pub id: MsgId,
    pub timestamp: Timestamp,
    pub payload: BridgeResponse,
}
