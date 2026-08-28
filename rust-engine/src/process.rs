use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::UdpSocket,
    sync::{mpsc, oneshot, OwnedSemaphorePermit},
};

use crate::ptcp::{PTCPBody, PTCPEvent, PTCPPayload, PTCPSession, PTCP};

pub struct HttpRewriteConfig {
    pub host: String,
    pub onvif_port: u16,
    pub rtsp_port: u16,
}

fn replace_url_authorities(data: &[u8], config: &HttpRewriteConfig) -> Vec<u8> {
    let mut output = data.to_vec();
    for (prefix, authority) in [
        (b"http://".as_slice(), format!("{}:{}", config.host, config.onvif_port)),
        (b"rtsp://".as_slice(), format!("{}:{}", config.host, config.rtsp_port)),
    ] {
        let mut search_from = 0;
        while search_from + prefix.len() < output.len() {
            let Some(relative) = output[search_from..]
                .windows(prefix.len())
                .position(|window| window.eq_ignore_ascii_case(prefix))
            else {
                break;
            };
            let start = search_from + relative + prefix.len();
            let end = output[start..]
                .iter()
                .position(|byte| matches!(*byte, b'/' | b' ' | b'\r' | b'\n' | b'"' | b'\'' | b'<' | b'>'))
                .map(|offset| start + offset)
                .unwrap_or(output.len());
            output.splice(start..end, authority.as_bytes().iter().copied());
            search_from = start + authority.len();
        }
    }
    output
}

fn rewrite_http_response(data: &[u8], config: &HttpRewriteConfig) -> Vec<u8> {
    let Some(header_end) = data.windows(4).position(|window| window == b"\r\n\r\n") else {
        return replace_url_authorities(data, config);
    };
    let body = replace_url_authorities(&data[header_end + 4..], config);
    let header_text = String::from_utf8_lossy(&data[..header_end]);
    let mut header_lines = Vec::new();
    let mut found_length = false;
    for line in header_text.split("\r\n") {
        if line.to_ascii_lowercase().starts_with("content-length:") {
            header_lines.push(format!("Content-Length: {}", body.len()));
            found_length = true;
        } else {
            header_lines.push(line.to_string());
        }
    }
    if !found_length {
        header_lines.push(format!("Content-Length: {}", body.len()));
    }
    let mut output = header_lines.join("\r\n").into_bytes();
    output.extend_from_slice(b"\r\n\r\n");
    output.extend_from_slice(&body);
    output
}

/**
 * Read data from the channel and write it back to the client
 */
pub async fn process_writer(
    mut writer: tokio::net::tcp::OwnedWriteHalf,
    mut rx: mpsc::Receiver<Vec<u8>>,
    http_rewrite: Option<HttpRewriteConfig>,
) {
    if let Some(config) = http_rewrite {
        let mut response = Vec::new();
        while let Some(data) = rx.recv().await {
            response.extend_from_slice(&data);
        }
        if !response.is_empty() {
            let rewritten = rewrite_http_response(&response, &config);
            println!(
                "Rewrote ONVIF response URLs to {}:{} ({} bytes)",
                config.host,
                config.onvif_port,
                rewritten.len()
            );
            let _ = writer.write_all(&rewritten).await;
        }
        return;
    }
    loop {
        let Some(data) = rx.recv().await else {
            break;
        };
        if writer.write_all(&data).await.is_err() {
            println!("Writer: Socket closed by peer.");
            break;
        }
    }
}

/**
 * Read data from the client and send it to the channel
 */
pub async fn process_reader(
    mut reader: tokio::net::tcp::OwnedReadHalf,
    realm_id: u32,
    dh_tx: mpsc::Sender<PTCPEvent>,
    channels: Arc<Mutex<HashMap<u32, mpsc::Sender<Vec<u8>>>>>,
    _connection_permit: Option<OwnedSemaphorePermit>,
) {
    let mut buf = [0u8; 4096];

    loop {
        let n = match reader.read(&mut buf).await {
            Ok(n) => {
                if n == 0 {
                    println!("Reader: Socket closed by peer.");
                    let _ = dh_tx.send(PTCPEvent::Disconnect(realm_id)).await;
                    channels.lock().unwrap().remove(&realm_id);
                    break;
                }

                n
            }
            Err(e) => {
                println!("Reader: {}", e);
                let _ = dh_tx.send(PTCPEvent::Disconnect(realm_id)).await;
                channels.lock().unwrap().remove(&realm_id);
                break;
            }
        };

        dh_tx
            .send(PTCPEvent::Data(realm_id, buf[0..n].to_vec()))
            .await
            .unwrap();
    }
}

/**
* Read data from client and send it to devices
*/
pub async fn dh_writer(
    session: Arc<Mutex<PTCPSession>>,
    socket: Arc<UdpSocket>,
    mut dh_rx: mpsc::Receiver<PTCPEvent>,
) {
    loop {
        let ev = dh_rx.recv().await.unwrap();

        match ev {
            PTCPEvent::Heartbeat => {
                let p = session.lock().unwrap().send(PTCPBody::Heartbeat);
                socket.ptcp_request(p).await;
            }
            PTCPEvent::Connect(realm, remote_port) => {
                let p = session
                    .lock()
                    .unwrap()
                    .send(PTCPBody::Bind(realm, remote_port));
                socket.ptcp_request(p).await;
            }
            PTCPEvent::Disconnect(realm) => {
                let p = session
                    .lock()
                    .unwrap()
                    .send(PTCPBody::Status(realm, "DISC".to_string()));
                socket.ptcp_request(p).await;
            }
            PTCPEvent::Data(realm, data) => {
                let p = session
                    .lock()
                    .unwrap()
                    .send(PTCPBody::Payload(PTCPPayload { realm, data }));
                socket.ptcp_request(p).await;
            }
        }
    }
}

/**
 * Read data from devices and send it to clients
 */
pub async fn dh_reader(
    session: Arc<Mutex<PTCPSession>>,
    socket: Arc<UdpSocket>,
    channels: Arc<Mutex<HashMap<u32, mpsc::Sender<Vec<u8>>>>>,
    conn_channels: Arc<Mutex<HashMap<u32, oneshot::Sender<bool>>>>,
) {
    loop {
        let packet = socket.ptcp_read().await;
        let should_ack = !matches!(&packet.body, PTCPBody::Empty);
        let packets = session.lock().unwrap().recv(packet);

        if should_ack {
            let p = session.lock().unwrap().send(PTCPBody::Empty);
            socket.ptcp_request(p).await;
        }

        for packet in packets {
            match packet.body {
            PTCPBody::Status(realm, status) => {
                if status == "CONN" {
                    if let Some(sender) = conn_channels.lock().unwrap().remove(&realm) {
                        let _ = sender.send(true);
                    }
                } else if status == "DISC" {
                    channels.lock().unwrap().remove(&realm);
                    conn_channels.lock().unwrap().remove(&realm);
                }
            }
            PTCPBody::Payload(p) => {
                let tx = channels.lock().unwrap().get(&p.realm).cloned();
                if let Some(tx) = tx {
                    if tx.send(p.data).await.is_err() {
                        println!("Realm {:08x} unavailable", p.realm);
                    }
                }
            }
            _ => {}
            }
        }
    }
}
