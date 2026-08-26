//!
//! Integration tests for the MT5 ZMQ bridge.
//!
//! These tests validate:
//! - Basic ping/pong latency (should be < 5ms for localhost)
//! - Command serialization/deserialization integrity
//! - Reconnection behavior after disconnect
//! - Order placement round-trip
//! - Tick subscriber data flow

use std::thread;
use std::time::{Duration, Instant};

mod test_server;

use mt5_bridge::OrderRequest;
use mt5_bridge::{BridgeCommand, BridgeResponse, Mt5Bridge};
use mt5_bridge::{BridgeError, TickSubscriber};

use test_server::{TestServerConfig, spawn_test_server, spawn_test_server_with_config};

/// Test endpoint used across tests
const REP_ENDPOINT: &str = "tcp://127.0.0.1:35555";

#[test]
fn test_ping_latency_under_5ms() {
    let (_server, _ctx) = spawn_test_server();
    thread::sleep(Duration::from_millis(150));

    let mut bridge = Mt5Bridge::new(REP_ENDPOINT);
    assert!(
        bridge.is_ok(),
        "Failed to create bridge: {:?}",
        bridge.err()
    );
    let mut bridge = bridge.unwrap();

    bridge.connect().expect("Failed to connect to test server");

    // Warm up
    let _ = bridge.ping();

    // Measure 10 pings
    let mut max_latency = 0u128;
    for _ in 0..10 {
        let start = Instant::now();
        bridge.ping().expect("Ping failed");
        let elapsed = start.elapsed().as_micros();
        if elapsed > max_latency {
            max_latency = elapsed;
        }
    }

    bridge.disconnect();

    println!("Max ping latency: {:.0} µs", max_latency as f64);
    assert!(
        max_latency < 5_000,
        "Max ping latency {} µs exceeds 5ms threshold",
        max_latency
    );
}

#[test]
fn test_command_roundtrip_account_info() {
    let (_server, _ctx) = spawn_test_server();
    thread::sleep(Duration::from_millis(150));

    let mut bridge = Mt5Bridge::new(REP_ENDPOINT).expect("Failed to create bridge");
    bridge.connect().expect("Failed to connect");

    // Send a GetAccountInfo command
    let result = bridge.send_command(BridgeCommand::GetAccountInfo {
        id: "test-account-info".to_string(),
    });

    assert!(result.is_ok(), "GetAccountInfo failed: {:?}", result.err());
    let response = result.unwrap();

    match response {
        BridgeResponse::AccountInfo { info, .. } => {
            assert_eq!(info.login, 1);
            assert_eq!(info.balance, 10000.0);
            assert_eq!(info.currency, "USD");
        }
        _ => panic!("Expected AccountInfo response, got: {:?}", response),
    }

    bridge.disconnect();
}

#[test]
fn test_reconnect_after_disconnect() {
    let config = TestServerConfig {
        rep_endpoint: "tcp://127.0.0.1:35557".to_string(),
        pub_endpoint: None,
        respond_delay_ms: 0,
        auto_heartbeat: false,
        tick_interval_ms: 0,
    };

    let (_server, _ctx) = spawn_test_server_with_config(config);
    thread::sleep(Duration::from_millis(150));

    let bridge_cfg = "tcp://127.0.0.1:35557";

    let mut bridge = Mt5Bridge::new(bridge_cfg).expect("Failed to create bridge");
    bridge.connect().expect("Initial connect failed");

    // Server 1 — should work
    let ping_result = bridge.ping();
    assert!(ping_result.is_ok(), "Ping to server 1 failed");

    // Drop server
    drop(_server);
    thread::sleep(Duration::from_millis(200));

    // Start new server on same port
    let (_server2, _ctx2) = spawn_test_server_with_config(TestServerConfig {
        rep_endpoint: "tcp://127.0.0.1:35557".to_string(),
        pub_endpoint: None,
        respond_delay_ms: 0,
        auto_heartbeat: false,
        tick_interval_ms: 0,
    });
    thread::sleep(Duration::from_millis(150));

    // Reconnect
    bridge.disconnect();
    bridge.connect().expect("Reconnect failed");

    let ping_result = bridge.ping();
    assert!(ping_result.is_ok(), "Ping after reconnect failed");
}

#[test]
fn test_order_placement_roundtrip() {
    let (_server, _ctx) = spawn_test_server();
    thread::sleep(Duration::from_millis(150));

    let mut bridge = Mt5Bridge::new(REP_ENDPOINT).expect("Failed to create bridge");
    bridge.connect().expect("Failed to connect");

    let order = OrderRequest {
        symbol: "EURUSD".to_string(),
        order_type: "BUY".to_string(),
        volume: 0.1,
        price: None,
        sl: Some(1.0750),
        tp: Some(1.0850),
        deviation: 10,
        magic: 20240826,
        comment: "test-order".to_string(),
    };

    let result = bridge.place_order(order);
    assert!(result.is_ok(), "Order placement failed: {:?}", result.err());

    let order_result = result.unwrap();
    assert!(order_result.success, "Order should have succeeded");
    assert_eq!(order_result.retcode, 0, "Retcode should be 0 (success)");

    bridge.disconnect();
}

#[test]
fn test_tick_subscriber_receives_data() {
    let pub_endpoint = "tcp://*:35559";
    let config = TestServerConfig {
        rep_endpoint: "tcp://127.0.0.1:35558".to_string(),
        pub_endpoint: Some(pub_endpoint.to_string()),
        respond_delay_ms: 0,
        auto_heartbeat: false,
        tick_interval_ms: 10,
    };

    let (_server, ctx) = spawn_test_server_with_config(config);
    thread::sleep(Duration::from_millis(150));

    let mut subscriber =
        TickSubscriber::new(&ctx, "tcp://127.0.0.1:35559").expect("Failed to create subscriber");

    // Subscribe to EURUSD
    subscriber.subscribe("EURUSD");

    // Wait for ticks — PUB/SUB needs time to establish connection
    thread::sleep(Duration::from_millis(500));

    let mut received_count = 0;
    for _ in 0..10 {
        match subscriber.recv_timeout(Duration::from_millis(50)) {
            Some(tick) => {
                assert_eq!(tick.data.symbol, "EURUSD");
                received_count += 1;
            }
            None => break,
        }
    }

    assert!(received_count > 0, "Should have received at least 1 tick");

    let stats = subscriber.stats();
    println!("Received {} ticks", stats.0);

    drop(subscriber);
}

#[test]
fn test_protocol_message_serialization() {
    // Test that BridgeCommand serializes and deserializes correctly
    let cmd = BridgeCommand::Ping {
        id: "test-id-123".to_string(),
    };

    let serialized = serde_json::to_string(&cmd).expect("Failed to serialize");

    // BridgeCommand uses #[serde(tag = "cmd")] so cmd is at top level
    let parsed: serde_json::Value = serde_json::from_str(&serialized).expect("Failed to parse");
    assert_eq!(parsed["cmd"], "PING");
    assert_eq!(parsed["id"], "test-id-123");

    // Test OrderRequest serialization
    let order = OrderRequest {
        symbol: "GBPJPY".to_string(),
        order_type: "SELL".to_string(),
        volume: 0.05,
        price: Some(195.500),
        sl: Some(196.000),
        tp: Some(194.500),
        deviation: 5,
        magic: 123456,
        comment: "protocol-test".to_string(),
    };

    let serialized_order = serde_json::to_string(&order).expect("Failed to serialize order");
    let parsed_order: serde_json::Value =
        serde_json::from_str(&serialized_order).expect("Failed to parse order");
    assert_eq!(parsed_order["symbol"], "GBPJPY");
    assert_eq!(parsed_order["order_type"], "SELL");
    assert_eq!(parsed_order["volume"], 0.05);
}

#[test]
fn test_error_on_unreachable_server() {
    let mut bridge =
        Mt5Bridge::new("tcp://127.0.0.1:39999").expect("Bridge creation should succeed");

    // connect() just sets up the socket; the actual failure happens on send
    let connect_result = bridge.connect();
    if connect_result.is_ok() {
        // If connect succeeded (socket created), the ping will fail
        let result = bridge.ping();
        assert!(result.is_err(), "Should fail to ping unreachable server");
    } else {
        // connect itself failed
    }

    match connect_result {
        Err(BridgeError::ConnectionLost(_)) | Err(BridgeError::ZmqError(_)) => {}
        _ => {
            // If connect succeeded, ping should have failed
            if connect_result.is_ok() {
                // Already asserted ping failed above
            }
        }
    }
}

#[test]
fn test_send_command_ping() {
    let (_server, _ctx) = spawn_test_server();
    thread::sleep(Duration::from_millis(150));

    let mut bridge = Mt5Bridge::new(REP_ENDPOINT).expect("Failed to create bridge");
    bridge.connect().expect("Failed to connect to test server");

    let result = bridge.ping();
    assert!(result.is_ok(), "Ping failed: {:?}", result.err());

    bridge.disconnect();
}

#[test]
fn test_send_command_not_connected() {
    let mut bridge = Mt5Bridge::new(REP_ENDPOINT).expect("Failed to create bridge");
    // Don't connect — should fail
    let result = bridge.send_command(BridgeCommand::Ping {
        id: "test".to_string(),
    });
    assert!(result.is_err());

    match result.unwrap_err() {
        BridgeError::ConnectionLost(_) => {}
        e => panic!("Expected ConnectionLost error, got: {:?}", e),
    }
}
