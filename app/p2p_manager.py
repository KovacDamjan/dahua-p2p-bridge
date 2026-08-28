import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field

LOG_HISTORY_LIMIT = max(100, int(os.getenv("P2P_LOG_HISTORY", "1000")))
LOG_STATUS_LIMIT = max(20, int(os.getenv("P2P_LOG_STATUS_LINES", "500")))
TRANSIENT_RETRY_SECONDS = max(1, int(os.getenv("P2P_TRANSIENT_RETRY_SECONDS", "60")))
TRANSIENT_FAILURE_MARKERS = (
    "timed out",
    "temporary failure in name resolution",
    "connection refused",
    "timeout occurred",
    "did not return nat info",
    "did not return p2p channel",
    "did not return authentication salt",
    "devpwd_invalidsalt",
    "encrypted p2p channel response did not include nonce",
    "no easy4ip p2p server responded",
)


@dataclass
class ServiceState:
    process: subprocess.Popen
    service: str
    status: str = "connecting"
    last_error: str | None = None


@dataclass
class WorkerState:
    port: int
    camera: dict
    password: str
    services: dict[str, ServiceState] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        statuses = [state.status for state in self.services.values()]
        if len(statuses) < 2 or "connecting" in statuses:
            return "connecting"
        if "error" in statuses:
            return "error"
        if statuses and all(status == "online" for status in statuses):
            return "online"
        return "stopped"

    @property
    def last_error(self) -> str | None:
        for service in ("rtsp", "onvif"):
            state = self.services.get(service)
            if state and state.last_error:
                return f"{service.upper()}: {state.last_error}"
        return None


class P2PManager:
    """Owns one shared upstream P2P session for RTSP and ONVIF per camera."""

    def __init__(self, first_port: int = 15540, max_cameras: int = 30):
        self.first_port = first_port
        self.max_cameras = max_cameras
        self._workers: dict[int, WorkerState] = {}
        self._lock = threading.RLock()

    def port_for(self, camera_id: int) -> int:
        if camera_id < 1 or camera_id > self.max_cameras:
            raise ValueError(f"camera id must be between 1 and {self.max_cameras}")
        return self.first_port + camera_id - 1

    def onvif_port_for(self, camera_id: int) -> int:
        return self.port_for(camera_id) + 1000

    def start(self, camera: dict, password: str) -> WorkerState:
        camera_id = int(camera["id"])
        self.stop(camera_id)
        if camera["vendor"] not in ("dahua", "policetech"):
            raise ValueError(f"P2P adapter for {camera['vendor']} is not implemented")

        worker = WorkerState(
            port=self.port_for(camera_id), camera=dict(camera), password=password
        )
        with self._lock:
            self._workers[camera_id] = worker
        self._start_service(camera_id, worker, "rtsp")
        return worker

    def _start_service(self, camera_id: int, worker: WorkerState, service: str) -> None:
        bind_port = worker.port if service == "rtsp" else self.onvif_port_for(camera_id)
        if service == "rtsp":
            # Both local listeners live in one process and share the authenticated
            # upstream PTCP session. Remove an alias left by the previous process.
            worker.services.pop("onvif", None)
        env = os.environ.copy()
        env.update(
            P2P_USERNAME=worker.camera["username"],
            P2P_PASSWORD=worker.password,
            P2P_IDLE_RECONNECT_SECONDS="60",
            PYTHONUNBUFFERED="1",
        )
        command = [
            sys.executable,
            "-m",
            "app.vendor.dh_p2p.main",
            "--type",
            "1",
            "--service",
            "both" if service == "rtsp" else service,
            "--bind-port",
            str(bind_port),
            "--public-rtsp-port",
            str(worker.port),
            "--transport",
            "direct",
            worker.camera["serial"],
        ]
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        state = ServiceState(process=process, service=service)
        worker.services[service] = state
        worker.logs.append(f"[{service.upper()}] Starting independent P2P session")
        worker.logs[:] = worker.logs[-LOG_HISTORY_LIMIT:]
        threading.Thread(
            target=self._read_output,
            args=(camera_id, worker, state),
            daemon=True,
        ).start()

    def _read_output(
        self, camera_id: int, worker: WorkerState, state: ServiceState
    ) -> None:
        assert state.process.stdout is not None
        for raw_line in state.process.stdout:
            line = raw_line.rstrip()
            with self._lock:
                worker.logs.append(f"[{state.service.upper()}] {line}")
                worker.logs[:] = worker.logs[-LOG_HISTORY_LIMIT:]
                if "Ready to connect" in line:
                    state.status = "online"
                    state.last_error = None
                    if state.service == "rtsp" and "onvif" not in worker.services:
                        worker.services["onvif"] = state
                        worker.logs.append(
                            "[ONVIF] Sharing the authenticated RTSP P2P session"
                        )
                elif "Error:" in line or "Traceback" in line:
                    state.last_error = line

        return_code = state.process.wait()
        restart = False
        restart_delay = 1
        with self._lock:
            current = self._workers.get(camera_id)
            if current is worker and worker.services.get(state.service) is state:
                recent_logs = worker.logs[-100:]
                if return_code == 75:
                    state.status = "connecting"
                    state.last_error = None
                    worker.logs.append(
                        f"[{state.service.upper()}] P2P engine requested reconnect; "
                        "rebuilding only this session"
                    )
                    restart = True
                elif return_code != 0 and any(
                    marker in line.lower()
                    for line in recent_logs
                    for marker in TRANSIENT_FAILURE_MARKERS
                ):
                    state.status = "connecting"
                    worker.logs.append(
                        f"[{state.service.upper()}] Transient Easy4IP failure; retrying in "
                        f"{TRANSIENT_RETRY_SECONDS} seconds"
                    )
                    restart = True
                    restart_delay = TRANSIENT_RETRY_SECONDS
                else:
                    state.status = "stopped" if return_code == 0 else "error"
                if return_code not in (0, 75) and not state.last_error:
                    state.last_error = f"P2P worker exited with code {return_code}"
                worker.logs[:] = worker.logs[-LOG_HISTORY_LIMIT:]

        if restart:
            threading.Event().wait(restart_delay)
            with self._lock:
                if (
                    self._workers.get(camera_id) is worker
                    and worker.services.get(state.service) is state
                ):
                    self._start_service(camera_id, worker, state.service)

    def stop(self, camera_id: int) -> None:
        with self._lock:
            worker = self._workers.pop(camera_id, None)
        if not worker:
            return
        states = list({id(state): state for state in worker.services.values()}.values())
        for state in states:
            if state.process.poll() is None:
                state.process.terminate()
        for state in states:
            if state.process.poll() is None:
                try:
                    state.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    state.process.kill()

    def status(self, camera_id: int) -> dict:
        with self._lock:
            worker = self._workers.get(camera_id)
            if worker is None:
                return {"status": "stopped", "last_error": None, "logs": []}
            return {
                "status": worker.status,
                "last_error": worker.last_error,
                "port": worker.port,
                "services": {
                    name: {"status": state.status, "last_error": state.last_error}
                    for name, state in worker.services.items()
                },
                "logs": worker.logs[-LOG_STATUS_LIMIT:],
            }

    def stop_all(self) -> None:
        with self._lock:
            camera_ids = list(self._workers)
        for camera_id in camera_ids:
            self.stop(camera_id)
