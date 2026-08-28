use clap::Parser;
use std::{
    collections::HashMap,
    os::fd::FromRawFd,
    sync::{Arc, Mutex},
};
use tokio::{
    net::{TcpListener, UdpSocket},
    sync::{mpsc, oneshot, Semaphore},
    time::{timeout, Duration},
};

use crate::{
    process::{dh_reader, dh_writer, process_reader, process_writer, HttpRewriteConfig},
    ptcp::{PTCPEvent, PTCPSession},
};

mod process;
mod ptcp;

async fn handle_client(
    client: tokio::net::TcpStream,
    addr: std::net::SocketAddr,
    remote_port: u32,
    service: &'static str,
    dh_tx: mpsc::Sender<PTCPEvent>,
    channels: Arc<Mutex<HashMap<u32, mpsc::Sender<Vec<u8>>>>>,
    conn_channels: Arc<Mutex<HashMap<u32, oneshot::Sender<bool>>>>,
    onvif_slots: Arc<Semaphore>,
    onvif_realm: Arc<Mutex<Option<u32>>>,
    rtsp_local_port: u16,
) {
    println!("Accepted {service} connection from {addr}");
    let connection_permit = if service == "ONVIF/HTTP" {
        println!("Queueing ONVIF/HTTP connection from {addr}");
        match onvif_slots.acquire_owned().await {
            Ok(permit) => Some(permit),
            Err(_) => return,
        }
    } else {
        None
    };
    let persistent_onvif = service == "ONVIF/HTTP";
    // The camera closes an ONVIF realm after each HTTP response. Keep the
    // authenticated P2P relay alive, but bind a fresh realm per request so a
    // following Synology request can never race with the delayed DISC packet.
    let realm_id = rand::random::<u32>();
    let (tx, rx) = mpsc::channel::<Vec<u8>>(128);
    channels.lock().unwrap().insert(realm_id, tx);

    let (conn_tx, conn_rx) = oneshot::channel::<bool>();
    conn_channels.lock().unwrap().insert(realm_id, conn_tx);
    if dh_tx
        .send(PTCPEvent::Connect(realm_id, remote_port))
        .await
        .is_err()
    {
        eprintln!("PTCP writer unavailable");
        channels.lock().unwrap().remove(&realm_id);
        conn_channels.lock().unwrap().remove(&realm_id);
        return;
    }
    match timeout(Duration::from_secs(12), conn_rx).await {
        Ok(Ok(true)) => {
            println!("PTCP {service} realm {realm_id:08x} connected");
            if persistent_onvif {
                *onvif_realm.lock().unwrap() = Some(realm_id);
            }
        }
        _ => {
            println!("PTCP {service} realm {realm_id:08x} bind timed out");
            channels.lock().unwrap().remove(&realm_id);
            conn_channels.lock().unwrap().remove(&realm_id);
            return;
        }
    }

    let local_address = client.local_addr().ok();
    let http_rewrite = if service == "ONVIF/HTTP" {
        local_address.map(|address| HttpRewriteConfig {
            host: address.ip().to_string(),
            onvif_port: address.port(),
            rtsp_port: rtsp_local_port,
        })
    } else {
        None
    };
    let (reader, writer) = client.into_split();
    tokio::spawn(process_reader(
        reader,
        realm_id,
        dh_tx,
        channels,
        connection_permit,
        persistent_onvif,
    ));
    tokio::spawn(process_writer(writer, rx, http_rewrite));
}

#[derive(Parser)]
#[command(about = "Async PTCP tunnel engine for an authenticated Dahua P2P session")]
struct Cli {
    #[arg(long)]
    udp_fd: i32,
    #[arg(long)]
    listener_fd: i32,
    #[arg(long)]
    http_listener_fd: i32,
    #[arg(long, default_value_t = 554)]
    remote_port: u32,
    #[arg(long)]
    rtsp_public_port: Option<u16>,
    #[arg(long)]
    session_sent: u32,
    #[arg(long)]
    session_recv: u32,
    #[arg(long)]
    session_count: u32,
    #[arg(long)]
    session_id: u32,
    #[arg(long)]
    session_rmid: u32,
}

#[tokio::main]
async fn main() {
    let args = Cli::parse();

    let udp_std = unsafe { std::net::UdpSocket::from_raw_fd(args.udp_fd) };
    udp_std.set_nonblocking(true).expect("set UDP nonblocking");
    let socket = UdpSocket::from_std(udp_std).expect("adopt UDP socket");

    let listener_std = unsafe { std::net::TcpListener::from_raw_fd(args.listener_fd) };
    listener_std
        .set_nonblocking(true)
        .expect("set listener nonblocking");
    let listener = TcpListener::from_std(listener_std).expect("adopt TCP listener");
    let rtsp_local_port = args
        .rtsp_public_port
        .unwrap_or_else(|| listener.local_addr().expect("RTSP listener address").port());

    let http_listener_std = unsafe { std::net::TcpListener::from_raw_fd(args.http_listener_fd) };
    http_listener_std
        .set_nonblocking(true)
        .expect("set HTTP listener nonblocking");
    let http_listener = TcpListener::from_std(http_listener_std).expect("adopt HTTP listener");

    let session = PTCPSession::from_state(
        args.session_sent,
        args.session_recv,
        args.session_count,
        args.session_id,
        args.session_rmid,
    );
    let (dh_tx, dh_rx) = mpsc::channel::<PTCPEvent>(128);
    let session = Arc::new(Mutex::new(session));
    let channels = Arc::new(Mutex::new(HashMap::<u32, mpsc::Sender<Vec<u8>>>::new()));
    let conn_channels = Arc::new(Mutex::new(HashMap::<u32, oneshot::Sender<bool>>::new()));
    let onvif_slots = Arc::new(Semaphore::new(1));
    let onvif_realm = Arc::new(Mutex::new(None::<u32>));

    let socket = Arc::new(socket);
    tokio::spawn(dh_writer(
        session.clone(),
        socket.clone(),
        dh_rx,
    ));
    tokio::spawn(dh_reader(
        session.clone(),
        socket.clone(),
        channels.clone(),
        conn_channels.clone(),
        onvif_realm.clone(),
    ));

    let heartbeat_tx = dh_tx.clone();
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(5)).await;
            if heartbeat_tx.send(PTCPEvent::Heartbeat).await.is_err() {
                break;
            }
        }
    });

    println!("Rust PTCP engine adopted authenticated session");
    println!("Ready to connect!");

    loop {
        let (client, addr, remote_port, service) = tokio::select! {
            result = listener.accept() => match result {
                Ok((client, addr)) => (client, addr, args.remote_port, "RTSP"),
                Err(error) => {
                    eprintln!("RTSP accept failed: {error}");
                    continue;
                }
            },
            result = http_listener.accept() => match result {
                Ok((client, addr)) => (client, addr, 80, "ONVIF/HTTP"),
                Err(error) => {
                    eprintln!("ONVIF/HTTP accept failed: {error}");
                    continue;
                }
            },
        };
        tokio::spawn(handle_client(
            client,
            addr,
            remote_port,
            service,
            dh_tx.clone(),
            channels.clone(),
            conn_channels.clone(),
            onvif_slots.clone(),
            onvif_realm.clone(),
            rtsp_local_port,
        ));
    }
}
