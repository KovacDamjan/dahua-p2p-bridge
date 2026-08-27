import base64
import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

DATA_PATH = Path(os.getenv("DATABASE_PATH", "data/bridge.db"))
STATIC_PATH = Path(__file__).parent / "static"
APP_SECRET = os.getenv("APP_SECRET_KEY", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

app = FastAPI(title="Dahua P2P Bridge", version="0.1.0")
security = HTTPBasic(auto_error=False)


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


def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD is not configured")
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


def public_camera(row: sqlite3.Row) -> dict:
    item = dict(row)
    item.pop("password_enc", None)
    item["enabled"] = bool(item["enabled"])
    item["motion_enabled"] = bool(item["motion_enabled"])
    item["rtsp_path"] = f"rtsp://NAS-IP:8554/camera-{item['id']}"
    return item


@app.get("/")
def index():
    return FileResponse(STATIC_PATH / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version, "p2p_engine": "not-installed"}


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
            return public_camera(row)
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
        return public_camera(connection.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone())


@app.delete("/api/cameras/{camera_id}", status_code=204, dependencies=[Depends(require_auth)])
def delete_camera(camera_id: int):
    with db() as connection:
        if connection.execute("DELETE FROM cameras WHERE id=?", (camera_id,)).rowcount == 0:
            raise HTTPException(404, "Camera not found")


@app.post("/api/cameras/{camera_id}/test", dependencies=[Depends(require_auth)])
def test_camera(camera_id: int):
    with db() as connection:
        row = connection.execute("SELECT * FROM cameras WHERE id=?", (camera_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Camera not found")
    raise HTTPException(501, "P2P adapter is not installed yet; configuration was saved safely")
