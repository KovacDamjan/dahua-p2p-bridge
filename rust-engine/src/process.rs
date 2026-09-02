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
            let original_authority = &output[start..end];
            let original_host = original_authority
                .rsplit(|byte| *byte == b'@')
                .next()
                .unwrap_or(original_authority)
                .split(|byte| *byte == b':')
                .next()
                .unwrap_or(original_authority);
            let ipv4_literal = original_host.iter().all(|byte| byte.is_ascii_digit() || *byte == b'.')
                && original_host.iter().filter(|byte| **byte == b'.').count() == 3;
            let localhost = original_host.eq_ignore_ascii_case(b"localhost");

            if ipv4_literal || localhost {
                output.splice(start..end, authority.as_bytes().iter().copied());
                search_from = start + authority.len();
            } else {
                // Preserve XML namespaces such as www.w3.org and www.onvif.org.
                search_from = end;
            }
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
    mut rx: mpsc::UnboundedReceiver<Vec<u8>>,
    http_rewrite: Option<HttpRewriteConfig>,
    realm_id: u32,
    channels: Arc<Mutex<HashMap<u32, mpsc::UnboundedSender<Vec<u8>>>>>,
    _connection_permit: Option<OwnedSemaphorePermit>,
) {
    if let Some(config) = http_rewrite {
        let mut response = Vec::new();
        while let Some(data) = rx.recv().await {
            response.extend_from_slice(&data);
            if http_response_complete(&response) {
                break;
            }
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
        channels.lock().unwrap().remove(&realm_id);
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

fn http_response_complete(data: &[u8]) -> bool {
    let Some(header_end) = data.windows(4).position(|window| window == b"\r\n\r\n") else {
        return false;
    };
    let headers = String::from_utf8_lossy(&data[..header_end]);
    let content_length = headers.lines().find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("content-length")
            .then(|| value.trim().parse::<usize>().ok())
            .flatten()
    });
    match content_length {
        Some(length) => data.len() >= header_end + 4 + length,
        None => true,
    }
}

/**
 * Read data from the client and send it to the channel
 */
pub async fn process_reader(
    mut reader: tokio::net::tcp::OwnedReadHalf,
    realm_id: u32,
    dh_tx: mpsc::Sender<PTCPEvent>,
    channels: Arc<Mutex<HashMap<u32, mpsc::UnboundedSender<Vec<u8>>>>>,
    persistent_realm: bool,
) {
    let mut buf = [0u8; 4096];

    loop {
        let n = match reader.read(&mut buf).await {
            Ok(n) => {
                if n == 0 {
                    println!("Reader: Socket closed by peer.");
                    if !persistent_realm {
                        let _ = dh_tx.send(PTCPEvent::Disconnect(realm_id)).await;
                        channels.lock().unwrap().remove(&realm_id);
                    } else {
                        println!(
                            "ONVIF client finished sending request; waiting for camera response"
                        );
                    }
                    break;
                }

                n
            }
            Err(e) => {
                println!("Reader: {}", e);
                if !persistent_realm {
                    let _ = dh_tx.send(PTCPEvent::Disconnect(realm_id)).await;
                    channels.lock().unwrap().remove(&realm_id);
                } else {
                    println!("ONVIF request reader closed; waiting for camera response");
                }
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
    channels: Arc<Mutex<HashMap<u32, mpsc::UnboundedSender<Vec<u8>>>>>,
    conn_channels: Arc<Mutex<HashMap<u32, oneshot::Sender<bool>>>>,
    onvif_realm: Arc<Mutex<Option<u32>>>,
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
                    let mut current_onvif_realm = onvif_realm.lock().unwrap();
                    if *current_onvif_realm == Some(realm) {
                        println!(
                            "Camera closed PTCP ONVIF/HTTP realm {realm:08x}; next request will rebind"
                        );
                        *current_onvif_realm = None;
                    }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rewrites_camera_addresses_but_preserves_xml_namespaces() {
        let config = HttpRewriteConfig {
            host: "192.168.1.2".to_string(),
            onvif_port: 16540,
            rtsp_port: 15540,
        };
        let xml = br#"<Envelope xmlns="http://www.w3.org/2003/05/soap-envelope"><XAddr>http://10.0.0.25/onvif/device_service</XAddr><Uri>rtsp://10.0.0.25:554/cam/realmonitor</Uri></Envelope>"#;
        let rewritten = String::from_utf8(replace_url_authorities(xml, &config)).unwrap();

        assert!(rewritten.contains("http://www.w3.org/2003/05/soap-envelope"));
        assert!(rewritten.contains("http://192.168.1.2:16540/onvif/device_service"));
        assert!(rewritten.contains("rtsp://192.168.1.2:15540/cam/realmonitor"));
    }

    #[test]
    fn detects_complete_keep_alive_http_response() {
        assert!(!http_response_complete(
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n1234"
        ));
        assert!(http_response_complete(
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n12345"
        ));
    }
}
