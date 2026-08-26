//!
//! Error types for the MT5 ZMQ bridge.

use std::fmt;

/// Error type for the MT5 ZMQ bridge.
#[derive(Debug, Clone)]
pub enum BridgeError {
    /// ZMQ socket operation failed
    ZmqError(zmq::Error),
    /// Connection to the MQL5 EA was lost
    ConnectionLost(String),
    /// Request timed out waiting for response
    Timeout(String),
    /// The MQL5 EA returned an error response
    RemoteError { code: i32, message: String },
    /// Invalid or malformed message received
    InvalidMessage(String),
    /// Circuit breaker is open — too many consecutive failures
    CircuitOpen { failures: u32, last_error: String },
    /// Heartbeat timeout — EA stopped responding
    HeartbeatTimeout,
    /// IO error
    Io(String),
}

impl fmt::Display for BridgeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            BridgeError::ZmqError(err) => write!(f, "ZMQ error: {err}"),
            BridgeError::ConnectionLost(msg) => write!(f, "Connection lost: {msg}"),
            BridgeError::Timeout(msg) => write!(f, "Timeout: {msg}"),
            BridgeError::RemoteError { code, message } => {
                write!(f, "Remote error (code {code}): {message}")
            }
            BridgeError::InvalidMessage(msg) => write!(f, "Invalid message: {msg}"),
            BridgeError::CircuitOpen {
                failures,
                last_error,
            } => {
                write!(
                    f,
                    "Circuit breaker open after {failures} failures: {last_error}"
                )
            }
            BridgeError::HeartbeatTimeout => write!(f, "Heartbeat timeout — EA not responding"),
            BridgeError::Io(msg) => write!(f, "IO error: {msg}"),
        }
    }
}

impl std::error::Error for BridgeError {}

impl From<zmq::Error> for BridgeError {
    fn from(err: zmq::Error) -> Self {
        BridgeError::ZmqError(err)
    }
}
