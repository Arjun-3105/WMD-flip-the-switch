"""Keeper's HTTP side: the live ledger page, the deal state, closing files, and online verification."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .ledger import verify_certificate

WEB = Path(__file__).parent / "web"
PUBLIC_FILE = re.compile(r"^D-\d+-(certificate\.json|order-form\.pdf)$")  # annexes are private and never served


class VerifyRequest(BaseModel):
    certificate: dict
    annexes: list[dict] = []


def create_app(get_state: Callable[[], dict], files_dir: Path) -> FastAPI:
    app = FastAPI(title="Mandate referee", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB / "index.html").read_text()

    @app.get("/api/state")
    def state() -> dict:
        return get_state()

    @app.get("/files/{name}")
    def files(name: str):
        if not PUBLIC_FILE.match(name):
            raise HTTPException(404)
        path = files_dir / name
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path)

    @app.post("/api/verify")
    def verify(req: VerifyRequest) -> dict:
        try:
            checks = verify_certificate(req.certificate, req.annexes)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(400, f"not a Mandate certificate: {exc}") from exc
        return {"valid": all(c.ok is not False for c in checks),
                "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks]}

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    return app
