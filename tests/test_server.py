import json

from fastapi.testclient import TestClient

from mandate.server import create_app
from mandate.sim import run_demo


def test_server_serves_public_files_only_and_verifies(tmp_path):
    result = run_demo(tmp_path, quiet=True)
    client = TestClient(create_app(lambda: result["state"], tmp_path))
    assert client.get("/").status_code == 200
    assert client.get("/api/state").json()["status"] == "closed"
    assert client.get("/files/D-1-order-form.pdf").status_code == 200
    assert client.get("/files/D-1-annex-acme.json").status_code == 404  # private
    assert client.get("/files/..%2F..%2Fetc%2Fpasswd").status_code == 404
    cert = json.loads((tmp_path / "D-1-certificate.json").read_text())
    assert client.post("/api/verify", json={"certificate": cert}).json()["valid"] is True
    cert["root_hash"] = "0" * 64
    assert client.post("/api/verify", json={"certificate": cert}).json()["valid"] is False
    assert client.post("/api/verify", json={"certificate": {"nope": 1}}).status_code == 400
