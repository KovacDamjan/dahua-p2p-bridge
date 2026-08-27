# Dahua P2P Bridge

Private, early-stage manager for exposing remote Dahua/PoliceTech cameras to Synology Surveillance Station.

> **Status:** experimental. The first Easy4IP/PTCP adapter is included and ready for a real-camera connectivity test. Do not rely on it for production recording yet.

## Synology quick start

1. Copy `.env.example` to `.env` and replace both secrets.
2. In Synology Container Manager create a Project from `compose.yaml`.
3. Open `http://NAS-IP:8095`.

Generate secrets on any machine with Python:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The default image name is `ghcr.io/kovacdamjan/dahua-p2p-bridge:latest`. Until the first GHCR release is published, Container Manager can build from the included `Dockerfile` by replacing `image:` with `build: .`.

## Security

- Camera passwords are encrypted with Fernet before being stored in SQLite.
- `APP_SECRET_KEY`, `ADMIN_PASSWORD`, `.env`, `data/` and vendor SDK files must never be committed.
- The web port should only be exposed to a trusted LAN/VPN.
- Dahua/SmartPSS binaries are not included.
- Local RTSP tunnels use TCP ports `15540-15569`; restrict them to the trusted LAN.

## Planned adapters

1. Stabilize Dahua/PoliceTech Easy4IP P2P video and audio
2. Dahua motion/SMD/IVS events to Synology webhooks
3. IMOU OpenSDK adapter
4. Optional ONVIF event facade
