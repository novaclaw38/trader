//!
//! Main binary for the MT5↔Rust ZMQ bridge.
//!
//! This is a standalone binary that connects to the MQL5 Expert Advisor
//! running inside the MT5 terminal via ZMQ REQ/REP and PUB/SUB sockets.
//!
//! Usage:
//!   mt5-bridge                                    # default: tcp://127.0.0.1:5555
//!   mt5-bridge --endpoint tcp://127.0.0.1:5560    # custom endpoint
//!   mt5-bridge --subscribe tcp://*:5556           # also subscribe to tick stream
//!   mt5-bridge --ping-interval 30                 # heartbeat every 30s
//!   mt5-bridge --timeout 5000                     # 5s request timeout (ms)
//!

use clap::Parser;
use std::process;
use std::time::Duration;

use mt5_bridge::{Mt5Bridge, TickSubscriber};

/// MT5↔Rust ZMQ Bridge — low-latency trading bridge
#[derive(Parser)]
#[command(
    name = "mt5-bridge",
    version,
    about = "ZeroMQ bridge between Rust and MetaTrader 5"
)]
struct Args {
    /// ZMQ endpoint for the MQL5 EA's REP socket
    #[arg(short, long, default_value = "tcp://127.0.0.1:5555")]
    endpoint: String,

    /// ZMQ endpoint for subscribing to tick PUB stream (optional)
    #[arg(short = 's', long)]
    subscribe: Option<String>,

    /// Heartbeat interval in seconds
    #[arg(short = 'i', long, default_value = "10")]
    heartbeat_interval: u64,

    /// Request timeout in milliseconds
    #[arg(short = 't', long, default_value = "5000")]
    timeout: u64,

    /// Log level (debug, info, warn, error)
    #[arg(short, long, default_value = "info")]
    log_level: String,
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args = Args::parse();

    // Initialize logging
    unsafe {
        std::env::set_var("RUST_LOG", &args.log_level);
    }
    env_logger::init();

    log::info!("=== MT5↔Rust ZMQ Bridge ===");
    log::info!("Endpoint: {}", args.endpoint);
    log::info!(
        "Heartbeat: {}s | Timeout: {}ms",
        args.heartbeat_interval,
        args.timeout
    );

    // Create ZMQ context and bridge
    let ctx = zmq::Context::new();
    let mut bridge = match Mt5Bridge::new(&args.endpoint) {
        Ok(b) => {
            log::info!("Bridge created, attempting connection...");
            b
        }
        Err(e) => {
            log::error!("Failed to create bridge: {e}");
            process::exit(1);
        }
    };

    // Set timeouts
    bridge.set_request_timeout(Duration::from_millis(args.timeout));
    bridge.set_heartbeat_timeout(Duration::from_secs(args.heartbeat_interval + 5));

    // Connect to EA
    match bridge.connect() {
        Ok(_) => log::info!("Connected to MT5 EA"),
        Err(e) => {
            log::error!("Initial connection failed: {e}");
            log::info!("Attempting reconnect with backoff...");
            match bridge.reconnect() {
                Ok(_) => log::info!("Reconnected successfully"),
                Err(e) => {
                    log::error!("Reconnect failed after 10 attempts: {e}");
                    process::exit(1);
                }
            }
        }
    }

    // Start tick subscriber if requested
    let _tick_subscriber = if let Some(sub_endpoint) = &args.subscribe {
        log::info!("Subscribing to tick stream at {}", sub_endpoint);
        match TickSubscriber::new(&ctx, sub_endpoint) {
            Ok(sub) => {
                log::info!("Tick subscriber started (tick count: 0)");
                Some(sub)
            }
            Err(e) => {
                log::error!("Failed to create tick subscriber: {e}");
                None
            }
        }
    } else {
        None
    };

    // Main loop: heartbeat + health monitoring
    let mut last_heartbeat = std::time::Instant::now();
    let heartbeat_interval = Duration::from_secs(args.heartbeat_interval);

    log::info!("Bridge running. Press Ctrl+C to stop.");

    loop {
        // Periodic heartbeat
        if last_heartbeat.elapsed() >= heartbeat_interval {
            match bridge.heartbeat() {
                Ok(elapsed) => {
                    log::debug!("Heartbeat OK: {elapsed:?}");
                    if let Some(sub) = &_tick_subscriber {
                        let ticks = sub.tick_count();
                        let healthy = sub.is_healthy();
                        if ticks > 0 || last_heartbeat.elapsed() > heartbeat_interval * 2 {
                            log::info!("Tick stream: {ticks} ticks, healthy={healthy}");
                        }
                    }
                }
                Err(e) => {
                    log::warn!("Heartbeat failed: {e}");
                    if !bridge.is_healthy() {
                        log::error!("Bridge unhealthy — attempting reconnect");
                        if let Err(reconnect_err) = bridge.reconnect() {
                            log::error!("Reconnect failed: {reconnect_err}");
                        }
                    }
                }
            }
            last_heartbeat = std::time::Instant::now();
        }

        // Brief sleep to avoid busy-wait
        std::thread::sleep(Duration::from_millis(500));

        // Check connection health
        if !bridge.is_connected() {
            log::warn!("Bridge disconnected — attempting reconnect");
            match bridge.reconnect() {
                Ok(_) => log::info!("Reconnected"),
                Err(e) => {
                    log::error!("Reconnect failed: {e}");
                    // Continue trying; don't exit
                }
            }
        }
    }
}
