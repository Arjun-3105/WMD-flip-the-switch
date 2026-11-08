"""A small client for the Switch agent protocol (WebSocket in, HTTP out) and the Gateway API.

Written against Switch 0.31 and its docs: docs.switchagents.ai/switch-rooms/internals/agent-protocol.
There is no official client library, so this implements registration, the event socket with
ping/pong and cursor resume, and the operations Mandate needs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import websockets

log = logging.getLogger("mandate.switch")


class SwitchError(RuntimeError):
    pass


class TakenOver(SwitchError):
    """Another client opened our connection id. Final: never reconnect after this."""


@dataclass
class Event:
    kind: str  # message | command | room_join
    room_id: str
    seq: int
    payload: dict

    @property
    def sender(self) -> str:
        return self.payload.get("sender") or self.payload.get("member") or ""

    @property
    def sender_name(self) -> str:
        return self.payload.get("sender_name") or self.payload.get("member_name") or ""

    @property
    def body(self) -> str:
        return self.payload.get("body") or ""

    @property
    def message_id(self) -> str:
        return self.payload.get("message_id") or f"{self.room_id}:{self.seq}"

    @property
    def addressed(self) -> bool:
        return bool(self.payload.get("addressed"))


@dataclass
class AgentCredentials:
    agent_id: str
    api_key: str
    name: str

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=2))
        path.chmod(0o600)

    @classmethod
    def load(cls, path: Path) -> "AgentCredentials":
        return cls(**json.loads(path.read_text()))


def register_agent(
    base_url: str,
    registration_key: str,
    name: str,
    description: str,
    display_name: str | None = None,
    connection_model: str = "always_on",
    overwrite: bool = False,
) -> AgentCredentials:
    body = {
        "name": name,
        "description": description,
        "connector_type": "mandate-protocol-client",
        "display_name": display_name or name,
        "integration_profile": {
            "connection_model": connection_model,
            "message_exchange": True,
            "pre_invocation_mediation": [],
            "post_invocation_mediation": [],
            "event_reporting": [],
        },
        "overwrite": overwrite,
    }
    r = httpx.post(
        f"{base_url}/agents", json=body, headers={"Authorization": f"Bearer {registration_key}"}, timeout=30
    )
    if r.status_code >= 300:
        raise SwitchError(f"register {name}: {r.status_code} {r.text}")
    data = r.json()
    return AgentCredentials(agent_id=data["id"], api_key=data["api_key"], name=name)


@dataclass
class AgentClient:
    base_url: str
    creds: AgentCredentials
    state_path: Path | None = None
    connection_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    cursor: int | None = None
    generation: int | None = None
    rooms: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.creds.api_key}"},
            timeout=30,
        )
        if self.state_path and self.state_path.exists():
            saved = json.loads(self.state_path.read_text())
            self.connection_id = saved.get("connection_id", self.connection_id)
            self.cursor = saved.get("cursor")
            self.generation = saved.get("generation")

    def _save_state(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"connection_id": self.connection_id, "cursor": self.cursor, "generation": self.generation})
            )

    # ---------- operations ----------
    async def op(self, _op: str, _connected: bool = False, **args: Any) -> Any:
        headers = {"X-Switch-Connection-Id": self.connection_id} if _connected else {}
        r = await self.http.post(f"/agents/{self.creds.agent_id}/ops/{_op}", json=args, headers=headers)
        if r.status_code >= 300:
            raise SwitchError(f"op {_op}: {r.status_code} {r.text}")
        return r.json().get("result")

    async def post(self, room_id: str, content: str) -> str:
        """Post into any room the agent belongs to. @mentions in the text address agents."""
        r = await self.http.post(f"/agents/{self.creds.agent_id}/message", json={"room_id": room_id, "content": content})
        if r.status_code >= 300:
            raise SwitchError(f"post to {room_id}: {r.status_code} {r.text}")
        return r.json().get("event_id", "")

    async def set_addressing(self, preset: str) -> None:
        await self.op("update_agent_detail", agent_id=self.creds.agent_id, addressing=preset)

    async def read_context(self, room_id: str, limit: int = 30) -> dict:
        return await self.op("read_context", room_id=room_id, limit=limit)

    async def close(self) -> None:
        await self.http.aclose()

    # ---------- event stream ----------
    def _ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[1]
        q = [f"connection_id={self.connection_id}", "scope=all", "filter=all"]
        q.append(f"start_from={self.cursor}" if self.cursor is not None else "start_from=head")
        if self.generation is not None:
            q.append(f"expected_generation={self.generation}")
        q += ["client=mandate", "client_version=0.1.0"]
        return f"{scheme}://{host}/agents/{self.creds.agent_id}/connection/ws?" + "&".join(q)

    async def run(self, on_event: Callable[[Event], Awaitable[None]], stop: asyncio.Event | None = None) -> None:
        """Hold the socket, answer pings first, resume from the cursor after drops."""
        backoff = 1.0
        while not (stop and stop.is_set()):
            try:
                async with websockets.connect(
                    self._ws_url(),
                    additional_headers={"Authorization": f"Bearer {self.creds.api_key}"},
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    backoff = 1.0
                    await self._pump(ws, on_event, stop)
            except TakenOver:
                log.error("%s: connection taken over by another client; stopping", self.creds.name)
                raise
            except (OSError, websockets.ConnectionClosed, SwitchError) as exc:
                if isinstance(exc, websockets.ConnectionClosed) and exc.rcvd and exc.rcvd.code in (4401, 4403):
                    raise SwitchError(f"socket refused: {exc.rcvd.code} {exc.rcvd.reason}") from exc
                log.warning("%s: socket dropped (%s); reconnecting in %.0fs", self.creds.name, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _pump(self, ws, on_event, stop) -> None:
        queue: asyncio.Queue[Event] = asyncio.Queue()

        async def worker() -> None:
            while True:
                ev = await queue.get()
                try:
                    await on_event(ev)
                except Exception:  # an error handling one event must not kill the stream
                    log.exception("handler failed on %s seq %s", ev.kind, ev.seq)
                self.cursor = max(self.cursor or 0, ev.seq)
                self._save_state()

        task = asyncio.create_task(worker())
        try:
            async for raw in ws:
                if stop and stop.is_set():
                    return
                frame = json.loads(raw)
                kind, data = frame.get("event"), frame.get("data") or {}
                if kind == "ping":  # answer pings before anything else
                    await ws.send(json.dumps({"type": "pong", "cursor": self.cursor or 0}))
                elif kind == "connection_state":
                    self.generation = data.get("generation")
                    self.rooms = data.get("rooms") or []
                    if self.cursor is None:
                        self.cursor = data.get("cursor")
                    self._save_state()
                    log.info("%s connected: %d rooms, cursor %s", self.creds.name, len(self.rooms), self.cursor)
                elif kind == "evicted":
                    if data.get("code") == "taken_over":
                        raise TakenOver(data.get("reason", ""))
                    raise SwitchError(f"evicted: {data}")
                elif kind == "refused":
                    raise SwitchError(f"refused: {data}")
                elif kind == "gap":
                    log.warning("%s: gap in event stream %s", self.creds.name, data)
                    self.cursor = data.get("resumed_at", self.cursor)
                elif kind == "subscription_changed":
                    self.rooms = data.get("rooms") or self.rooms
                elif kind in ("message", "command", "room_join"):
                    seq = frame.get("id") or data.get("sequence") or 0
                    await queue.put(Event(kind, data.get("room_id", ""), int(seq), data.get("payload") or {}))
        finally:
            task.cancel()


class Gateway:
    """The operator API (cookie auth). Used by setup to create rooms' rules and read users."""

    def __init__(self, base_url: str):
        self.http = httpx.Client(base_url=base_url, timeout=30)

    def login(self, email: str, password: str) -> None:
        for path in ("/gateway/api/auth/login", "/gateway/auth/login", "/gateway/api/login"):
            r = self.http.post(path, json={"email": email, "password": password})
            if r.status_code < 300:
                return
        raise SwitchError(f"gateway login failed: {r.status_code} {r.text[:200]}")

    def request(self, method: str, path: str, **kw) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 300:
            raise SwitchError(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None
