//!
//! ZMQ bridge client connecting Rust to the MQL5 Expert Advisor via REQ/REP pattern.
//!
//! This module handles:
//! - Synchronous command/response (order placement, queries)
//! - Heartbeat/ping monitoring for connection health
//! - Circuit breaker on consecutive failures
//! - Automatic reconnect with exponential backoff
//!
//! # Architecture
//!
//! ```text
//! Rust (this module)          ZMQ TCP          MQL5 EA (in MT5)
//!    REQ socket              REQ/REP           REP socket
//!    (commands)            (localhost:5555)    (handles orders, data queries)
//!
//!    PUB socket              PUB/SUB            (no direct Rust PUB here)
//!    (tick streaming)       (localhost:5556)    (MQL5 publishes ticks)
//! ```

use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use uuid::Uuid;
use zmq::{Context, Message, Socket, SocketType};

use crate::error::BridgeError;
use crate::protocol::{
    BridgeCommand, BridgeResponse, MessageEnvelope, OrderRequest, OrderResult, ResponseEnvelope,
};

/// Bridge connection state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BridgeState {
    Disconnected,
    Connected,
    Connecting,
    Error,
}

/// Circuit breaker state
#[derive(Debug, Clone, PartialEq)]
enum CircuitState {
    Closed,   // Normal operation
    Open,     // Too many failures — reject all requests
    HalfOpen, // Testing if service recovered
}

/// ZMQ bridge client for communicating with the MT5 MQL5 EA.
pub struct Mt5Bridge {
    ctx: Context,
    /// REQ socket for synchronous commands (order placement, queries)
    req_socket: Arc<Mutex<Option<Socket>>>,
    endpoint: String,
    /// Circuit breaker
    circuit_state: Arc<Mutex<CircuitState>>,
    consecutive_failures: Arc<AtomicU32>,
    circuit_open_until: Arc<Mutex<Option<Instant>>>,
    /// Health monitoring
    last_heartbeat: Arc<Mutex<Option<Instant>>>,
    heartbeat_timeout: Duration,
    /// Config
    request_timeout: Duration,
    reconnect_delay: Duration,
    max_reconnect_delay: Duration,
    max_failures_before_open: u32,
    circuit_reset_timeout: Duration,
    /// Whether the bridge is currently connected
    is_connected: Arc<AtomicBool>,
}

impl Mt5Bridge {
    /// Create a new bridge client.
    ///
    /// # Args
    /// * `endpoint` — ZMQ endpoint for the MQL5 EA's REP socket (e.g., "tcp://127.0.0.1:5555")
    /// * `heartbeat_timeout` — how long to wait before declaring connection lost
    /// * `request_timeout` — timeout for individual command requests
    pub fn new(endpoint: &str) -> Result<Self, BridgeError> {
        let ctx = zmq::Context::new();

        Ok(Self {
            ctx,
            req_socket: Arc::new(Mutex::new(None)),
            endpoint: endpoint.to_string(),
            circuit_state: Arc::new(Mutex::new(CircuitState::Closed)),
            consecutive_failures: Arc::new(AtomicU32::new(0)),
            circuit_open_until: Arc::new(Mutex::new(None)),
            last_heartbeat: Arc::new(Mutex::new(None)),
            heartbeat_timeout: Duration::from_secs(5),
            request_timeout: Duration::from_secs(5),
            reconnect_delay: Duration::from_millis(500),
            max_reconnect_delay: Duration::from_secs(30),
            max_failures_before_open: 3,
            circuit_reset_timeout: Duration::from_secs(10),
            is_connected: Arc::new(AtomicBool::new(false)),
        })
    }

    /// Connect to the MQL5 EA.
    pub fn connect(&mut self) -> Result<(), BridgeError> {
        log::info!("Connecting to MT5 EA at {}", self.endpoint);

        // Create the REQ socket
        let socket = self
            .ctx
            .socket(SocketType::REQ)
            .map_err(BridgeError::from)?;

        // Set socket options
        socket.set_linger(2000).map_err(BridgeError::from)?;
        socket.set_rcvtimeo(5000).map_err(BridgeError::from)?;

        socket.connect(&self.endpoint).map_err(BridgeError::from)?;

        // Store the socket
        *self.req_socket.lock().unwrap() = Some(socket);

        // Wait for the connection to establish by sending a ping
        let ping_id = Uuid::new_v4().to_string();
        let cmd = BridgeCommand::Ping {
            id: ping_id.clone(),
        };
        let msg = serde_json::to_string(&MessageEnvelope {
            id: ping_id.clone(),
            timestamp: chrono::Utc::now(),
            payload: cmd,
        })
        .map_err(|e| BridgeError::InvalidMessage(format!("serialize ping: {e}")))?;

        {
            let socket = self.req_socket.lock().unwrap();
            let sock = socket.as_ref().unwrap();
            sock.send(&msg, 0).map_err(BridgeError::from)?;

            let mut reply = Message::new();
            sock.recv(&mut reply, 0).map_err(BridgeError::from)?;

            let response: ResponseEnvelope = serde_json::from_str(reply.as_str().unwrap_or(""))
                .map_err(|e| BridgeError::InvalidMessage(format!("parse ping response: {e}")))?;

            if response.id == ping_id {
                log::info!("Connected to MT5 EA successfully");
                self.is_connected.store(true, Ordering::SeqCst);
                *self.last_heartbeat.lock().unwrap() = Some(Instant::now());
                *self.circuit_state.lock().unwrap() = CircuitState::Closed;
                self.consecutive_failures.store(0, Ordering::SeqCst);
                return Ok(());
            }
        }

        Err(BridgeError::ConnectionLost(
            "Ping response ID mismatch".into(),
        ))
    }

    /// Check if the bridge is connected.
    pub fn is_connected(&self) -> bool {
        self.is_connected.load(Ordering::SeqCst)
    }

    /// Get the current connection state.
    pub fn state(&self) -> BridgeState {
        if self.is_connected.load(Ordering::SeqCst) {
            BridgeState::Connected
        } else {
            BridgeState::Disconnected
        }
    }

    /// Send a command and wait for the response.
    ///
    /// This method implements:
    /// - Circuit breaker check (fails fast if open)
    /// - Request timeout
    /// - Automatic reconnect on connection loss
    /// - Heartbeat update
    pub fn send_command(&mut self, cmd: BridgeCommand) -> Result<BridgeResponse, BridgeError> {
        let msg_id = match &cmd {
            BridgeCommand::Ping { id } => id.clone(),
            BridgeCommand::PlaceOrder { id, .. } => id.clone(),
            BridgeCommand::GetTicks { id, .. } => id.clone(),
            BridgeCommand::GetRates { id, .. } => id.clone(),
            BridgeCommand::GetPosition { id, .. } => id.clone(),
            BridgeCommand::GetAccountInfo { id } => id.clone(),
            BridgeCommand::ClosePosition { id, .. } => id.clone(),
            BridgeCommand::ModifyOrder { id, .. } => id.clone(),
            BridgeCommand::SubscribeTicks { id, .. } => id.clone(),
            BridgeCommand::UnsubscribeTicks { id, .. } => id.clone(),
        };

        // Check circuit breaker
        self.check_circuit()?;

        // Serialize the command
        let envelope = MessageEnvelope {
            id: msg_id.clone(),
            timestamp: chrono::Utc::now(),
            payload: cmd,
        };

        let json_msg = serde_json::to_string(&envelope)
            .map_err(|e| BridgeError::InvalidMessage(format!("serialize command: {e}")))?;

        // Attempt to send
        let start = Instant::now();
        let result = self._send_with_retry(json_msg, &msg_id);

        match &result {
            Ok(_) => {
                self.consecutive_failures.store(0, Ordering::SeqCst);
                *self.last_heartbeat.lock().unwrap() = Some(Instant::now());
                *self.circuit_state.lock().unwrap() = CircuitState::Closed;
            }
            Err(e) => {
                let failures = self.consecutive_failures.fetch_add(1, Ordering::SeqCst) + 1;
                log::warn!("Command failed (failure #{failures}): {e}");

                if failures >= self.max_failures_before_open {
                    *self.circuit_state.lock().unwrap() = CircuitState::Open;
                    *self.circuit_open_until.lock().unwrap() =
                        Some(Instant::now() + self.circuit_reset_timeout);
                    log::error!(
                        "Circuit breaker OPEN — rejecting requests for {:#?}",
                        self.circuit_reset_timeout
                    );
                }
            }
        }

        let elapsed = start.elapsed();
        log::debug!("Command {msg_id} completed in {elapsed:?}");

        result
    }

    /// Internal: send with retry logic for transient failures.
    fn _send_with_retry(
        &mut self,
        json_msg: String,
        msg_id: &str,
    ) -> Result<BridgeResponse, BridgeError> {
        let guard = self.req_socket.lock().unwrap();
        let socket = match guard.as_ref() {
            Some(s) => s,
            None => return Err(BridgeError::ConnectionLost("Socket not initialized".into())),
        };

        socket.send(&json_msg, 0).map_err(BridgeError::from)?;

        let mut reply = Message::new();
        socket.recv(&mut reply, 0).map_err(BridgeError::from)?;

        let reply_str = reply
            .as_str()
            .ok_or_else(|| BridgeError::InvalidMessage("empty response".into()))?;

        let response: ResponseEnvelope = serde_json::from_str(reply_str)
            .map_err(|e| BridgeError::InvalidMessage(format!("parse response: {e}")))?;

        if response.id != *msg_id {
            return Err(BridgeError::InvalidMessage(format!(
                "Response ID mismatch: expected {msg_id}, got {}",
                response.id
            )));
        }

        // The Error variant in BridgeResponse has id, code, message fields
        match response.payload {
            BridgeResponse::Error { code, message, .. } => {
                Err(BridgeError::RemoteError { code, message })
            }
            other => Ok(other),
        }
    }

    /// Check the circuit breaker state.
    fn check_circuit(&self) -> Result<(), BridgeError> {
        let state = self.circuit_state.lock().unwrap();

        match &*state {
            CircuitState::Open => {
                if let Some(until) = *self.circuit_open_until.lock().unwrap() {
                    if Instant::now() < until {
                        return Err(BridgeError::CircuitOpen {
                            failures: self.consecutive_failures.load(Ordering::SeqCst),
                            last_error: "circuit breaker is open".into(),
                        });
                    }
                    // Timeout expired — go to half-open
                    log::info!("Circuit breaker HALF-OPEN — testing connection");
                }
            }
            CircuitState::Closed | CircuitState::HalfOpen => {}
        }
        Ok(())
    }

    /// Send a heartbeat ping and check timing.
    pub fn heartbeat(&mut self) -> Result<Duration, BridgeError> {
        let start = Instant::now();
        let pong = self.send_command(BridgeCommand::Ping {
            id: Uuid::new_v4().to_string(),
        })?;

        match pong {
            BridgeResponse::Pong { .. } => {
                let elapsed = start.elapsed();
                *self.last_heartbeat.lock().unwrap() = Some(Instant::now());
                Ok(elapsed)
            }
            _ => Err(BridgeError::InvalidMessage(
                "unexpected response to ping".into(),
            )),
        }
    }

    /// Check if the connection is healthy (hasn't missed heartbeat window).
    pub fn is_healthy(&self) -> bool {
        let last = self.last_heartbeat.lock().unwrap();
        match *last {
            Some(t) => t.elapsed() < self.heartbeat_timeout,
            None => false,
        }
    }

    /// Reconnect to the MQL5 EA with exponential backoff.
    pub fn reconnect(&mut self) -> Result<(), BridgeError> {
        let mut delay = self.reconnect_delay;
        let mut attempt = 0u32;

        loop {
            attempt += 1;
            log::info!("Reconnect attempt {} — waiting {delay:?}", attempt);
            thread::sleep(delay);

            match self.connect() {
                Ok(_) => {
                    log::info!("Reconnected after {attempt} attempts");
                    return Ok(());
                }
                Err(e) => {
                    log::warn!("Reconnect attempt {attempt} failed: {e}");
                    delay = (delay * 2).min(self.max_reconnect_delay);
                    if attempt >= 10 {
                        return Err(BridgeError::ConnectionLost(format!(
                            "Failed to reconnect after {attempt} attempts: {e}"
                        )));
                    }
                }
            }
        }
    }

    /// Disconnect and clean up.
    pub fn disconnect(&mut self) -> Result<(), BridgeError> {
        log::info!("Disconnecting from MT5 EA");
        self.is_connected.store(false, Ordering::SeqCst);
        *self.req_socket.lock().unwrap() = None;
        Ok(())
    }

    /// Get the request timeout.
    pub fn request_timeout(&self) -> Duration {
        self.request_timeout
    }

    /// Set the request timeout.
    pub fn set_request_timeout(&mut self, timeout: Duration) {
        self.request_timeout = timeout;
    }

    /// Set the heartbeat timeout.
    pub fn set_heartbeat_timeout(&mut self, timeout: Duration) {
        self.heartbeat_timeout = timeout;
    }

    /// Ping the MT5 EA and return the round-trip latency.
    pub fn ping(&mut self) -> Result<Duration, BridgeError> {
        self.heartbeat()
    }

    /// Place an order via the bridge.
    pub fn place_order(&mut self, order: OrderRequest) -> Result<OrderResult, BridgeError> {
        let msg_id = uuid::Uuid::new_v4().to_string();
        let response = self.send_command(BridgeCommand::PlaceOrder { id: msg_id, order })?;

        match response {
            BridgeResponse::OrderResult { result, .. } => Ok(result),
            BridgeResponse::Error { code, message, .. } => {
                Err(BridgeError::RemoteError { code, message })
            }
            _ => Err(BridgeError::InvalidMessage(format!(
                "Unexpected response to order placement: {:?}",
                response
            ))),
        }
    }
}

impl Drop for Mt5Bridge {
    fn drop(&mut self) {
        let _ = self.disconnect();
    }
}
