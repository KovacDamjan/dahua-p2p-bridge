import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field


@dataclass
class WorkerState:
    process: subprocess.Popen
    port: int
    status: str = "connecting"
    last_error: str | None = None
    logs: list[str] = field(default_factory=list)


class P2PManager:
    """Owns one isolated upstream P2P tunnel process per camera."""

    def __init__(self, first_port: int = 15540, max_cameras: int = 30):
        self.first_port = first_port
        self.max_cameras = max_cameras
        self._workers: dict[int, WorkerState] = {}
        self._lock = threading.RLock()

    def port_for(self, camera_id: int) -> int:
        if camera_id < 1 or camera_id > self.max_cameras:
            raise ValueError(f"camera id must be between 1 and {self.max_cameras}")
        return self.first_port + camera_id - 1

    def start(self, camera: dict, password: str) -> WorkerState:
        camera_id = int(camera["id"])
        self.stop(camera_id)

        if camera["vendor"] not in ("dahua", "policetech"):
            raise ValueError(f"P2P adapter for {camera['vendor']} is not implemented")

        port = self.port_for(camera_id)
        env = os.environ.copy()
        env.update(
            P2P_USERNAME=camera["username"],
            P2P_PASSWORD=password,
            P2P_BIND_PORT=str(port),
            PYTHONUNBUFFERED="1",
        )
        command = [
            sys.executable,
            "-m",
            "app.vendor.dh_p2p.main",
            "--type",
            "1",
            "--bind-port",
            str(port),
            camera["serial"],
        ]
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        state = WorkerState(process=process, port=port)
        with self._lock:
            self._workers[camera_id] = state
        threading.Thread(target=self._read_output, args=(camera_id, state), daemon=True).start()
        return state

    def _read_output(self, camera_id: int, state: WorkerState) -> None:
        assert state.process.stdout is not None
        for raw_line in state.process.stdout:
            line = raw_line.rstrip()
            with self._lock:
                state.logs.append(line)
                state.logs[:] = state.logs[-100:]
                if "Ready to connect" in line:
                    state.status = "online"
                elif line.startswith("Error:") or "Traceback" in line:
                    state.last_error = line

        return_code = state.process.wait()
        with self._lock:
            if self._workers.get(camera_id) is state:
                state.status = "stopped" if return_code == 0 else "error"
                if return_code and not state.last_error:
                    state.last_error = f"P2P worker exited with code {return_code}"

    def stop(self, camera_id: int) -> None:
        with self._lock:
            state = self._workers.pop(camera_id, None)
        if state and state.process.poll() is None:
            state.process.terminate()
            try:
                state.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                state.process.kill()

    def status(self, camera_id: int) -> dict:
        with self._lock:
            state = self._workers.get(camera_id)
            if state is None:
                return {"status": "stopped", "last_error": None, "logs": []}
            return {
                "status": state.status,
                "last_error": state.last_error,
                "port": state.port,
                "logs": state.logs[-20:],
            }

    def stop_all(self) -> None:
        with self._lock:
            camera_ids = list(self._workers)
        for camera_id in camera_ids:
            self.stop(camera_id)
