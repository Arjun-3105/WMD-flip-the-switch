"""An in-memory stand-in for a Switch server, so the whole story runs with no server and no API key.

It keeps per-room append-only logs, applies the same room-scoped addressing rules the live setup
configures, and drives the real Engine and the real NegotiatorAgent code.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import write_closing_files
from .engine import Engine, Incoming
from .ledger import Signer
from .negotiator import BUYER_SCRIPT, SELLER_SCRIPT, NegotiatorAgent, ScriptedBrain
from .scenario import DIRECTOR, demo_config

MENTION = re.compile(r"@([a-z0-9][a-z0-9._-]*)", re.I)
START_MS = 1794045600000  # 2026-11-07 10:00 UTC, fixed so runs are reproducible


@dataclass
class Posted:
    room: str
    sender: str
    text: str
    seq: int
    message_id: str
    ts: int
    refused: list[str] = field(default_factory=list)


class SimSwitch:
    def __init__(self, cfg, members: dict[str, set[str]], rules: dict[str, list[dict]], owners: dict[str, str]):
        self.cfg, self.members, self.rules, self.owners = cfg, members, rules, owners
        self.logs: dict[str, list[Posted]] = {r: [] for r in members}
        self.clock = START_MS
        self.count = 0

    def addressing_allows(self, agent: str, room: str, sender: str) -> bool:
        """Mirror of Switch's rule check: a rule matches when room AND sender match; any rule admits."""
        for rule in self.rules.get(agent, []):
            if rule.get("owner") and self.owners.get(agent) == sender:
                return True
            rooms = rule.get("rooms", "*")
            senders = rule.get("senders", "*")
            if (rooms == "*" or room in rooms) and (senders == "*" or sender in senders):
                return True
        return False

    def post(self, room: str, sender: str, text: str) -> Posted:
        self.count += 1
        self.clock += 7000
        log = self.logs[room]
        msg = Posted(room, sender, text, len(log) + 1, f"$sim{self.count:04d}", self.clock)
        log.append(msg)
        return msg


def run_demo(out_dir: Path, quiet: bool = False, brains: dict | None = None) -> dict:
    cfg = demo_config()
    shared, acme_room, nimbus_room = cfg.shared_room, cfg.buyer.private_room, cfg.seller.private_room
    members = {
        shared: {"keeper", "acme-buyer", "nimbus-seller", "priya", "sam"},
        acme_room: {"keeper", "acme-buyer", "priya"},
        nimbus_room: {"keeper", "nimbus-seller", "sam"},
    }
    owners = {"acme-buyer": "priya", "nimbus-seller": "sam"}
    # Same shape as scripts/setup_live.py: owner anywhere; in the deal room, people plus keeper and the other agent.
    rules = {
        "acme-buyer": [{"owner": True}, {"rooms": [shared], "senders": ["keeper", "nimbus-seller", "priya", "sam"]}],
        "nimbus-seller": [{"owner": True}, {"rooms": [shared], "senders": ["keeper", "acme-buyer", "priya", "sam"]}],
    }
    sw = SimSwitch(cfg, members, rules, owners)
    signer = Signer.load_or_create(out_dir / "keeper_ed25519.pem")
    engine = Engine(cfg, signer, on_close=lambda cert, annexes: write_closing_files(out_dir, cert, annexes))
    brains = brains or {"acme-buyer": ScriptedBrain(BUYER_SCRIPT), "nimbus-seller": ScriptedBrain(SELLER_SCRIPT)}
    agents = {
        "acme-buyer": NegotiatorAgent("acme-buyer", "acme", shared, brains["acme-buyer"]),
        "nimbus-seller": NegotiatorAgent("nimbus-seller", "nimbus", shared, brains["nimbus-seller"]),
    }
    room_key = {"shared": shared, "acme": acme_room, "nimbus": nimbus_room}
    transcript: list[dict] = []
    queue: deque[tuple[str, str, str]] = deque()

    def deliver(room: str, sender: str, text: str) -> None:
        msg = sw.post(room, sender, text)
        transcript.append({"room": room, "sender": sender, "text": text, "seq": msg.seq, "ts": msg.ts, "refused": msg.refused})
        if not quiet:
            show(room, sender, text, cfg)
        for reply in engine.handle(Incoming(room, sender, text, msg.message_id, sender_id=f"@sim-{sender}", seq=msg.seq, timestamp_ms=msg.ts)):
            queue.append((reply.room_id, cfg.keeper, reply.text))
        for name, agent in agents.items():
            if name == sender or name not in members[room]:
                continue
            agent.observe(room, sender, text)
            if f"@{name}" in text:
                if not sw.addressing_allows(name, room, sender):
                    note = f"@{sender} I only take instructions from my owner or people in our deal room."
                    msg.refused.append(name)
                    queue.append((room, name, note))
                    continue
                move = agent.on_addressed(room, sender, text)
                if move:
                    queue.append((room, name, move))

    def drain() -> None:
        while queue:
            deliver(*queue.popleft())

    for who, room, text in DIRECTOR:
        drain()
        deliver(room_key[room], who, text.replace("{buyer}", cfg.buyer.agent))
    drain()

    out_dir.mkdir(parents=True, exist_ok=True)
    state = engine.state()
    (out_dir / "transcript.json").write_text(json.dumps(transcript, indent=2))
    (out_dir / "state.json").write_text(json.dumps(state, indent=2))
    return {"engine": engine, "state": state, "transcript": transcript}


COLORS = {"deal-acme-nimbus": "\033[38;5;111m", "acme-private": "\033[38;5;179m", "nimbus-private": "\033[38;5;150m"}
WHO = {"keeper": "\033[1;37m", "priya": "\033[1;33m", "sam": "\033[1;32m", "acme-buyer": "\033[33m", "nimbus-seller": "\033[32m"}
RESET, DIM = "\033[0m", "\033[2m"


def show(room: str, sender: str, text: str, cfg) -> None:
    label = {cfg.shared_room: "#deal-acme-nimbus", cfg.buyer.private_room: "#acme-private",
             cfg.seller.private_room: "#nimbus-private"}.get(room, room)
    body = re.sub(r"```offer\n(.*?)\n```", lambda m: "OFFER " + m.group(1), text, flags=re.S)
    body = body.replace("**", "")
    pad = " " * 28
    lines = body.splitlines() or [""]
    print(f"{COLORS.get(room, '')}{label:<18}{RESET}{WHO.get(sender, '')}{sender:>10}{RESET}  {lines[0]}")
    for line in lines[1:]:
        print(f"{pad}  {line}" if line.strip() else "", flush=True)
