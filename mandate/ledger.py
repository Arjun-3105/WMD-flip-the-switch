"""The hash-chained deal ledger, the Ed25519-signed certificate, and offline verification.

Only public-safe facts go on the chain: offers and their verdicts, approvals bound to a terms hash,
countersigns, and *commitments* to each side's mandate (a salted hash). Each side's mandate itself
stays private; a side can prove its own compliance later by revealing its annex (rules + salt).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .terms import Mandate, Terms, parse_constraints

GENESIS = "0" * 64


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Ledger:
    events: list[dict] = field(default_factory=list)

    @property
    def head(self) -> str:
        return self.events[-1]["hash"] if self.events else GENESIS

    def append(self, kind: str, **data) -> dict:
        body = {"n": len(self.events) + 1, "kind": kind, "data": data, "prev": self.head}
        event = {**body, "hash": sha256(canonical(body))}
        self.events.append(event)
        return event


def check_chain(events: list[dict]) -> list[str]:
    problems, prev = [], GENESIS
    for i, ev in enumerate(events, 1):
        body = {k: ev[k] for k in ("n", "kind", "data", "prev")}
        if ev.get("n") != i:
            problems.append(f"event {i}: numbering broken")
        if ev.get("prev") != prev:
            problems.append(f"event {i}: does not link to event {i - 1}")
        if sha256(canonical(body)) != ev.get("hash"):
            problems.append(f"event {i} ({ev.get('kind')}): contents were changed after it was recorded")
        prev = ev.get("hash", "")
    return problems


class Signer:
    def __init__(self, key: Ed25519PrivateKey):
        self.key = key

    @classmethod
    def load_or_create(cls, path: Path) -> "Signer":
        if path.exists():
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            assert isinstance(key, Ed25519PrivateKey)
            return cls(key)
        key = Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        )
        path.chmod(0o600)
        return cls(key)

    @property
    def public_key_b64(self) -> str:
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def secret(self, label: str) -> str:
        """A deterministic secret derived from the key (used for mandate salts)."""
        raw = self.key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        return hashlib.sha256(raw + label.encode()).hexdigest()[:32]

    def sign(self, cert: dict) -> str:
        return base64.b64encode(self.key.sign(canonical(cert).encode())).decode()


def build_certificate(signer: Signer, deal: dict, ledger: Ledger, agreement: dict, issued_at: str) -> dict:
    approvals = [e["data"] for e in ledger.events if e["kind"] == "approval"]
    countersigns = [e["data"] for e in ledger.events if e["kind"] == "countersign"]
    cert = {
        "certificate_version": 1,
        "deal": deal,
        "final_offer": agreement["offer"],
        "final_terms": agreement["terms"],
        "final_terms_hash": agreement["terms_hash"],
        "mandates_in_force": agreement["mandates"],
        "approvals": approvals,
        "countersigns": countersigns,
        "chain": ledger.events,
        "root_hash": ledger.head,
        "issued_at": issued_at,
        "referee_public_key": signer.public_key_b64,
    }
    cert["signature"] = signer.sign(cert)
    return cert


@dataclass
class Check:
    name: str
    ok: bool | None  # None = not checked
    detail: str = ""


def verify_certificate(cert: dict, annexes: list[dict] | None = None, trusted_key_b64: str | None = None) -> list[Check]:
    checks: list[Check] = []
    body = {k: v for k, v in cert.items() if k != "signature"}

    key_b64 = trusted_key_b64 or cert.get("referee_public_key", "")
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64))
        pub.verify(base64.b64decode(cert.get("signature", "")), canonical(body).encode())
        sig_ok, sig_detail = True, "signed by the referee key " + key_b64[:12] + "…"
        if not trusted_key_b64:
            sig_detail += " (key taken from the certificate; pass --key to pin a known key)"
    except (InvalidSignature, ValueError, TypeError):
        sig_ok, sig_detail = False, "signature does not match the certificate contents"
    checks.append(Check("Referee signature", sig_ok, sig_detail))

    chain = cert.get("chain", [])
    problems = check_chain(chain)
    root_ok = bool(chain) and chain[-1].get("hash") == cert.get("root_hash")
    checks.append(Check("Hash chain", not problems and root_ok, "; ".join(problems) or f"{len(chain)} events link up to root {cert.get('root_hash','')[:12]}…"))

    terms = Terms.from_dict(cert["final_terms"])
    agreed = [e for e in chain if e["kind"] == "agreement"]
    agreement_ok = bool(agreed) and agreed[-1]["data"]["terms_hash"] == terms.digest() == cert.get("final_terms_hash")
    checks.append(Check("Final terms match the recorded agreement", agreement_ok, terms.describe()))

    companies = [cert["deal"]["buyer"]["key"], cert["deal"]["seller"]["key"]]
    signed = {c["company"] for c in cert.get("countersigns", [])}
    missing = [c for c in companies if c not in signed]
    checks.append(Check("Both principals countersigned", not missing, "missing: " + ", ".join(missing) if missing else ", ".join(
        f"{c['company']} by {c['principal']}" for c in cert.get("countersigns", []))))

    annex_by_company = {a["company"]: a for a in annexes or []}
    for company in companies:
        mif = cert["mandates_in_force"].get(company, {})
        approved = [a for a in cert.get("approvals", []) if a["company"] == company and a["terms_hash"] == terms.digest()]
        annex = annex_by_company.get(company)
        name = f"{company}: deal within its authority"
        if annex is None:
            if approved:
                checks.append(Check(name, True, f"approved by principal {approved[-1]['principal']} for these exact terms (annex not supplied, so the mandate itself was not re-checked)"))
            else:
                checks.append(Check(name, None, "not checked: supply this side's annex to re-check against its mandate"))
            continue
        mandate = Mandate(company, annex["label"], parse_constraints(annex["rules"]), salt=annex["salt"])
        if mandate.commitment() != mif.get("commitment"):
            checks.append(Check(name, False, "annex does not match the mandate committed on the ledger"))
            continue
        violations = mandate.evaluate(terms)
        if not violations:
            checks.append(Check(name, True, f"within mandate {annex['label']}"))
        elif approved:
            checks.append(Check(name, True, f"outside mandate {annex['label']} ({violations[0].detail}) but approved by principal {approved[-1]['principal']} for these exact terms"))
        else:
            checks.append(Check(name, False, f"outside mandate {annex['label']} with no approval: " + "; ".join(v.detail for v in violations)))
    return checks
