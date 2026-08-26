//!
//! Test server helper — spawns a mock ZMQ REP server to respond to bridge commands.
//!

pub use serde_json::Value;
pub use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
pub use std::thread;
pub use std::time::Duration;
pub use zmq::{Context, Message, SocketType};

/// Configuration for the test server.
pub struct TestServerConfig {
    pub rep_endpoint: String,
    pub pub_endpoint: Option<String>,
    pub respond_delay_ms: u64,
    pub auto_heartbeat: bool,
    pub tick_interval_ms: u64,
}

impl Default for TestServerConfig {
    fn default() -> Self {
        TestServerConfig {
            rep_endpoint: "tcp://127.0.0.1:35555".to_string(),
            pub_endpoint: Some("tcp://127.0.0.1:35556".to_string()),
            respond_delay_ms: 0,
            auto_heartbeat: false,
            tick_interval_ms: 100,
        }
    }
}

/// Handle for a running test server.
pub struct TestServerHandle {
    pub config: TestServerConfig,
    pub shutdown: Arc<AtomicBool>,
    pub handle: Option<thread::JoinHandle<()>>,
    pub pub_handle: Option<thread::JoinHandle<()>>,
}

impl Drop for TestServerHandle {
    fn drop(&mut self) {
        self.shutdown.store(true, Ordering::SeqCst);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
        if let Some(h) = self.pub_handle.take() {
            let _ = h.join();
        }
    }
}

/// Spawn a test server with default config.
pub fn spawn_test_server() -> (TestServerHandle, Context) {
    spawn_test_server_with_config(TestServerConfig::default())
}

/// Spawn a test server with custom config.
pub fn spawn_test_server_with_config(config: TestServerConfig) -> (TestServerHandle, Context) {
    let _ctx = zmq::Context::new();
    _ctx.set_io_threads(2);

    let shutdown = Arc::new(AtomicBool::new(false));
    let shutdown_clone = shutdown.clone();
    let rep_endpoint = config.rep_endpoint.clone();
    let respond_delay = config.respond_delay_ms;

    // --- REP server thread (command/response) ---
    let rep_shutdown = shutdown.clone();
    let handle = thread::spawn(move || {
        let server_ctx = zmq::Context::new();
        let socket = server_ctx
            .socket(SocketType::REP)
            .expect("Failed to create REP socket");
        socket
            .bind(&rep_endpoint)
            .expect("Failed to bind REP socket");

        let mut msg = Message::new();
        while !rep_shutdown.load(Ordering::SeqCst) {
            let rc = socket.recv(&mut msg, zmq::DONTWAIT);

            if rc.is_ok() {
                let request_str = msg.as_str().unwrap_or("").to_string();

                if respond_delay > 0 {
                    thread::sleep(Duration::from_millis(respond_delay));
                }

                let response = respond_to_command(&request_str);
                let _ = socket.send(&response, 0);
            }

            thread::sleep(Duration::from_micros(100));
        }
    });

    // --- PUB server thread (tick streaming) ---
    let pub_handle = if let Some(ref pub_endpoint) = config.pub_endpoint {
        let pub_shutdown = shutdown.clone();
        let pub_endpoint = pub_endpoint.clone();
        let tick_interval = config.tick_interval_ms;
        let auto_hb = config.auto_heartbeat;

        Some(thread::spawn(move || {
            let server_ctx = zmq::Context::new();
            let socket = server_ctx
                .socket(SocketType::PUB)
                .expect("Failed to create PUB socket");
            socket
                .bind(&pub_endpoint)
                .expect("Failed to bind PUB socket");

            let mut tick_count: u64 = 0;
            while !pub_shutdown.load(Ordering::SeqCst) {
                if auto_hb {
                    let hb = serde_json::json!({
                        "type": "HEARTBEAT",
                        "time": std::time::SystemTime::now()
                            .duration_since(std::time::UNIX_EPOCH)
                            .unwrap()
                            .as_secs()
                    });
                    let _ = socket.send(hb.to_string().as_str(), 0);
                }

                let tick = serde_json::json!({
                    "symbol": "EURUSD",
                    "bid": 1.0800 + (tick_count as f64) * 0.0001,
                    "ask": 1.0801 + (tick_count as f64) * 0.0001,
                    "volume": tick_count,
                    "time": chrono::Utc::now().to_rfc3339(),
                });
                let _ = socket.send(tick.to_string().as_str(), 0);

                tick_count += 1;
                thread::sleep(Duration::from_millis(tick_interval));
            }
        }))
    } else {
        None
    };

    (
        TestServerHandle {
            config,
            shutdown,
            handle: Some(handle),
            pub_handle,
        },
        _ctx,
    )
}

/// Respond to a ZMQ command — mirrors MQL5 EA behavior.
fn respond_to_command(request: &str) -> String {
    let parsed: Result<Value, _> = serde_json::from_str(request);
    if parsed.is_err() {
        return r#"{"type":"ERROR","code":-1,"message":"Invalid JSON"}"#.to_string();
    }

    let v = parsed.unwrap();
    let msg_id = v["id"].as_str().unwrap_or("unknown");

    let response = match v["payload"]["cmd"].as_str().unwrap_or("") {
        "PING" => serde_json::json!({
            "type": "PONG",
            "id": msg_id,
            "latency_ms": 0.0,
        }),
        "GET_TICKS" => {
            let count = v["payload"]["count"].as_u64().unwrap_or(10) as usize;
            let mut ticks = Vec::with_capacity(count);
            for i in 0..count {
                ticks.push(serde_json::json!({
                    "symbol": "EURUSD",
                    "bid": 1.0800 + (i as f64) * 0.0001,
                    "ask": 1.0801 + (i as f64) * 0.0001,
                    "volume": i as i64,
                    "time": chrono::Utc::now().to_rfc3339(),
                }));
            }
            serde_json::json!({
                "type": "TICKS",
                "id": msg_id,
                "ticks": ticks,
            })
        }
        "GET_ACCOUNT_INFO" => serde_json::json!({
            "type": "ACCOUNT_INFO",
            "id": msg_id,
            "info": {
                "login": 1,
                "balance": 10000.0,
                "equity": 10000.0,
                "margin": 0.0,
                "margin_free": 10000.0,
                "margin_level": 0.0,
                "assets": 10000.0,
                "liabilities": 0.0,
                "profit": 0.0,
                "currency": "USD",
                "server": "test",
                "account_trade_mode": "DEMO",
                "account_trade_expert": false,
                "trade_allowed": true,
                "min_margin_level": 200.0,
            }
        }),
        "PLACE_ORDER" => serde_json::json!({
            "type": "ORDER_RESULT",
            "id": msg_id,
            "result": {
                "success": true,
                "order_id": 12345,
                "retcode": 0,
                "retcode_description": "Trade executed",
                "executed_price": 1.0800,
                "executed_volume": 0.1,
                "slippage": 0.0,
                "timestamp": "2024-01-01T00:00:00Z"
            }
        }),
        _ => serde_json::json!({
            "type": "ERROR",
            "id": msg_id,
            "code": -1,
            "message": "Unknown command"
        }),
    };

    // Wrap in ResponseEnvelope: {"id":"...","timestamp":"...","payload":{...}}
    let ts = chrono::Utc::now().to_rfc3339();
    serde_json::json!({
        "id": msg_id,
        "timestamp": ts,
        "payload": response,
    })
    .to_string()
}
