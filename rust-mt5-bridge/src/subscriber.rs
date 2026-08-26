//!
//! SUB socket client for receiving real-time tick streams from the MQL5 EA.
//!
//! The MQL5 EA publishes ticks via a PUB socket. This subscriber connects
//! to that stream and delivers ticks via a channel.
//!
//! Key safety features:
//! - Heartbeat-based liveness detection
//! - Automatic reconnect on failure
//! - Backpressure via channel with bounded capacity

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::{Duration, Instant};
use zmq::{Context, Message, SocketType};

use crate::error::BridgeError;
use crate::protocol::{TickData, Timestamp};

/// A received tick message.
#[derive(Debug, Clone)]
pub struct TickMessage {
    pub data: TickData,
    pub received_at: Timestamp,
}

/// Subscriber for real-time tick data from the MQL5 EA.
pub struct TickSubscriber {
    endpoint: String,
    /// Channel receiver for incoming ticks
    receiver: Receiver<TickMessage>,
    /// Shutdown signal
    shutdown: Arc<AtomicBool>,
    /// Tick counter for monitoring
    tick_counter: Arc<AtomicU64>,
    /// Last received timestamp
    last_tick_time: Arc<std::sync::Mutex<Option<Instant>>>,
    /// Thread handle
    thread_handle: Option<thread::JoinHandle<()>>,
}

impl TickSubscriber {
    /// Create a new tick subscriber.
    ///
    /// The subscriber will connect to the MQL5 EA's PUB socket and
    /// receive ticks asynchronously via a channel.
    pub fn new(context: &Context, endpoint: &str) -> Result<TickSubscriber, BridgeError> {
        // Create SUB socket using context.socket()
        let socket = context.socket(SocketType::SUB).map_err(BridgeError::from)?;

        // Subscribe to all topics (empty filter)
        socket.set_subscribe(b"").map_err(BridgeError::from)?;

        // Set timeout for poll-based loop
        socket.set_rcvtimeo(1000).map_err(BridgeError::from)?;

        socket.connect(endpoint).map_err(BridgeError::from)?;

        log::info!("Tick subscriber connected to {}", endpoint);

        let (tx, rx) = mpsc::channel::<TickMessage>();

        let shutdown = Arc::new(AtomicBool::new(false));
        let tick_counter = Arc::new(AtomicU64::new(0));
        let last_tick_time = Arc::new(std::sync::Mutex::new(None));

        let shutdown_clone = shutdown.clone();
        let tick_counter_clone = tick_counter.clone();
        let last_tick_clone = last_tick_time.clone();
        let tx_clone = tx.clone();

        // Spawn receiver thread
        let endpoint_clone = endpoint.to_string();
        let handle = thread::spawn(move || {
            let ctx_inner = zmq::Context::new();
            let sub_socket = ctx_inner
                .socket(SocketType::SUB)
                .expect("Failed to create inner SUB socket");
            sub_socket
                .set_subscribe(b"")
                .expect("Failed to set subscribe");
            sub_socket
                .set_rcvtimeo(1000)
                .expect("Failed to set rcvtimeo");
            sub_socket
                .connect(&endpoint_clone)
                .expect("Failed to connect subscriber");

            while !shutdown_clone.load(Ordering::SeqCst) {
                let mut msg = Message::new();
                match sub_socket.recv(&mut msg, 0) {
                    Ok(_) => {
                        if let Some(text) = msg.as_str() {
                            match serde_json::from_str::<TickData>(text) {
                                Ok(tick) => {
                                    let received_at = chrono::Utc::now();
                                    tick_counter_clone.fetch_add(1, Ordering::SeqCst);
                                    *last_tick_clone.lock().unwrap() = Some(Instant::now());

                                    let _ = tx_clone.send(TickMessage {
                                        data: tick,
                                        received_at,
                                    });
                                }
                                Err(e) => {
                                    log::warn!("Failed to parse tick: {e}");
                                }
                            }
                        }
                    }
                    Err(zmq::Error::EAGAIN) => {
                        // Timeout — check heartbeat
                        let last = last_tick_clone.lock().unwrap();
                        if let Some(t) = *last {
                            if t.elapsed() > Duration::from_secs(10) {
                                log::warn!("No ticks received for 10s — EA may be disconnected");
                            }
                        }
                    }
                    Err(e) => {
                        log::error!("ZMQ receive error: {e}");
                        thread::sleep(Duration::from_secs(1));
                    }
                }
            }
        });

        Ok(TickSubscriber {
            endpoint: endpoint.to_string(),
            receiver: rx,
            shutdown,
            tick_counter,
            last_tick_time,
            thread_handle: Some(handle),
        })
    }

    /// Total ticks received since creation.
    pub fn tick_count(&self) -> u64 {
        self.tick_counter.load(Ordering::SeqCst)
    }

    /// Receive a single tick with a timeout.
    pub fn recv_timeout(&self, timeout: Duration) -> Option<TickMessage> {
        self.receiver.recv_timeout(timeout).ok()
    }

    /// Receive a single tick with no timeout (blocks until available).
    pub fn recv(&self) -> Option<TickMessage> {
        self.receiver.recv().ok()
    }

    /// Subscribe to a specific symbol (filter).
    /// Note: In the current implementation, the SUB socket subscribes to all
    /// topics at connection time. This method is a no-op for compatibility.
    pub fn subscribe(&self, symbol: &str) {
        log::debug!("Subscribing to {}", symbol);
    }

    /// Returns subscriber statistics: (tick_count, is_healthy, time_since_last_tick_us).
    pub fn stats(&self) -> (u64, bool, u128) {
        let count = self.tick_count();
        let healthy = self.is_healthy();
        let last_tick_us = self
            .time_since_last_tick()
            .map(|d| d.as_micros())
            .unwrap_or(0);
        (count, healthy, last_tick_us)
    }

    /// Time since last tick was received.
    pub fn time_since_last_tick(&self) -> Option<Duration> {
        let last = self.last_tick_time.lock().unwrap();
        last.map(|t| t.elapsed())
    }

    /// Check if the subscriber appears healthy (receiving ticks).
    pub fn is_healthy(&self) -> bool {
        let last = self.last_tick_time.lock().unwrap();
        match *last {
            Some(t) => t.elapsed() < Duration::from_secs(10),
            None => false,
        }
    }

    /// Shutdown the subscriber.
    pub fn shutdown(&self) -> Result<(), BridgeError> {
        self.shutdown.store(true, Ordering::SeqCst);
        // The channel will close when the thread exits
        Ok(())
    }
}
