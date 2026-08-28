import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from app.p2p_manager import P2PManager

DATA_PATH = Path(os.getenv("DATABASE_PATH", "data/bridge.db"))
STATIC_PATH = Path(__file__).parent / "static"
APP_SECRET = os.getenv("APP_SECRET_KEY", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_COOKIE = "bridge_session"
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(30 * 24 * 60 * 60)))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

app = FastAPI(title="Dahua P2P Bridge", version="0.4.8")
security = HTTPBasic(auto_error=False)
p2p_manager = P2PManager()


class CameraInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    vendor: Literal["dahua", "policetech", "imou"]
    serial: str = Field(min_length=4, max_length=80)
    username: str = Field(default="admin", min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    channel: int = Field(default=0, ge=0, le=255)
    stream: Literal["main", "sub"] = "main"
    enabled: bool = True
    motion_enabled: bool = True
    synology_webhook: str | None = Field(default=None, max_length=1000)


class CameraPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    username: str | None = Field(default=None, min_length=1, max_length=64)
    password: str | None = Field(default=None, min_length=1, max_length=256)
    channel: int | None = Field(default=None, ge=0, le=255)
    stream: Literal["main", "sub"] | None = None
    enabled: bool | None = None
    motion_enabled: bool | None = None
    synology_webhook: str | None = Field(default=None, max_length=1000)


class LoginInput(BaseModel):
    username: str
    password: str


def _session_signing_key() -> bytes:
    if len(APP_SECRET) < 24:
        raise HTTPException(503, "APP_SECRET_KEY must contain at least 24 characters")
    return hashlib.sha256(APP_SECRET.encode()).digest()


def create_session_token() -> str:
    payload = json.dumps(
        {"user": "admin", "exp": int(time.time()) + SESSION_MAX_AGE, "nonce": secrets.token_hex(8)},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(_session_signing_key(), encoded, hashlib.sha256).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def valid_session_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected = hmac.new(_session_signing_key(), encoded.encode(), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(supplied_signature + "=" * (-len(supplied_signature) % 4))
        if not hmac.compare_digest(expected, supplied):
            return False
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return payload.get("user") == "admin" and int(payload.get("exp", 0)) > int(time.time())
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def require_auth(
    request: Request, credentials: HTTPBasicCredentials | None = Depends(security)
) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD is not configured")
    if valid_session_token(request.cookies.get(SESSION_COOKIE)):
        return
    valid_user = credentials is not None and hmac.compare_digest(credentials.username, "admin")
    valid_password = credentials is not None and hmac.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (valid_user and valid_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required", {"WWW-Authenticate": "Basic"})


def cipher() -> Fernet:
    if len(APP_SECRET) < 24:
        raise HTTPException(503, "APP_SECRET_KEY must contain at least 24 characters")
    key = base64.urlsafe_b64encode(hashlib.sha256(APP_SECRET.encode()).digest())
    return Fernet(key)


@contextmanager
def db():
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATA_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    with db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                vendor TEXT NOT NULL,
                serial TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                password_enc TEXT NOT NULL,
                channel INTEGER NOT NULL DEFAULT 0,
                stream TEXT NOT NULL DEFAULT 'main',
                enabled INTEGER NOT NULL DEFAULT 1,
                motion_enabled INTEGER NOT NULL DEFAULT 1,
                synology_webhook TEXT,
                status TEXT NOT NULL DEFAULT 'not_tested',
                last_error TEXT
            )
        """)


@app.on_event("startup")
def startup() -> None:
    init_db()
    with db() as connection:
        rows = connection.execute("SELECT * FROM cameras WHERE enabled=1").fetchall()
    for row in rows:
        try:
            password = cipher().decrypt(row["password_enc"].encode()).decode()
            p2p_manager.start(dict(row), password)
        except (InvalidToken, ValueError) as error:
            # The status endpoint exposes the actionable error after a manual retry.
            print(f"Could not start camera {row['id']}: {error}")


@app.on_event("shutdown")
def shutdown() -> None:
    p2p_manager.stop_all()


def public_camera(row: sqlite3.Row) -> dict:
    item = dict(row)
    item.pop("password_enc", None)
    item["enabled"] = bool(item["enabled"])
    item["motion_enabled"] = bool(item["motion_enabled"])
    worker = p2p_manager.status(item["id"])
    item["status"] = worker["status"]
    item["last_error"] = worker["last_error"]
    item["rtsp_path"] = (
        f"rtsp://NAS-IP:{p2p_manager.port_for(item['id'])}"
        f"/cam/realmonitor?channel={item['channel'] + 1}"
        f"&subtype={0 if item['stream'] == 'main' else 1}"
    )
    item["onvif_port"] = p2p_manager.onvif_port_for(item["id"])
    item["onvif_service"] = f"http://NAS-IP:{item['onvif_port']}/onvif/device_service"
    return item


@app.get("/")
def index():
    return FileResponse(
        STATIC_PATH / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version, "p2p_engine": "not-installed"}


@app.post("/api/login")
def login(login_data: LoginInput, response: Response):
    valid_user = hmac.compare_digest(login_data.username, "admin")
    valid_password = bool(ADMIN_PASSWORD) and hmac.compare_digest(
        login_data.password, ADMIN_PASSWORD
    )
    if not (valid_user and valid_password):
        raise HTTPException(401, "Napačno uporabniško ime ali geslo")
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=COOKIE_SECURE,
        path="/",
    )
    return {"username": "admin"}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@app.get("/api/session", dependencies=[Depends(require_auth)])
def session_info():
    return {"username": "admin"}


@app.get("/api/cameras", dependencies=[Depends(require_auth)])
def list_cameras():
    with db() as connection:
        return [public_camera(row) for row in connection.execute("SELECT * FROM cameras ORDER BY name")]


@app.post("/api/cameras", status_code=201, dependencies=[Depends(require_auth)])
def add_camera(camera: CameraInput):
    encrypted = cipher().encrypt(camera.password.encode()).decode()
    try:
        with db() as connection:
            cursor = connection.execute(
                """INSERT INTO cameras
                (name,vendor,serial,username,password_enc,channel,stream,enabled,motion_enabled,synology_webhook)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (camera.name, camera.vendor, camera.serial, camera.username, encrypted, camera.channel,
                 camera.stream, camera.enabled, camera.motion_enabled, camera.synology_webhook),
            )
            row = connection.execute("SELECT * FROM cameras WHERE id=?", (cursor.lastrowid,)).fetchone()
            result = public_camera(row)
        if camera.enabled and camera.vendor in ("dahua", "policetech"):
            p2p_manager.start(dict(row), camera.password)
        return result
    except sqlite3.IntegrityError:
        raise HTTPException(409, "A camera with this serial already exists")


@app.patch("/api/cameras/{camera_id}", dependencies=[Depends(require_auth)])
def update_camera(camera_id: int, patch: CameraPatch):
    values = patch.model_dump(exclude_unset=True)
    if "password" in values:
        values["password_enc"] = cipher().encrypt(values.pop("password").encode()).decode()
    if not values:
        raise HTTPException(400, "No changes supplied")
    fields = ", ".join(f"{key}=?" for key in values)
    with db() as connection:
        cursor = connection.execute(f"UPDATE cameras SET {fields} WHERE id=?", (*values.values(), camera_id))
        if cursor.rowcount == 0:
            raise HTTPException(404, "Camera not found")
        row = connection.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
        result = public_camera(row)
    p2p_manager.stop(camera_id)
    if row["enabled"] and row["vendor"] in ("dahua", "policetech"):
        password = cipher().decrypt(row["password_enc"].encode()).decode()
        p2p_manager.start(dict(row), password)
    return result


@app.delete("/api/cameras/{camera_id}", status_code=204, dependencies=[Depends(require_auth)])
def delete_camera(camera_id: int):
    p2p_manager.stop(camera_id)
    with db() as connection:
        if connection.execute("DELETE FROM cameras WHERE id=?", (camera_id,)).rowcount == 0:
            raise HTTPException(404, "Camera not found")


@app.post("/api/cameras/{camera_id}/test", dependencies=[Depends(require_auth)])
def test_camera(camera_id: int):
    with db() as connection:
        row = connection.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Camera not found")
    if row["vendor"] not in ("dahua", "policetech"):
        raise HTTPException(501, f"P2P adapter for {row['vendor']} is not implemented yet")
    try:
        password = cipher().decrypt(row["password_enc"].encode()).decode()
        state = p2p_manager.start(dict(row), password)
    except (InvalidToken, ValueError) as error:
        raise HTTPException(400, str(error))
    return {"status": state.status, "port": state.port}


@app.get("/api/cameras/{camera_id}/status", dependencies=[Depends(require_auth)])
def camera_status(camera_id: int):
    with db() as connection:
        if connection.execute("SELECT 1 FROM cameras WHERE id=?", (camera_id,)).fetchone() is None:
            raise HTTPException(404, "Camera not found")
    return p2p_manager.status(camera_id)
