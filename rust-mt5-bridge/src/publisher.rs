//!
//! ZMQ PUB socket for streaming real-time data.
//!
//! This module is used by the Rust side to publish processed data
//! (e.g., signals, risk alerts) to subscribers. The MQL5 EA side
//! typically acts as the PUB, but this provides the Rust-side PUB
//! for bidirectional streaming patterns.
//!
//! Key features:
//! - Publish-subscribe with topic filtering
//! - Non-blocking sends with HWM limits
//! - Graceful shutdown

use serde::Serialize;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use zmq::{Context, Message, SocketType};

use crate::error::BridgeError;

/// Publisher for streaming data to subscribers.
pub struct Publisher {
    context: Context,
    socket: zmq::Socket,
    endpoint: String,
    shutdown: Arc<AtomicBool>,
}

impl Publisher {
    /// Create a new publisher on the given endpoint.
    ///
    /// # Args
    /// * `context` — ZMQ context
    /// * `endpoint` — bind endpoint (e.g., "tcp://*:5556")
    pub fn new(context: &Context, endpoint: &str) -> Result<Self, BridgeError> {
        let socket = context.socket(SocketType::PUB).map_err(BridgeError::from)?;

        // Set high water mark to prevent unbounded memory growth
        socket.set_sndhwm(1000).map_err(BridgeError::from)?;

        socket.bind(endpoint).map_err(BridgeError::from)?;

        log::info!("Publisher bound to {}", endpoint);

        Ok(Publisher {
            context: context.clone(),
            socket,
            endpoint: endpoint.to_string(),
            shutdown: Arc::new(AtomicBool::new(false)),
        })
    }

    /// Publish a message on a specific topic.
    ///
    /// # Args
    /// * `topic` — topic prefix for subscriber filtering
    /// * `payload` — serializable payload
    pub fn publish<T: Serialize>(&self, topic: &str, payload: &T) -> Result<(), BridgeError> {
        let json = serde_json::to_string(payload)
            .map_err(|e| BridgeError::InvalidMessage(format!("serialize: {e}")))?;

        let full_msg = format!("{topic} {json}");
        self.socket
            .send(full_msg.as_str(), zmq::DONTWAIT)
            .map_err(BridgeError::from)?;

        Ok(())
    }

    /// Publish raw bytes.
    pub fn publish_raw(&self, topic: &str, data: &[u8]) -> Result<(), BridgeError> {
        let mut msg = topic.as_bytes().to_vec();
        msg.extend_from_slice(data);
        self.socket
            .send(&msg, zmq::DONTWAIT)
            .map_err(BridgeError::from)?;
        Ok(())
    }

    /// Shutdown the publisher.
    pub fn shutdown(&self) -> Result<(), BridgeError> {
        self.shutdown.store(true, Ordering::SeqCst);
        Ok(())
    }
}
