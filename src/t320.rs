//! t320-client — lightweight MQTT client for the Inrico T320 PTT radio.
//!
//! Connects to the Pi MQTT broker, publishes heartbeats,
//! subscribes to send/notify commands. No Python, no Termux deps.

use rumqttc::{Client, Event, MqttOptions, Packet, QoS};
use serde::Deserialize;
use std::process::Command;
use std::thread;
use std::time::Duration;

const BROKER: &str = "192.168.0.19";
const BROKER_PORT: u16 = 8883;
const CA_CERT_PATH: &str = "/data/data/com.termux/files/home/pi-mqtt.crt";
const CLIENT_ID: &str = "t320";
const HEARTBEAT_SECS: u64 = 30;

const TOPIC_STATUS: &str = "t320/status";
const TOPIC_CMD_SEND: &str = "cmd/t320/sms/send";
const TOPIC_CMD_NOTIFY: &str = "cmd/t320/notify";

#[derive(Deserialize)]
struct SendCmd {
    number: Option<String>,
    message: Option<String>,
}

#[derive(Deserialize)]
struct NotifyCmd {
    title: Option<String>,
    text: Option<String>,
}

fn log(msg: &str) {
    let ts = Command::new("date")
        .arg("+%Y-%m-%d %H:%M:%S")
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();
    eprintln!("{} {}", ts, msg);
}

fn get_battery() -> i32 {
    // Read Android battery level directly from sysfs
    std::fs::read_to_string("/sys/class/power_supply/battery/capacity")
        .ok()
        .and_then(|s| s.trim().parse().ok())
        .unwrap_or(-1)
}

fn get_uptime() -> u64 {
    std::fs::read_to_string("/proc/uptime")
        .ok()
        .and_then(|s| s.split_whitespace().next()?.parse::<f64>().ok())
        .map(|f| f as u64)
        .unwrap_or(0)
}

fn send_sms(number: &str, message: &str) -> bool {
    // Try am start with SMS intent (works without Termux API)
    let uri = format!("sms:{}", number);
    let r = Command::new("am")
        .args([
            "start",
            "-a", "android.intent.action.SENDTO",
            "-d", &uri,
            "--es", "sms_body", message,
            "--ez", "exit_on_sent", "true",
        ])
        .output();

    if let Ok(o) = r {
        if o.status.success() {
            // Wait for activity to start, then press send
            thread::sleep(Duration::from_secs(2));
            // Try to press the send button via keyevent
            let _ = Command::new("input")
                .args(["keyevent", "KEYCODE_ENTER"])
                .output();
            return true;
        }
    }

    // Fallback: try termux-sms-send if available
    let termux_bin = "/data/data/com.termux/files/usr/bin/termux-sms-send";
    if std::path::Path::new(termux_bin).exists() {
        let r = Command::new(termux_bin)
            .args(["-n", number, message])
            .output();
        return matches!(r, Ok(o) if o.status.success());
    }

    false
}

fn show_notification(title: &str, text: &str) {
    // Use Android's built-in notification via am/service call
    // Toast is simplest — visible without Termux API
    let toast_msg = format!("{}: {}", title, text);
    let _ = Command::new("am")
        .args([
            "broadcast",
            "-a", "android.intent.action.SHOW_TOAST",
            "--es", "message", &toast_msg,
        ])
        .output();

    // Also try termux-notification if available
    let termux_bin = "/data/data/com.termux/files/usr/bin/termux-notification";
    if std::path::Path::new(termux_bin).exists() {
        let _ = Command::new(termux_bin)
            .args(["--title", title, "--content", text])
            .output();
    }

    log(&format!("notification: {}: {}", title, text));
}

fn handle_message(topic: &str, payload: &[u8]) {
    let payload_str = String::from_utf8_lossy(payload);

    if topic == TOPIC_CMD_SEND {
        if let Ok(cmd) = serde_json::from_str::<SendCmd>(&payload_str) {
            let number = cmd.number.unwrap_or_default();
            let message = cmd.message.unwrap_or_default();
            if !number.is_empty() && !message.is_empty() {
                let ok = send_sms(&number, &message);
                log(&format!(
                    "SMS {} to {}: {}",
                    if ok { "sent" } else { "FAILED" },
                    number,
                    &message[..message.len().min(50)]
                ));
            }
        }
    } else if topic == TOPIC_CMD_NOTIFY {
        if let Ok(cmd) = serde_json::from_str::<NotifyCmd>(&payload_str) {
            let title = cmd.title.unwrap_or_else(|| "m0usunet".into());
            let text = cmd.text.unwrap_or_default();
            if !text.is_empty() {
                show_notification(&title, &text);
            }
        }
    }
}

fn main() {
    log("t320-client starting");

    loop {
        log(&format!("connecting to {}:{}", BROKER, BROKER_PORT));

        let mut opts = MqttOptions::new(CLIENT_ID, BROKER, BROKER_PORT);
        opts.set_keep_alive(Duration::from_secs(60));
        opts.set_clean_session(true);

        // TLS setup — rumqttc handles the cert parsing internally
        match std::fs::read(CA_CERT_PATH) {
            Ok(ca_bytes) => {
                opts.set_transport(rumqttc::Transport::Tls(rumqttc::TlsConfiguration::Simple {
                    ca: ca_bytes,
                    alpn: None,
                    client_auth: None,
                }));
            }
            Err(e) => {
                log(&format!("can't read CA cert {}: {}, retrying in 10s", CA_CERT_PATH, e));
                thread::sleep(Duration::from_secs(10));
                continue;
            }
        }

        let (client, mut connection) = Client::new(opts, 32);

        // Subscribe
        if let Err(e) = client.subscribe(TOPIC_CMD_SEND, QoS::AtLeastOnce) {
            log(&format!("subscribe error: {}", e));
        }
        if let Err(e) = client.subscribe(TOPIC_CMD_NOTIFY, QoS::AtLeastOnce) {
            log(&format!("subscribe error: {}", e));
        }

        // Heartbeat thread
        let hb_client = client.clone();
        let hb_handle = thread::spawn(move || loop {
            let payload = format!(
                "{{\"battery\":{},\"uptime\":{},\"timestamp\":{}}}",
                get_battery(),
                get_uptime(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs_f64()
            );
            if hb_client
                .publish(TOPIC_STATUS, QoS::AtMostOnce, false, payload.as_bytes())
                .is_err()
            {
                break;
            }
            thread::sleep(Duration::from_secs(HEARTBEAT_SECS));
        });

        log("connected, listening");

        // Event loop
        for event in connection.iter() {
            match event {
                Ok(Event::Incoming(Packet::Publish(publish))) => {
                    handle_message(&publish.topic, &publish.payload);
                }
                Ok(_) => {}
                Err(e) => {
                    log(&format!("connection error: {}, reconnecting in 10s", e));
                    break;
                }
            }
        }

        drop(hb_handle);
        thread::sleep(Duration::from_secs(10));
    }
}
