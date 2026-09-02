"""
DH-P2P + PTCP Implementation
"""
import argparse
import datetime
import random
import os
import select
import socket
import subprocess
import sys
import time
from urllib.parse import quote

from .helpers import (
    CLOUDS,
    DEFAULT_CLOUD,
    UDP,
    PTCPPayload,
    get_auth,
    get_dec,
    get_device_info,
    get_enc,
    get_key,
    get_nonce,
    set_cloud,
)


def launch_engine(
    device_remote, socketserver, onvif_socketserver, rtsp_port, public_rtsp_port
):
    print("Ready to connect", flush=True)
    print("Test with: rtsp://127.0.0.1/cam/realmonitor?channel=1&subtype=0")
    receive_buffer = int(os.getenv("P2P_UDP_RECEIVE_BUFFER", str(4 * 1024 * 1024)))
    device_remote.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
    actual_receive_buffer = device_remote.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    print(f"PTCP UDP receive buffer: {actual_receive_buffer} bytes", flush=True)
    device_remote.connect((device_remote.rhost, device_remote.rport))
    engine_path = os.getenv("P2P_RUST_ENGINE", "/usr/local/bin/dh-p2p-engine")
    engine_command = [
        engine_path,
        "--udp-fd", str(device_remote.fileno()),
        "--listener-fd", str(socketserver.fileno()),
        "--http-listener-fd", str(onvif_socketserver.fileno()),
        "--remote-port", str(rtsp_port),
        "--rtsp-public-port", str(public_rtsp_port),
        "--session-sent", str(device_remote.ptcp_sent),
        "--session-recv", str(device_remote.ptcp_recv),
        "--session-count", str(device_remote.ptcp_count),
        "--session-id", str(device_remote.ptcp_id),
        "--session-rmid", str(device_remote.rmid),
    ]
    print("Handing authenticated PTCP session to Rust engine", flush=True)
    engine = subprocess.Popen(
        engine_command,
        pass_fds=(
            device_remote.fileno(),
            socketserver.fileno(),
            onvif_socketserver.fileno(),
        ),
    )
    sys.exit(engine.wait())


def main(
    serial, dtype=0, username=None, password=None, debug=False, cloud=DEFAULT_CLOUD,
    bind_port=554, service="both", public_rtsp_port=None, transport="direct"
):
    # Rebinds the module-level credentials as well, which UDP.request reads.
    main_server, main_port = set_cloud(cloud)
    print(f"Using {cloud} cloud: {main_server}:{main_port}")

    socketserver = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    socketserver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rtsp_address = ("0.0.0.0", bind_port) if service in ("both", "rtsp") else ("127.0.0.1", 0)
    socketserver.bind(rtsp_address)
    socketserver.listen(5)
    actual_rtsp_port = socketserver.getsockname()[1]
    if service in ("both", "rtsp"):
        print(f"RTSP listening on port {actual_rtsp_port}", flush=True)
    onvif_socketserver = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    onvif_socketserver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    onvif_bind_port = bind_port + 1000 if service == "both" else bind_port
    onvif_address = ("0.0.0.0", onvif_bind_port) if service in ("both", "onvif") else ("127.0.0.1", 0)
    onvif_socketserver.bind(onvif_address)
    onvif_socketserver.listen(5)
    if service in ("both", "onvif"):
        print(f"ONVIF/HTTP listening on port {onvif_socketserver.getsockname()[1]}", flush=True)

    if debug:
        subprocess.Popen(
            [
                "ffplay",
                "-rtsp_transport",
                "tcp",
                "-i",
                f"rtsp://{username}:{quote(password)}@127.0.0.1/cam/realmonitor?channel=6&subtype=0",
            ]
        )

    fallback_servers = os.getenv(
        "P2P_MAIN_SERVERS",
        "www.easy4ipcloud.com,146.235.211.50,146.235.223.187,"
        "155.248.199.231,159.54.166.208,159.54.167.231,192.9.243.233",
    ).split(",")
    main_remote = None
    last_probe_error = None
    for candidate in fallback_servers:
        candidate = candidate.strip()
        if not candidate:
            continue
        probe = UDP(candidate, main_port, debug)
        probe.settimeout(8)
        try:
            print(f"Probing P2P server {candidate}:{main_port}", flush=True)
            res = probe.request("/probe/p2psrv")
            # Keep a finite timeout for every Easy4IP control request.  The
            # initial probe can succeed while the following /online request is
            # silently dropped; without this the worker remains "connecting"
            # forever and the manager never gets a chance to rebuild the P2P
            # session.
            probe.settimeout(12)
            main_remote = probe
            main_server = candidate
            print(f"Selected P2P server {candidate}:{main_port}", flush=True)
            break
        except (OSError, socket.timeout) as error:
            last_probe_error = error
            print(f"P2P server {candidate}:{main_port} did not respond: {error}", flush=True)
            probe.close()

    if main_remote is None:
        raise ConnectionError(
            f"No Easy4IP P2P server responded on UDP {main_port}: {last_probe_error}"
        )

    res = main_remote.request(f"/online/p2psrv/{serial}")

    ds_server, ds_port = res["data"]["body"]["DS"].split(":")
    ds_port = int(ds_port)
    p2psrv_server, p2psrv_port = res["data"]["body"]["US"].split(":")
    p2psrv_port = int(p2psrv_port)

    res = None
    info = {}
    last_device_probe_error = None
    device_info_attempts = 5
    for attempt in range(1, device_info_attempts + 1):
        p2psrv_remote = UDP(p2psrv_server, p2psrv_port, debug)
        p2psrv_remote.settimeout(8)
        try:
            print(
                f"Probing device via {p2psrv_server}:{p2psrv_port} "
                f"(attempt {attempt}/{device_info_attempts})",
                flush=True,
            )
            p2psrv_remote.request(f"/probe/device/{serial}")
            res = p2psrv_remote.request(f"/info/device/{serial}")
            response_body = res.get("data", {}).get("body") or {}
            info = get_device_info(response_body.get("Info"))
            if dtype == 0 or info.get("randsalt"):
                break

            last_device_probe_error = ConnectionError(
                "device returned an empty authentication salt"
            )
            print(
                f"Device info attempt {attempt}/{device_info_attempts} returned "
                "no authentication salt; retrying",
                flush=True,
            )
            res = None
        except (OSError, socket.timeout) as error:
            last_device_probe_error = error
            print(
                f"Device probe attempt {attempt}/{device_info_attempts} failed: "
                f"{error}",
                flush=True,
            )
        finally:
            p2psrv_remote.close()
        if attempt < device_info_attempts:
            time.sleep(1)

    if res is None:
        raise ConnectionError(
            f"Device server {p2psrv_server}:{p2psrv_port} did not return "
            f"authentication salt after {device_info_attempts} attempts: "
            f"{last_device_probe_error}"
        )

    randsalt = info.get("randsalt", "")
    rtsp_port = int(info.get("rtspport") or 554)

    if randsalt:
        print(f"Device info: {info}")
    elif dtype == 0:
        print("Device reported no salt, continuing without one.")

    device_remote = UDP(main_server, main_port, debug)
    # SmartPSS uses a separate socket for the pending device channel request
    # and the relay-agent negotiation.
    channel_remote = UDP(main_server, main_port, debug)

    # Advertise the NAS LAN address to Easy4IP. 127.0.0.1 is only a
    # local bind address and causes the cloud to silently discard the channel
    # request because it cannot route the returned channel to loopback.
    advertise_ip = os.getenv("P2P_ADVERTISE_IP", "").strip()
    if advertise_ip:
        print(f"CHANNEL: using configured advertise IP {advertise_ip}", flush=True)
    else:
        route_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            route_probe.connect((main_server, main_port))
            advertise_ip = route_probe.getsockname()[0]
        finally:
            route_probe.close()
    laddr = f"{advertise_ip}:{main_remote.lport}"
    print(f"CHANNEL: advertising LocalAddr {laddr}", flush=True)
    auth = ""
    ipaddr = ""
    aid = random.randbytes(8)

    if not randsalt:
        raise ConnectionError("Device did not provide authentication salt")

    key = get_key(username, password, randsalt)
    nonce = get_nonce()

    # SmartPSS sends an encrypted, fixed 64-byte LocalAddr field.
    local_addr_plain = laddr.encode("ascii").ljust(64, bytes([0]))
    laddr = get_enc(key, nonce, local_addr_plain.decode("latin1"))
    ipaddr = f"<IpEncrptV2>true</IpEncrptV2><LocalAddr>{laddr}</LocalAddr>"
    print(f"CHANNEL: IpEncrptV2=true LocalAddrLen={len(laddr)}", flush=True)
    auth = get_auth(username, key, nonce, randsalt, laddr)

    relay_pcs_request_id = __import__("uuid").uuid4().hex

    def setup_relay_agent():
        # SmartPSS creates the relay agent after the pending direct channel
        # request has been sent, and uses the same PCS request id.
        main_remote.rhost = main_server
        main_remote.rport = main_port
        relay_res = main_remote.request(
            "/online/relay",
            pcs_request_id=relay_pcs_request_id,
        )
        relay_server, relay_port = relay_res["data"]["body"]["Address"].split(":")
        main_remote.rhost = relay_server
        main_remote.rport = int(relay_port)
        agent_res = main_remote.request(
            "/relay/agent",
            f"<body><Dev>{serial}</Dev></body>",
            pcs_request_id=relay_pcs_request_id,
        )
        token = agent_res["data"]["body"]["Token"]
        agent_server, agent_port = agent_res["data"]["body"]["Agent"].split(":")
        agent_port = int(agent_port)
        main_remote.rhost = agent_server
        main_remote.rport = agent_port
        main_remote.request(
            f"/relay/start/{token}",
            f"<body><Dev>{serial}</Dev><Client>:0</Client></body>",
            pcs_request_id=relay_pcs_request_id,
        )
        main_remote.rhost = main_server
        main_remote.rport = main_port
        return agent_server, agent_port

    # Match SmartPSS channel negotiation fields and XML order.
    def field(name):
        return auth.split(f"<{name}>", 1)[1].split(f"</{name}>", 1)[0]

    p2p_channel_body = (
        f"<body><CreateDate>{field('CreateDate')}</CreateDate>"
        f"<DevAuth>{field('DevAuth')}</DevAuth>"
        f"<Identify>{' '.join(f'{b:x}' for b in aid)}</Identify>"
        f"<IpEncrptV2>true</IpEncrptV2><NatValueT>268435455</NatValueT>"
        f"<Nonce>{field('Nonce')}</Nonce><RandSalt>{randsalt}</RandSalt>"
        f"<UserName>{username}</UserName><version>6.7.15</version>"
        f"<sVersion>1.1.0</sVersion><LocalAddr>{laddr}</LocalAddr>"
        f"<Pid>0</Pid></body>"
    )
    if transport == "relay":
        # Relay transport does not require the direct device channel response.
        # The relay-channel negotiation below creates the authenticated PTCP
        # session and hands it to the Rust engine.
        agent_server, agent_port = setup_relay_agent()
        print("CHANNEL: relay-only mode; skipping direct p2p-channel", flush=True)
        res = {
            "code": 200,
            "data": {"body": {"LocalAddr": "127.0.0.1:0", "PubAddr": "127.0.0.1:0"}},
        }
    else:
        pcs_request_id = relay_pcs_request_id
        print(f"CHANNEL: PCS request id {pcs_request_id}", flush=True)
        channel_remote.rhost = ds_server
        channel_remote.rport = ds_port
        print(f"CHANNEL: requesting via DS {ds_server}:{ds_port}", flush=True)
        channel_remote.request(
            f"/device/{serial}/p2p-channel",
            p2p_channel_body,
            should_read=False,
            pcs_request_id=pcs_request_id,
        )

        # SmartPSS starts the relay-agent negotiation while the direct channel
        # response is pending on the separate DS socket.
        agent_server, agent_port = setup_relay_agent()

        res = None
        last_channel_error = None
        channel_attempts = 5
        for attempt in range(1, channel_attempts + 1):
            if attempt > 1:
                print(f"Retrying P2P channel request (attempt {attempt}/{channel_attempts})", flush=True)
                channel_remote.request(
                    f"/device/{serial}/p2p-channel",
                    p2p_channel_body,
                    should_read=False,
                    pcs_request_id=pcs_request_id,
                )
            channel_remote.settimeout(45)
            try:
                res = channel_remote.read(return_error=True)
                while res["code"] < 200:
                    res = channel_remote.read(return_error=True)
                break
            except (OSError, socket.timeout) as error:
                last_channel_error = error
                print(f"P2P channel attempt {attempt}/{channel_attempts} failed: {error}", flush=True)
                res = None
            finally:
                channel_remote.settimeout(None)


    if res["code"] >= 400:
        print("Error:", res["status"])

        if dtype == 0 and res["code"] == 403:
            print("Device requires authentication when creating P2P channel.")
            print("Try again with:")
            print(
                f"main.py --type 1 --username <username> --password <password> {serial}"
            )

        sys.exit(1)

    channel_info = (res.get("data") or {}).get("body") or {}
    missing_channel_fields = [
        field for field in ("LocalAddr", "PubAddr") if not channel_info.get(field)
    ]
    if missing_channel_fields:
        raise ConnectionError(
            "P2P channel response missing required fields: "
            + ", ".join(missing_channel_fields)
        )
    device_laddr = channel_info["LocalAddr"]
    encrypted_address = str(channel_info.get("IpEncrptV2", "")).lower() == "true"
    if dtype > 0 and encrypted_address:
        response_nonce = channel_info.get("Nonce")
        if response_nonce is None:
            raise ConnectionError(
                "Encrypted P2P channel response did not include Nonce"
            )
        nonce = response_nonce
        device_laddr = get_dec(key, nonce, device_laddr)
    elif randsalt:
        # Some firmware returns IpEncrpt=false and an already-plain LocalAddr.
        # Such a response legitimately has no Nonce; keep the nonce generated
        # for DevAuth and do not attempt to decrypt the address.
        print("P2P channel returned an unencrypted LocalAddr", flush=True)

    device_server, device_port = channel_info["PubAddr"].split(":")
    device_port = int(device_port)
    device_remote.rhost = device_server
    device_remote.rport = device_port

    if transport == "relay":
        # Relay mode uses the already-created agent session and asks the device
        # side to return NAT information through that agent.
        relay_nat_response = None
        if randsalt:
            auth = get_auth(username, key, nonce, randsalt)
        channel_remote.rhost = ds_server
        channel_remote.rport = ds_port
        channel_remote.request(
            f"/device/{serial}/relay-channel",
            f"<body>{auth}<sVersion>1.1.0</sVersion>"
            f"<agentAddr>{agent_server}:{agent_port}</agentAddr></body>",
            should_read=False,
            pcs_request_id=relay_pcs_request_id,
        )
        main_remote.rhost = agent_server
        main_remote.rport = agent_port
        main_remote.settimeout(10)
        try:
            relay_nat_response = main_remote.read()
        except (OSError, socket.timeout) as error:
            raise ConnectionError(
                f"Agent server {agent_server}:{agent_port} did not return NAT info: {error}"
            ) from error
        finally:
            main_remote.settimeout(None)
    else:
        # In direct mode the p2p-channel response is the NAT response. Restore
        # the relay agent endpoint for the PTCP sign exchange; do not create a
        # second relay session or send relay-channel.
        main_remote.rhost = agent_server
        main_remote.rport = agent_port

    main_remote.request_ptcp(b"\x00\x03\x01\x00")
    res = main_remote.read_ptcp()

    if transport == "relay":
        if res.body != b"\x00\x03\x01\x00":
            raise ConnectionError("Relay transport did not acknowledge PTCP sync")
        print(
            f"Using Easy4IP UDP relay transport via {agent_server}:{agent_port}",
            flush=True,
        )
        device_remote.close()
        launch_engine(
            main_remote,
            socketserver,
            onvif_socketserver,
            rtsp_port,
            public_rtsp_port or actual_rtsp_port,
        )

    main_remote.request_ptcp(b"\x17\x00\x00\x00" + b"\x00\x00\x00\x00\x00\x00\x00\x00")
    sign = None
    for _ in range(12):
        try:
            res = main_remote.read_ptcp(timeout=3)
        except socket.timeout:
            break
        control = f"0x{res.body[0]:02X}" if res.body else "ACK"
        print(
            f"Relay PTCP response: {control} ({len(res.body)} bytes) "
            f"body={res.body.hex()}",
            flush=True,
        )
        if res.body and res.body[0] == 0x13:
            print("Acknowledging relay PTCP heartbeat 0x13", flush=True)
            main_remote.request_ptcp()
            continue
        if len(res.body) > 12 and res.body[0] == 0x18:
            sign = res.body[12:]
            break
    if sign is None:
        raise ConnectionError("Relay server did not return PTCP sign response 0x18")
    print(f"Relay PTCP sign received ({len(sign)} bytes)", flush=True)

    main_remote.request_ptcp()

    device_remote.rhost = device_server
    device_remote.rport = device_port

    aid = bytes(0xFF - b for b in aid)
    cookie = random.randbytes(4)
    trasn_id = random.randbytes(12)
    eaddr = device_port.to_bytes(2) + socket.inet_aton(device_server)
    eaddr = bytes(0xFF - b for b in eaddr)

    data = (
        b"\xff\xfe\xff\xe7"
        + cookie
        + trasn_id
        + b"\x7f\xd5\xff\xf7"
        + aid
        + b"\xff\xfb\xff\xf7\xff\xfe"
        + eaddr
    )
    print(f":{device_remote.lport} >>> {device_remote.rhost}:{device_remote.rport}")
    print("".join(f"\\x{b:02X}" for b in data))
    device_remote.send(data)

    try:
        data = device_remote.recv(timeout=5)
    except socket.timeout:
        # Relay mode is Rust-only by design. It routes through the agent server,
        # which pushes SYN, heartbeat, payload and status unprompted and in any
        # order. That needs concurrent read/write, a client-initiated heartbeat
        # and read timeouts -- none of which fit this simplex, blocking script.
        # A prototype carried RTSP only as far as SETUP; Rust reaches RTP.
        print("Timeout occurred while waiting for a response from the device.")
        print("If the issue persists, you may need to use relay mode with this device.")
        print("Note: Relay mode is Rust-only; try: dh-p2p --relay", serial)
        sys.exit(1)

    print("Data <<<")
    print("".join(f"\\x{b:02X}" for b in data))

    rtrans_id = data[8:20]
    ip, port = device_laddr.split(":")
    port = int(port)
    eaddr = port.to_bytes(2) + socket.inet_aton(ip)

    data = (
        b"\xfe\xfe\xff\xe7"
        + cookie
        + rtrans_id
        + b"\x7f\xd6\xff\xf7"
        + aid
        + b"\xff\xfb\xff\xf7\xff\xfe"
        + eaddr
    )
    print("Request >>>")
    print("".join(f"\\x{b:02X}" for b in data))
    device_remote.send(data)

    if randsalt:
        data = device_remote.recv()
        print("Data <<<")
        print("".join(f"\\x{b:02X}" for b in data))

        data = (
            b"\xfe\xfe\xff\xf3"
            + cookie
            + rtrans_id
            + b"\x7f\xd6\xff\xf7"
            + aid
            + b"\xff\xfb\xff\xf7\xff\xfe"
            + b"\xa8\x13\x3f\x57\xfe\x37"
        )

        for _ in range(5):
            print("Request >>>")
            print("".join(f"\\x{b:02X}" for b in data))
            device_remote.send(data)

    # Cameras do not necessarily acknowledge every repeated hole-punch packet.
    # Drain what is available, then continue instead of waiting forever for an
    # arbitrary fifth reply.
    replies = 0
    for _ in range(5):
        try:
            data = device_remote.recv(timeout=2)
        except socket.timeout:
            break
        replies += 1
        print("Data <<<")
        print("".join(f"\\x{b:02X}" for b in data))
    print(f"Hole-punch acknowledgements received: {replies}", flush=True)

    if replies == 0:
        # The cloud advertised both p2p and udprelay.  A direct UDP route is
        # not usable when the camera never acknowledges the hole punch.  The
        # relay PTCP session above is already authenticated and synchronized,
        # so hand it to the engine instead of blocking forever on the direct
        # socket.  This mirrors the transport fallback performed by DMSS.
        print(
            "Direct P2P hole punch failed; switching to the active Easy4IP UDP relay",
            flush=True,
        )
        device_remote.close()
        launch_engine(
            main_remote,
            socketserver,
            onvif_socketserver,
            rtsp_port,
            public_rtsp_port or actual_rtsp_port,
        )

    device_remote.request_ptcp(b"\x00\x03\x01\x00")
    try:
        res = device_remote.read_ptcp(timeout=5)
    except socket.timeout:
        print(
            "Direct P2P sync timed out; switching to the active Easy4IP UDP relay",
            flush=True,
        )
        device_remote.close()
        launch_engine(
            main_remote,
            socketserver,
            onvif_socketserver,
            rtsp_port,
            public_rtsp_port or actual_rtsp_port,
        )
    if res.body != b"\x00\x03\x01\x00":
        raise ConnectionError(
            f"Unexpected direct PTCP sync response: {res.body.hex()}"
        )

    channel_request = (
        b"\x19\x00\x00\x00" + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00" + sign
    )
    device_remote.request_ptcp(channel_request)
    channel_response = None
    for _ in range(8):
        try:
            res = device_remote.read_ptcp(timeout=3)
        except socket.timeout:
            break
        control = f"0x{res.body[0]:02X}" if res.body else "ACK"
        print(
            f"PTCP channel response: {control} ({len(res.body)} bytes) "
            f"body={res.body.hex()}",
            flush=True,
        )
        if len(res.body) >= 4 and res.body[0] == 0x13:
            # 0x13 is a heartbeat (not a request/response pair). The protocol
            # acknowledges it with an empty PTCP body.
            print("Acknowledging PTCP heartbeat 0x13", flush=True)
            device_remote.request_ptcp()
            continue
        if res.body and res.body[0] == 0x1A:
            channel_response = res
            break
    if channel_response is None:
        raise ConnectionError("Device did not return PTCP channel response 0x1A")

    device_remote.request_ptcp(
        b"\x1b\x00\x00\x00" + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00"
    )
    close_ack = None
    for _ in range(6):
        try:
            res = device_remote.read_ptcp(timeout=3)
        except socket.timeout:
            break
        control = f"0x{res.body[0]:02X}" if res.body else "ACK"
        print(f"PTCP setup acknowledgement: {control} ({len(res.body)} bytes)", flush=True)
        if not res.body:
            close_ack = res
            break
    if close_ack is None:
        raise ConnectionError("Device did not acknowledge PTCP channel setup")

    launch_engine(
        device_remote,
        socketserver,
        onvif_socketserver,
        rtsp_port,
        public_rtsp_port or actual_rtsp_port,
    )

    while True:
        ready, _, _ = select.select([socketserver], [], [], 0.1)

        if not ready:
            ptcp_ready, _, _ = select.select([device_remote], [], [], 0)

            if not ptcp_ready:
                continue

            # only simplex, duplex is not supported
            res = device_remote.read_ptcp()
            if len(res.body) == 0:
                continue

            control = res.body[0]
            print(
                f"Idle PTCP control packet: 0x{control:02X} "
                f"body={res.body.hex()}",
                flush=True,
            )
            device_remote.request_ptcp()

            continue

        socketclient, address = socketserver.accept()
        print(f"Connection from {address}")

        realm_id = random.randint(0x00000000, 0xFFFFFFFF)
        bind_request = (
            b"\x11\x00\x00\x00"
            + realm_id.to_bytes(4, "big")
            + b"\x00\x00\x00\x00"
            + rtsp_port.to_bytes(4, "big")
            + b"\x7f\x00\x00\x01"
        )
        bind_response = None
        for attempt in range(1, 4):
            print(
                f"Requesting camera RTSP port {rtsp_port} "
                f"(attempt {attempt}/3)",
                flush=True,
            )
            device_remote.request_ptcp(bind_request)
            for _ in range(4):
                try:
                    res = device_remote.read_ptcp(timeout=3)
                except socket.timeout:
                    break
                control = f"0x{res.body[0]:02X}" if res.body else "ACK"
                print(
                    f"RTSP bind response: {control} ({len(res.body)} bytes) "
                    f"body={res.body.hex()}",
                    flush=True,
                )
                if res.body and res.body[0] == 0x13:
                    print("Acknowledging PTCP heartbeat during RTSP bind", flush=True)
                    device_remote.request_ptcp()
                    continue
                if res.body and res.body[0] == 0x12:
                    bind_response = res
                    break
            if bind_response is not None:
                break
        if bind_response is None:
            print(
                "Camera did not acknowledge RTSP port bind; restarting P2P session",
                flush=True,
            )
            socketclient.close()
            sys.exit(75)
        bind_status = bind_response.body[12:]
        print(f"RTSP bind status: {bind_status!r}", flush=True)
        if bind_status == b"DISC":
            print("Camera rejected RTSP port bind; restarting P2P session", flush=True)
            socketclient.close()
            sys.exit(75)

        try:
            while True:
                ptcp_ready, _, _ = select.select([device_remote], [], [], 0.1)

                # if ptcp_ready:
                while ptcp_ready:
                    res = device_remote.read_ptcp()

                    if len(res.body) == 0:
                        continue

                    device_remote.request_ptcp()

                    if res.body[0] != 0x10:
                        continue

                    body = PTCPPayload.parse(res.body)

                    response_line = body.payload.split(b"\r\n", 1)[0][:160]
                    print(
                        f"Camera -> RTSP client: {len(body.payload)} bytes "
                        f"({response_line!r})",
                        flush=True,
                    )

                    if b"Content-Type: application/sdp" in body.payload:
                        headers, separator, sdp = body.payload.partition(b"\r\n\r\n")
                        declared_length = None
                        for header in headers.split(b"\r\n"):
                            if header.lower().startswith(b"content-length:"):
                                try:
                                    declared_length = int(header.split(b":", 1)[1].strip())
                                except ValueError:
                                    pass
                        controls = [
                            line.decode("utf-8", "replace")
                            for line in sdp.split(b"\r\n")
                            if line.startswith((b"m=", b"a=control:", b"a=rtpmap:"))
                        ]
                        print(
                            f"SDP body: received={len(sdp)} "
                            f"declared={declared_length} controls={controls}",
                            flush=True,
                        )

                    if debug:
                        print()
                        print(body)
                        print(f"[{datetime.datetime.now().isoformat()}]")
                        print("Data <<<")
                        print(body.payload)
                        print()

                    socketclient.sendall(body.payload)

                    ptcp_ready, _, _ = select.select([device_remote], [], [], 0.1)

                client_ready, _, _ = select.select([socketclient], [], [], 0)

                if not client_ready:
                    continue

                data = socketclient.recv(4096)

                if not data:
                    print("Connection closed?")
                    break

                request_line = data.split(b"\r\n", 1)[0][:160]
                print(
                    f"RTSP client -> camera: {len(data)} bytes ({request_line!r})",
                    flush=True,
                )

                if debug:
                    print()
                    print(f"[{datetime.datetime.now().isoformat()}]")
                    print("Data >>>")
                    print(data)
                    print()

                device_remote.request_ptcp(bytes(PTCPPayload(realm_id, data)))

        # handle connection reset by peer
        except ConnectionResetError:
            print("Connection reset by peer")
        except BrokenPipeError:
            print("Broken pipe")
        finally:
            print("Cleaning up connection and requesting tunnel restart", flush=True)
            try:
                device_remote.request_ptcp(
                    b"\x12\x00\x00\x00"
                    + realm_id.to_bytes(4, "big")
                    + b"\x00\x00\x00\x00"
                    + b"DISC"
                )
            except OSError:
                pass
            socketclient.close()
            print("Connection closed; restarting P2P session", flush=True)
            sys.exit(75)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("serial", help="Serial number of the camera")
    parser.add_argument("-d", "--debug", action="store_true", help="Enable debug mode")
    parser.add_argument(
        "--bind-port", type=int, default=int(os.getenv("P2P_BIND_PORT", "554"))
    )
    parser.add_argument("--service", choices=("both", "rtsp", "onvif"), default="both")
    parser.add_argument("--public-rtsp-port", type=int)
    parser.add_argument("--transport", choices=("direct", "relay"), default=os.getenv("P2P_TRANSPORT", "direct"))
    parser.add_argument("-t", "--type", type=int, help="Type of the camera", default=0)
    parser.add_argument("-u", "--username", help="Username of the camera")
    parser.add_argument("-p", "--password", help="Password of the camera")
    parser.add_argument(
        "-c",
        "--cloud",
        choices=sorted(CLOUDS),
        default=DEFAULT_CLOUD,
        help="P2P cloud the camera is registered with (default: %(default)s)",
    )
    args = parser.parse_args()
    args.username = args.username or os.getenv("P2P_USERNAME")
    args.password = args.password or os.getenv("P2P_PASSWORD")

    if args.username is None or args.password is None:
        if args.type > 0:
            parser.error("Username and password are required for type > 0")
        elif args.debug:
            parser.error("Username and password are required in debug mode")

    if args.serial:
        main(
            args.serial,
            args.type,
            args.username,
            args.password,
            args.debug,
            args.cloud,
            args.bind_port,
            args.service,
            args.public_rtsp_port,
            args.transport,
        )
