use async_trait::async_trait;
use std::{
    cmp,
    collections::{HashMap, VecDeque},
    sync::OnceLock,
};
use tokio::net::UdpSocket;
use tokio::time::{sleep, timeout, Duration, Instant};

pub enum PTCPEvent {
    Heartbeat,
    Connect(u32, u32),
    Disconnect(u32),
    Data(u32, Vec<u8>),
}

#[derive(Clone)]
pub struct PTCPPayload {
    pub realm: u32,
    pub data: Vec<u8>,
}

#[derive(Clone)]
pub enum PTCPBody {
    Sync,
    Command(Vec<u8>),
    Payload(PTCPPayload),
    Bind(u32, u32),
    Status(u32, String),
    Heartbeat,
    Empty,
}

#[derive(Clone)]
pub struct PTCPPacket {
    sent: u32,
    recv: u32,
    pid: u32,
    lmid: u32,
    rmid: u32,
    pub body: PTCPBody,
}

impl PTCPPayload {
    fn parse(data: &[u8]) -> PTCPPayload {
        assert!(data.len() >= 12, "Invalid payload");
        assert_eq!(data[0], 0x10, "Invalid header");

        // first 4 bytes it header
        let header = u32::from_be_bytes([data[0], data[1], data[2], data[3]]);
        let length = header & 0xFFFF;
        let realm = u32::from_be_bytes([data[4], data[5], data[6], data[7]]);
        let padding = u32::from_be_bytes([data[8], data[9], data[10], data[11]]);
        let data = data[12..].to_vec();

        assert_eq!(padding, 0, "Invalid padding");
        assert_eq!(length, data.len() as u32, "Invalid length");

        PTCPPayload { realm, data }
    }

    fn serialize(&self) -> Vec<u8> {
        let length = self.data.len() as u32;
        let header = 0x10000000 | length;
        let header = header.to_be_bytes();
        let realm = self.realm.to_be_bytes();
        let padding = 0u32.to_be_bytes();

        [
            header.to_vec(),
            realm.to_vec(),
            padding.to_vec(),
            self.data.clone(),
        ]
        .concat()
    }
}

impl std::fmt::Debug for PTCPPayload {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "length: {}, realm: 0x{:08x}, data: [{}{}]",
            self.data.len(),
            self.realm,
            self.data[0..cmp::min(self.data.len(), 16)]
                .iter()
                .map(|b| format!("{:02x}", b))
                .collect::<Vec<_>>()
                .join(" "),
            if self.data.len() > 16 { " ..." } else { "" },
        )
    }
}

impl std::fmt::Debug for PTCPBody {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            PTCPBody::Sync => write!(f, "Sync"),
            PTCPBody::Command(data) => write!(
                f,
                "Command([{}])",
                data.iter()
                    .map(|b| format!("{:02x}", b))
                    .collect::<Vec<_>>()
                    .join(" ")
            ),
            PTCPBody::Payload(payload) => write!(f, "{:?}", payload),
            PTCPBody::Bind(realm, port) => {
                write!(f, "Bind {{ realm: 0x{:08x}, port: {} }}", realm, port)
            }
            PTCPBody::Status(realm, status) => {
                write!(f, "Status {{ realm: 0x{:08x}, status: {} }}", realm, status)
            }
            PTCPBody::Heartbeat => write!(f, "Heartbeat"),
            PTCPBody::Empty => write!(f, "Empty"),
        }
    }
}

impl std::fmt::Debug for PTCPPacket {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "PTCPPacket {{ sent: {}, recv: {}, pid: 0x{:08x}, lmid: 0x{:08x}, rmid: 0x{:08x}, body: {:?} }}",
            self.sent, self.recv, self.pid, self.lmid, self.rmid, self.body
        )
    }
}

impl PTCPBody {
    fn parse(data: &[u8]) -> PTCPBody {
        if data.len() == 0 {
            return PTCPBody::Empty;
        }

        assert!(data.len() >= 4, "Invalid body");

        match data[0] {
            0x00 => PTCPBody::Sync,
            0x10 => PTCPBody::Payload(PTCPPayload::parse(data)),
            0x11 => PTCPBody::Bind(
                u32::from_be_bytes([data[4], data[5], data[6], data[7]]),
                u32::from_be_bytes([data[12], data[13], data[14], data[15]]),
            ),
            0x12 => {
                let realm = u32::from_be_bytes([data[4], data[5], data[6], data[7]]);
                let status = String::from_utf8_lossy(&data[12..]).to_string();
                PTCPBody::Status(realm, status)
            }
            0x13 => PTCPBody::Heartbeat,
            _ => PTCPBody::Command(data.to_vec()),
        }
    }

    fn serialize(&self) -> Vec<u8> {
        match self {
            PTCPBody::Sync => b"\x00\x03\x01\x00".to_vec(),
            PTCPBody::Command(data) => data.to_vec(),
            PTCPBody::Payload(payload) => payload.serialize(),
            PTCPBody::Bind(realm, port) => [
                b"\x11\x00\x00\x00".to_vec(),
                realm.to_be_bytes().to_vec(),
                b"\x00\x00\x00\x00".to_vec(),
                port.to_be_bytes().to_vec(),
                b"\x7f\x00\x00\x01".to_vec(),
            ]
            .concat(),
            PTCPBody::Status(realm, status) => [
                b"\x12\x00\x00\x00".to_vec(),
                realm.to_be_bytes().to_vec(),
                b"\x00\x00\x00\x00".to_vec(),
                status.as_bytes().to_vec(),
            ]
            .concat(),
            PTCPBody::Heartbeat => b"\x13\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00".to_vec(),
            PTCPBody::Empty => Vec::new(),
        }
    }

    fn len(&self) -> usize {
        match self {
            PTCPBody::Sync => 4,
            PTCPBody::Command(data) => data.len(),
            PTCPBody::Payload(payload) => payload.data.len() + 12,
            PTCPBody::Bind(_, _) => 20,
            PTCPBody::Status(_, status) => status.len() + 12,
            PTCPBody::Heartbeat => 12,
            PTCPBody::Empty => 0,
        }
    }
}

impl PTCPPacket {
    fn parse(data: &[u8]) -> PTCPPacket {
        assert!(data.len() >= 24, "Invalid packet");

        let magic = &data[0..4];

        assert_eq!(magic, b"PTCP", "Invalid magic");

        let sent = u32::from_be_bytes([data[4], data[5], data[6], data[7]]);
        let recv = u32::from_be_bytes([data[8], data[9], data[10], data[11]]);
        let pid = u32::from_be_bytes([data[12], data[13], data[14], data[15]]);
        let lmid = u32::from_be_bytes([data[16], data[17], data[18], data[19]]);
        let rmid = u32::from_be_bytes([data[20], data[21], data[22], data[23]]);
        let body = PTCPBody::parse(&data[24..]);

        let packet = PTCPPacket {
            sent,
            recv,
            pid,
            lmid,
            rmid,
            body,
        };

        packet
    }

    fn serialize(&self) -> Vec<u8> {
        [
            b"PTCP".to_vec(),
            self.sent.to_be_bytes().to_vec(),
            self.recv.to_be_bytes().to_vec(),
            self.pid.to_be_bytes().to_vec(),
            self.lmid.to_be_bytes().to_vec(),
            self.rmid.to_be_bytes().to_vec(),
            self.body.serialize(),
        ]
        .concat()
    }

    fn try_print_data(&self) {
        if let PTCPBody::Payload(p) = &self.body {
            if p.data.len() > 4 && p.data.iter().all(|b| *b < 0x80) {
                println!("{}", String::from_utf8_lossy(&p.data));
            }
        }
    }
}

pub struct PTCPSession {
    sent: u32,
    recv: u32,
    count: u32,
    id: u32,
    rmid: u32,
    pending: HashMap<u32, PTCPPacket>,
    gap_packets: usize,
    sent_window: VecDeque<TrackedPacket>,
}

struct TrackedPacket {
    packet: PTCPPacket,
    last_sent: Instant,
    retries: u8,
}

const GAP_PACKET_LIMIT: usize = 32;
const RETRANSMIT_AFTER: Duration = Duration::from_millis(600);
const MAX_RETRANSMITS: u8 = 8;
const MAX_SENT_WINDOW: usize = 2048;

impl PTCPSession {
    pub fn new() -> PTCPSession {
        PTCPSession {
            sent: 0,
            recv: 0,
            count: 0,
            id: 0,
            rmid: 0,
            pending: HashMap::new(),
            gap_packets: 0,
            sent_window: VecDeque::new(),
        }
    }

    pub fn from_state(sent: u32, recv: u32, count: u32, id: u32, rmid: u32) -> PTCPSession {
        PTCPSession {
            sent,
            recv,
            count,
            id,
            rmid,
            pending: HashMap::new(),
            gap_packets: 0,
            sent_window: VecDeque::new(),
        }
    }

    pub fn send(&mut self, body: PTCPBody) -> PTCPPacket {
        let sent = self.sent;
        let recv = self.recv;
        let pid = match &body {
            PTCPBody::Sync => 0x0002FFFF,
            _ => 0x0000FFFFu32.wrapping_sub(self.count),
        };
        let lmid = self.id;
        let rmid = self.rmid;

        /*
         * Update counters
         */
        self.sent = self.sent.wrapping_add(body.len() as u32);
        self.id = self.id.wrapping_add(1);
        self.count = self.count.wrapping_add(match &body {
            PTCPBody::Sync => 0,
            PTCPBody::Empty => 0,
            _ => 1,
        });

        let packet = PTCPPacket {
            sent,
            recv,
            pid,
            lmid,
            rmid,
            body,
        };
        if packet.body.len() > 0 {
            if self.sent_window.len() >= MAX_SENT_WINDOW {
                self.sent_window.pop_front();
                eprintln!("PTCP send window full; discarded oldest tracked packet");
            }
            self.sent_window.push_back(TrackedPacket {
                packet: packet.clone(),
                last_sent: Instant::now(),
                retries: 0,
            });
        }
        packet
    }

    pub fn recv(&mut self, packet: PTCPPacket) -> Vec<PTCPPacket> {
        let packet_start = packet.sent;
        let packet_end = packet_start.wrapping_add(packet.body.len() as u32);
        self.rmid = packet.lmid;
        self.acknowledge_sent(packet.recv);

        if packet.body.len() == 0 {
            return Vec::new();
        }
        // PTCP byte counters are wrapping u32 sequence numbers.  A negative or
        // zero signed distance means that this datagram has already been
        // acknowledged.  Plain integer comparisons fail after counter wrap.
        if !sequence_after(packet_end, self.recv) {
            if packet_debug_enabled() {
                eprintln!(
                    "PTCP duplicate/old packet: bytes {}..{} already received through {}",
                    packet_start, packet_end, self.recv
                );
            }
            return Vec::new();
        }

        if packet_start == self.recv {
            self.pending.insert(packet_start, packet);
            return self.drain_contiguous();
        }

        if self.pending.contains_key(&packet_start) {
            return Vec::new();
        }
        let is_recovery_command = packet.pid & 0xFF00_0000 == 0x0100_0000;
        self.pending.insert(packet_start, packet);
        self.gap_packets += 1;

        // Acknowledge only the last contiguous byte while the gap is short so
        // the camera can retransmit it.  Advance after sustained loss to avoid
        // freezing the complete RTSP session forever.
        if is_recovery_command || self.gap_packets >= GAP_PACKET_LIMIT {
            let next_start = self
                .pending
                .keys()
                .copied()
                .filter(|start| sequence_after(*start, self.recv))
                .min_by_key(|start| start.wrapping_sub(self.recv));
            let Some(next_start) = next_start else {
                return Vec::new();
            };
            eprintln!(
                "PTCP receive gap persisted for {} packets; skipping bytes {}..{}",
                self.gap_packets, self.recv, next_start
            );
            self.recv = next_start;
            return self.drain_contiguous();
        }

        if self.gap_packets == 1 {
            eprintln!(
                "PTCP receive gap at byte {}; waiting for camera retransmission",
                self.recv
            );
        }
        Vec::new()
    }

    fn drain_contiguous(&mut self) -> Vec<PTCPPacket> {
        let mut ready = Vec::new();
        while let Some(packet) = self.pending.remove(&self.recv) {
            self.recv = self.recv.wrapping_add(packet.body.len() as u32);
            ready.push(packet);
        }
        self.gap_packets = if self.pending.is_empty() { 0 } else { 1 };
        ready
    }

    fn acknowledge_sent(&mut self, remote_recv: u32) {
        while let Some(tracked) = self.sent_window.front() {
            let end = tracked
                .packet
                .sent
                .wrapping_add(tracked.packet.body.len() as u32);
            if end == remote_recv || sequence_after(remote_recv, end) {
                self.sent_window.pop_front();
            } else {
                break;
            }
        }
    }

    pub fn due_retransmissions(&mut self) -> Vec<PTCPPacket> {
        let now = Instant::now();
        let mut due = Vec::new();
        for tracked in &mut self.sent_window {
            if tracked.retries < MAX_RETRANSMITS
                && now.duration_since(tracked.last_sent) >= RETRANSMIT_AFTER
            {
                tracked.last_sent = now;
                tracked.retries += 1;
                due.push(tracked.packet.clone());
            }
        }
        due
    }
}

fn sequence_after(value: u32, reference: u32) -> bool {
    (value.wrapping_sub(reference) as i32) > 0
}

fn packet_debug_enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| {
        std::env::var("P2P_PACKET_DEBUG")
            .map(|value| matches!(value.to_ascii_lowercase().as_str(), "1" | "true" | "yes"))
            .unwrap_or(false)
    })
}

fn idle_reconnect_seconds() -> u64 {
    static SECONDS: OnceLock<u64> = OnceLock::new();
    *SECONDS.get_or_init(|| {
        std::env::var("P2P_IDLE_RECONNECT_SECONDS")
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(0)
    })
}

#[async_trait]
pub trait PTCP {
    async fn ptcp_request(&self, packet: PTCPPacket);
    async fn ptcp_read(&self) -> PTCPPacket;
}

#[async_trait]
impl PTCP for UdpSocket {
    async fn ptcp_request(&self, packet: PTCPPacket) {
        let peer = restart_on_socket_error(self.peer_addr(), "peer lookup");
        let log_packet = packet_debug_enabled()
            || !matches!(&packet.body, PTCPBody::Payload(_) | PTCPBody::Empty);
        if log_packet {
            println!(">>> {}", peer);
            println!("{:?}", packet);
            packet.try_print_data();
            println!("---");
        }

        let packet = packet.serialize();
        for attempt in 1..=4 {
            match self.send(&packet).await {
                Ok(_) => return,
                Err(error) if error.kind() == std::io::ErrorKind::ConnectionRefused => {
                    eprintln!(
                        "PTCP send received transient Connection refused (attempt {}/4)",
                        attempt
                    );
                    sleep(Duration::from_millis(150)).await;
                }
                Err(error) => {
                    restart_on_socket_error::<usize>(Err(error), "send");
                }
            }
        }
        eprintln!("PTCP send failed after transient-error retries; requesting full P2P reconnect");
        std::process::exit(75);
    }

    async fn ptcp_read(&self) -> PTCPPacket {
        let peer = restart_on_socket_error(self.peer_addr(), "peer lookup");
        if packet_debug_enabled() {
            println!("### {}", peer);
        }

        let mut buf = [0u8; 4096];
        let recovery_started = Instant::now();
        let idle_limit = idle_reconnect_seconds();
        let n = loop {
            match timeout(Duration::from_secs(15), self.recv(&mut buf)).await {
                Ok(Ok(n)) => break n,
                Ok(Err(error))
                    if error.kind() == std::io::ErrorKind::ConnectionRefused
                        && recovery_started.elapsed() < Duration::from_secs(15) =>
                {
                    eprintln!(
                        "PTCP receive got transient Connection refused; keeping the active RTSP session"
                    );
                    sleep(Duration::from_millis(150)).await;
                }
                Ok(Err(error)) => {
                    restart_on_socket_error::<usize>(Err(error), "receive");
                }
                Err(_)
                    if idle_limit == 0
                        || recovery_started.elapsed() < Duration::from_secs(idle_limit) =>
                {
                    if packet_debug_enabled() {
                        eprintln!(
                            "PTCP receive idle for 15 seconds; preserving the active session"
                        );
                    }
                    continue;
                }
                Err(_) => {
                    eprintln!(
                        "PTCP receive timed out for {} seconds; requesting full P2P reconnect",
                        idle_limit
                    );
                    std::process::exit(75);
                }
            }
        };

        let packet = PTCPPacket::parse(&buf[0..n]);
        let log_packet = packet_debug_enabled()
            || !matches!(&packet.body, PTCPBody::Payload(_) | PTCPBody::Empty);
        if log_packet {
            println!("<<< {}", peer);
            println!("{:?}", packet);
            packet.try_print_data();
            println!("---");
        }

        packet
    }
}

fn restart_on_socket_error<T>(result: std::io::Result<T>, operation: &str) -> T {
    match result {
        Ok(value) => value,
        Err(error) => {
            eprintln!(
                "PTCP {} failed: {}; requesting full P2P reconnect",
                operation, error
            );
            std::process::exit(75);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packet_advances_past_unrecoverable_gap() {
        let mut session = PTCPSession::from_state(0, 100, 0, 0, 0);
        let packet = PTCPPacket {
            sent: 200,
            recv: 0,
            pid: 0x0100_FFEB,
            lmid: 1,
            rmid: 0,
            body: PTCPBody::Command(vec![0x0A, 0, 0, 0]),
        };

        let ready = session.recv(packet);

        assert_eq!(ready.len(), 1);
        assert_eq!(session.recv, 204);
    }

    fn payload_packet(sent: u32, length: usize) -> PTCPPacket {
        PTCPPacket {
            sent,
            recv: 0,
            pid: 0,
            lmid: 1,
            rmid: 0,
            body: PTCPBody::Payload(PTCPPayload {
                realm: 7,
                data: vec![0; length],
            }),
        }
    }

    #[test]
    fn receive_gap_waits_for_missing_packet_then_drains_in_order() {
        let mut session = PTCPSession::from_state(0, 100, 0, 0, 0);
        let ready = session.recv(payload_packet(200, 1280));

        assert!(ready.is_empty());
        assert_eq!(session.recv, 100);

        let ready = session.recv(payload_packet(100, 88));
        assert_eq!(ready.len(), 2);
        assert_eq!(session.recv, 1480);
    }

    #[test]
    fn duplicate_payload_is_not_delivered_twice() {
        let mut session = PTCPSession::from_state(0, 1492, 0, 0, 0);
        let ready = session.recv(payload_packet(200, 1280));

        assert!(ready.is_empty());
        assert_eq!(session.recv, 1492);
    }

    #[test]
    fn receive_offset_wraps_without_treating_new_packet_as_old() {
        let mut session = PTCPSession::from_state(0, u32::MAX - 3, 0, 0, 0);
        let ready = session.recv(payload_packet(u32::MAX - 3, 8));

        assert_eq!(ready.len(), 1);
        assert_eq!(session.recv, 4);
    }

    #[test]
    fn unacknowledged_packet_is_retransmitted_and_then_pruned_by_ack() {
        let mut session = PTCPSession::new();
        session.send(PTCPBody::Heartbeat);
        session.sent_window.front_mut().unwrap().last_sent =
            Instant::now() - RETRANSMIT_AFTER;

        assert_eq!(session.due_retransmissions().len(), 1);

        session.recv(PTCPPacket {
            sent: 0,
            recv: 12,
            pid: 0,
            lmid: 0,
            rmid: 0,
            body: PTCPBody::Empty,
        });
        assert!(session.sent_window.is_empty());
    }
}
