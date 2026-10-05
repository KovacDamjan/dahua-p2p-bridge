import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

LOG_HISTORY_LIMIT = max(500, int(os.getenv("P2P_LOG_HISTORY", "10000")))
LOG_STATUS_LIMIT = max(100, int(os.getenv("P2P_LOG_STATUS_LINES", "5000")))
TRANSIENT_RETRY_SECONDS = max(1, int(os.getenv("P2P_TRANSIENT_RETRY_SECONDS", "15")))
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
    "p2p channel response missing required fields",
    "no easy4ip p2p server responded",
)


@dataclass
class ServiceState:
    process: subprocess.Popen
    service: str
    status: str = "connecting"
    last_error: str | None = None
    reconnect_attempt: int = 0
    online_since: float | None = None


@dataclass
class WorkerState:
    port: int
    camera: dict
    password: str
    services: dict[str, ServiceState] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    proxy_process: subprocess.Popen | None = None
    onvif_server: object | None = None

    @property
    def status(self) -> str:
        statuses = [state.status for state in self.services.values()]
        if not statuses:
            return "stopped"
        # A camera is usable only when every required local channel is ready.
        # Reporting online when only ONVIF is alive hides a dead RTSP stream
        # from Surveillance Station.
        if all(status == "online" for status in statuses):
            return "online"
        if "error" in statuses:
            return "error"
        if "connecting" in statuses:
            return "connecting"
        if "online" in statuses:
            return "connecting"
        return "stopped"

    @property
    def last_error(self) -> str | None:
        for service in ("rtsp", "onvif"):
            state = self.services.get(service)
            if state and state.last_error:
                return f"{service.upper()}: {state.last_error}"
        return None


class P2PManager:
    """Owns independent upstream P2P sessions for RTSP and ONVIF per camera."""

    def __init__(self, first_port: int = 15540, max_cameras: int = 30):
        self.first_port = first_port
        self.max_cameras = max_cameras
        self._workers: dict[int, WorkerState] = {}
        self._lock = threading.RLock()
        # Log output can be very noisy (especially ONVIF PullMessages). Keep it
        # independent from worker state so status/UI requests never wait on logs.
        self._log_lock = threading.Lock()
        # Serialize lifecycle changes per camera. Test/reconnect/startup may
        # otherwise launch two workers that race for the same RTSP port.
        self._lifecycle_locks: dict[int, threading.RLock] = {}

    def port_for(self, camera_id: int) -> int:
        if camera_id < 1 or camera_id > self.max_cameras:
            raise ValueError(f"camera id must be between 1 and {self.max_cameras}")
        return self.first_port + camera_id - 1

    def onvif_port_for(self, camera_id: int) -> int:
        return self.port_for(camera_id) + 1000

    def _lifecycle_lock_for(self, camera_id: int) -> threading.RLock:
        with self._lock:
            lock = self._lifecycle_locks.get(camera_id)
            if lock is None:
                lock = threading.RLock()
                self._lifecycle_locks[camera_id] = lock
            return lock

    def start(self, camera: dict, password: str) -> WorkerState:
        camera_id = int(camera["id"])
        with self._lifecycle_lock_for(camera_id):
            self.stop(camera_id)
            if camera["vendor"] not in ("dahua", "policetech"):
                raise ValueError(
                    f"P2P adapter for {camera['vendor']} is not implemented"
                )

            worker = WorkerState(
                port=self.port_for(camera_id), camera=dict(camera), password=password
            )
            with self._lock:
                self._workers[camera_id] = worker
            if os.getenv("P2P_BACKEND", "").lower() == "vendor":
                self._start_vendor_service(camera_id, worker)
            else:
                # DMSS creates separate port-mapped P2P channels for RTSP and
                # HTTP/ONVIF. Keep the local listeners independent as well:
                # a stalled ONVIF request must not block RTSP video.
                self._start_service(camera_id, worker, "rtsp")
                threading.Thread(
                    target=self._start_onvif_after_rtsp,
                    args=(camera_id, worker),
                    daemon=True,
                ).start()
            return worker

    def _start_onvif_after_rtsp(self, camera_id: int, worker: WorkerState) -> None:
        # Give the RTSP channel a short head start. This avoids sending two
        # channel negotiations at the exact same instant while still making
        # ONVIF available automatically for Surveillance Station.
        delay = max(
            0.0,
            float(os.getenv("P2P_ONVIF_START_DELAY_SECONDS", "3")),
        )
        time.sleep(delay)
        with self._lifecycle_lock_for(camera_id):
            with self._lock:
                if self._workers.get(camera_id) is not worker:
                    return
                if "onvif" not in worker.services:
                    self._start_service(camera_id, worker, "onvif")

    def _append_worker_log(self, worker: WorkerState, message: str) -> None:
        with self._log_lock:
            worker.logs.append(message)
            worker.logs[:] = worker.logs[-LOG_HISTORY_LIMIT:]

    def _start_vendor_service(self, camera_id: int, worker: WorkerState) -> None:
        """Start one Wine worker with both device ports on one P2P session."""
        rtsp_port = worker.port
        onvif_port = self.onvif_port_for(camera_id)
        env = os.environ.copy()
        env.update(
            WINEDEBUG=os.getenv("WINEDEBUG", "-all"),
            WINEPREFIX=os.getenv("WINEPREFIX", "/tmp/dahua-wine"),
            HOME=os.getenv("HOME", "/tmp"),
            P2P_VENDOR_DLL_DIR=os.getenv("P2P_VENDOR_DLL_DIR", "/vendor"),
            PYTHONUNBUFFERED="1",
            P2P_LAZY_CONNECT=os.getenv("P2P_LAZY_CONNECT", "1"),
        )
        worker_path = os.getenv(
            "P2P_VENDOR_WORKER", "/usr/local/lib/p2p_relay_multi.exe"
        )
        command = [
            os.getenv("WINE_BIN", "/usr/bin/wine"),
            worker_path,
            "--serial", worker.camera["serial"],
            "--user", worker.camera["username"],
            "--password", worker.password,
            "--dll-dir", "Z:\\vendor",
            "--map", f"554:{rtsp_port}",
        ]
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        state = ServiceState(process=process, service="rtsp")
        worker.services["rtsp"] = state
        self._append_worker_log(worker, "[P2P] Starting private Dahua P2P RTSP worker")
        threading.Thread(
            target=self._read_output, args=(camera_id, worker, state), daemon=True
        ).start()

    def _start_service(self, camera_id: int, worker: WorkerState, service: str) -> None:
        bind_port = (
            worker.port
            if service in ("rtsp", "both")
            else self.onvif_port_for(camera_id)
        )
        env = os.environ.copy()
        env.update(
            P2P_USERNAME=worker.camera["username"],
            P2P_PASSWORD=worker.password,
            P2P_IDLE_RECONNECT_SECONDS=os.getenv(
                "P2P_IDLE_RECONNECT_SECONDS", "0"
            ),
            PYTHONUNBUFFERED="1",
            P2P_SERVICE=service,
            P2P_UDP_RECEIVE_BUFFER=os.getenv(
                "P2P_UDP_RECEIVE_BUFFER", "33554432"
            ),
        )
        transport = os.getenv("P2P_TRANSPORT", "direct").strip().lower()
        if transport not in ("direct", "relay"):
            transport = "direct"
        command = [
            sys.executable,
            "-m",
            "app.vendor.dh_p2p.main",
            "--type",
            "1",
            "--service",
            # Each service owns its own authenticated P2P session, matching
            # the separate RTSP and HTTP port mappings used by DMSS.
            service,
            "--bind-port",
            str(bind_port),
            "--public-rtsp-port",
            str(worker.port),
            "--transport",
            transport,
            worker.camera["serial"],
        ]
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        state = ServiceState(process=process, service=service)
        if service == "both":
            worker.services["rtsp"] = state
            worker.services["onvif"] = state
        else:
            worker.services[service] = state
        if service == "both":
            start_message = "[P2P] Starting shared RTSP + ONVIF P2P session"
        else:
            start_message = (
                f"[P2P] [{service.upper()}] Starting "
                f"{service.upper()} P2P session"
            )
        self._append_worker_log(worker, start_message)
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
            # Keep RTSP and ONVIF output distinguishable. During recovery both
            # workers write into the same per-camera history, so an untagged
            # line makes the failed session impossible to reconstruct.
            service_tag = state.service.upper()
            self._append_worker_log(worker, f"[P2P] [{service_tag}] {line}")
            with self._lock:
                if line.startswith("READY remote="):
                    parts = dict(item.split("=", 1) for item in line.split()[1:] if "=" in item)
                    state.status = "online"
                    state.last_error = None
                    if parts.get("remote") == "554":
                        state.status = "online"
                        state.last_error = None
                    elif parts.get("remote") == "80" and "onvif" in worker.services:
                        worker.services["onvif"].status = "online"
                        worker.services["onvif"].last_error = None
                    if parts.get("remote") == "554":
                        state.online_since = time.monotonic()
                elif "RTSP media data:" in line and state.service == "rtsp":
                    # The P2P transport is ready before RTSP has produced video.
                    # Do not report RTSP online until media actually arrives.
                    state.status = "online"
                    state.last_error = None
                    state.online_since = time.monotonic()
                elif "Ready to connect" in line:
                    # Transport/listener readiness is not proof that RTSP PLAY
                    # succeeded. The RTSP media marker above is authoritative.
                    state.last_error = None
                elif "optional ONVIF channel unavailable" in line:
                    if "onvif" in worker.services:
                        worker.services["onvif"].status = "error"
                        worker.services["onvif"].last_error = "P2P port 80 unavailable"
                elif "Error:" in line or "Traceback" in line:
                    state.last_error = line

        return_code = state.process.wait()
        restart = False
        restart_delay = 1
        peer_restart_service: str | None = None
        with self._lock:
            current = self._workers.get(camera_id)
            if current is worker and any(
                value is state for value in worker.services.values()
            ):
                with self._log_lock:
                    recent_logs = worker.logs[-100:]
                if return_code == 75:
                    state.status = "connecting"
                    state.last_error = None
                    if (
                        state.online_since is not None
                        and time.monotonic() - state.online_since >= 60
                    ):
                        state.reconnect_attempt = 0
                    state.online_since = None
                    state.reconnect_attempt += 1
                    restart_delay = min(30, 2 ** min(state.reconnect_attempt, 5))
                    restart_message = (
                        "[P2P] P2P engine requested reconnect; "
                        f"rebuilding this session in {restart_delay} seconds"
                    )
                    self._append_worker_log(worker, restart_message)
                    restart = True

                    # If ONVIF detected the dead PTCP tunnel while RTSP is
                    # already stuck in connecting, rebuilding ONVIF alone
                    # leaves the camera permanently offline in Surveillance
                    # Station. Rebuild the peer RTSP session as well.
                    if state.service == "onvif":
                        rtsp_state = worker.services.get("rtsp")
                        if (
                            rtsp_state is not None
                            and rtsp_state is not state
                            and rtsp_state.status == "connecting"
                            and rtsp_state.process.poll() is None
                        ):
                            peer_restart_service = "rtsp"
                            rtsp_state.last_error = None
                            self._append_worker_log(
                                worker,
                                "[P2P] RTSP channel is still connecting; "
                                "rebuilding the RTSP session with ONVIF",
                            )
                elif return_code != 0 and any(
                    marker in line.lower()
                    for line in recent_logs
                    for marker in TRANSIENT_FAILURE_MARKERS
                ):
                    state.status = "connecting"
                    retry_message = (
                        "[P2P] Transient Easy4IP failure; retrying in "
                        f"{TRANSIENT_RETRY_SECONDS} seconds"
                    )
                    self._append_worker_log(worker, retry_message)
                    restart = True
                    restart_delay = TRANSIENT_RETRY_SECONDS
                else:
                    state.status = "stopped" if return_code == 0 else "error"
                if return_code not in (0, 75) and not state.last_error:
                    state.last_error = f"P2P worker exited with code {return_code}"

        if restart:
            threading.Event().wait(restart_delay)
            with self._lifecycle_lock_for(camera_id):
                with self._lock:
                    if (
                        self._workers.get(camera_id) is worker
                        and any(
                            value is state for value in worker.services.values()
                        )
                    ):
                        self._start_service(camera_id, worker, state.service)

                    if (
                        peer_restart_service is not None
                        and self._workers.get(camera_id) is worker
                    ):
                        peer_state = worker.services.get(peer_restart_service)
                        if peer_state is not None:
                            self._terminate_process_group(peer_state.process)
                            self._append_worker_log(
                                worker,
                                "[P2P] Restarting RTSP after the ONVIF "
                                "channel detected a stalled tunnel",
                            )
                            self._start_service(
                                camera_id, worker, peer_restart_service
                            )

    def _terminate_process_group(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    def stop(self, camera_id: int) -> None:
        with self._lifecycle_lock_for(camera_id):
            self._stop_locked(camera_id)

    def _stop_locked(self, camera_id: int) -> None:
        with self._lock:
            worker = self._workers.pop(camera_id, None)
        if not worker:
            return
        if worker.onvif_server is not None:
            try:
                worker.onvif_server.shutdown()
                worker.onvif_server.server_close()
            except Exception:
                pass
        if worker.proxy_process and worker.proxy_process.poll() is None:
            worker.proxy_process.terminate()
        states = list({id(state): state for state in worker.services.values()}.values())
        for state in states:
            self._terminate_process_group(state.process)

    def status(self, camera_id: int) -> dict:
        # Copy worker state under the manager lock, then read the log buffer
        # separately.  _read_output() appends under _log_lock before it may
        # update service state under _lock; taking the locks in the opposite
        # order here can deadlock the API after sustained camera traffic.
        with self._lock:
            worker = self._workers.get(camera_id)
            if worker is None:
                return {"status": "stopped", "last_error": None, "logs": []}
            worker_status = worker.status
            worker_last_error = worker.last_error
            worker_port = worker.port
            services = {
                name: {"status": state.status, "last_error": state.last_error}
                for name, state in worker.services.items()
            }

        with self._log_lock:
            logs = list(worker.logs[-LOG_STATUS_LIMIT:])

        return {
            "status": worker_status,
            "last_error": worker_last_error,
            "port": worker_port,
            "services": services,
            "logs": logs,
        }

    def stop_all(self) -> None:
        with self._lock:
            camera_ids = list(self._workers)
        for camera_id in camera_ids:
            self.stop(camera_id)
