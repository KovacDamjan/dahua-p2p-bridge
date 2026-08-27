import importlib

from fastapi.testclient import TestClient


def make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("APP_SECRET_KEY", "this-is-a-long-test-secret-key")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    import app.main
    importlib.reload(app.main)
    monkeypatch.setattr(app.main.p2p_manager, "start", lambda camera, password: None)
    return TestClient(app.main.app), app.main


def test_health(tmp_path, monkeypatch):
    client, _ = make_client(tmp_path, monkeypatch)
    with client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_camera_password_is_not_returned_or_plaintext(tmp_path, monkeypatch):
    client, module = make_client(tmp_path, monkeypatch)
    auth = ("admin", "test-password")
    payload = {"name":"Vhod","vendor":"dahua","serial":"TEST1234","username":"admin","password":"camera-secret"}
    with client:
        created = client.post("/api/cameras", json=payload, auth=auth)
        listed = client.get("/api/cameras", auth=auth)
    assert created.status_code == 201
    assert "password" not in created.json()
    assert "password_enc" not in created.json()
    assert listed.status_code == 200
    with module.db() as connection:
        stored = connection.execute("SELECT password_enc FROM cameras").fetchone()[0]
    assert stored != "camera-secret"
