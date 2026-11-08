import json

from mandate.ledger import Ledger, check_chain, verify_certificate
from mandate.sim import run_demo


def test_chain_detects_edits_and_reordering():
    led = Ledger()
    for i in range(4):
        led.append("x", i=i)
    assert check_chain(led.events) == []
    edited = json.loads(json.dumps(led.events))
    edited[1]["data"]["i"] = 99
    assert check_chain(edited)
    assert check_chain([led.events[0], led.events[2], led.events[1], led.events[3]])


def test_full_demo_closes_and_verifies(tmp_path):
    result = run_demo(tmp_path, quiet=True)
    state = result["state"]
    assert state["status"] == "closed"
    kinds = [t["kind"] for t in state["timeline"]]
    assert kinds.count("accept") == 2 and "claim" in kinds and "approval" in kinds
    cert = json.loads((tmp_path / "D-1-certificate.json").read_text())
    annexes = [json.loads((tmp_path / f"D-1-annex-{c}.json").read_text()) for c in ("acme", "nimbus")]
    checks = verify_certificate(cert, annexes)
    assert all(c.ok for c in checks), [(c.name, c.detail) for c in checks if not c.ok]
    assert (tmp_path / "D-1-order-form.pdf").read_bytes()[:4] == b"%PDF"


def test_tampering_and_wrong_keys_fail(tmp_path):
    run_demo(tmp_path, quiet=True)
    cert = json.loads((tmp_path / "D-1-certificate.json").read_text())
    acme = json.loads((tmp_path / "D-1-annex-acme.json").read_text())

    forged = json.loads(json.dumps(cert))
    forged["final_terms"]["price_per_year"] -= 1000
    assert any(c.ok is False for c in verify_certificate(forged))

    wrong_annex = dict(acme, rules="price<=99999 term=12..36 net>=1 seats>=1")
    names = {c.name: c.ok for c in verify_certificate(cert, [wrong_annex])}
    assert names["acme: deal within its authority"] is False

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64
    from cryptography.hazmat.primitives import serialization
    other = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    assert verify_certificate(cert, trusted_key_b64=other)[0].ok is False


def test_demo_is_reproducible(tmp_path):
    a = run_demo(tmp_path / "a", quiet=True)["state"]["chain"]
    b = run_demo(tmp_path / "b", quiet=True)["state"]["chain"]
    strip = lambda chain: [(e["kind"], {k: v for k, v in e["data"].items() if k not in ("commitment", "mandates")}) for e in chain]
    assert strip(a) == strip(b)
