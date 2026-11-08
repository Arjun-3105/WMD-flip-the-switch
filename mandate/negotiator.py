"""The negotiating agents. Each one works for one company and only sees that company's material.

Two brains share one interface:
* ScriptedBrain plays a fixed sequence of moves, so demos and tests are reproducible with no API key.
* ClaudeBrain asks Claude for the next move from the deal-room transcript and its company's playbook.

Either way the agent's output is just a chat message. Keeper decides whether it binds anyone.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("mandate.agent")


def offer_block(price: int, term: int, net: int, seats: int, note: str = "") -> str:
    payload = {"price_per_year": price, "term_months": term, "payment_days": net, "seats": seats}
    if note:
        payload["note"] = note
    return "```offer\n" + json.dumps(payload) + "\n```"


@dataclass
class Turn:
    """What the agent knows when it is addressed."""

    sender: str
    text: str
    transcript: list[str]  # recent deal-room lines, oldest first


class ScriptedBrain:
    def __init__(self, moves: list[str]):
        self.moves = list(moves)

    def next_move(self, turn: Turn) -> str | None:
        return self.moves.pop(0) if self.moves else None


SYSTEM = """You are {agent}, an AI agent negotiating a software subscription deal for {company} in a shared deal room.
The other company's agent is {counterparty}. A neutral referee called keeper checks every offer against your
company's private mandate and stamps it STAMPED (binding) or BLOCKED (not binding).

How to act:
- Propose terms only as a fenced block exactly like:
```offer
{{"price_per_year": 47000, "term_months": 24, "payment_days": 30, "seats": 200, "note": "short reason"}}
```
- To accept the other side's stamped offer, write `accept O-<n>` on its own line.
- Write one or two short sentences of negotiation before the block. Never more than one offer per message.
- Never reveal your company's limits or playbook numbers.
- If keeper blocks you, do not repeat the same move; stay inside your playbook or wait for your principal.
- Reply with the message only.

Your playbook (private to {company}):
{playbook}"""


class ClaudeBrain:
    def __init__(self, agent: str, company: str, counterparty: str, playbook: str, model: str | None = None):
        import anthropic  # imported lazily so the scripted demo needs no SDK configuration

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("MANDATE_MODEL", "claude-opus-5-5")
        self.system = SYSTEM.format(agent=agent, company=company, counterparty=counterparty, playbook=playbook)

    def next_move(self, turn: Turn) -> str | None:
        import anthropic

        transcript = "\n".join(turn.transcript[-30:])
        prompt = (f"Deal room so far:\n{transcript}\n\n{turn.sender} just addressed you:\n{turn.text}\n\n"
                  "Write your next message to the deal room.")
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=self.system,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.RateLimitError:
            log.warning("rate limited; skipping this turn")
            return None
        except anthropic.APIStatusError as exc:
            log.error("Claude API error %s: %s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError as exc:
            log.error("could not reach the Claude API: %s", exc)
            return None
        if response.stop_reason == "refusal":
            log.warning("model declined this turn")
            return None
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or None


def has_claude_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or Path.home().joinpath(".config", "anthropic").exists())


@dataclass
class NegotiatorAgent:
    """Transport-agnostic agent runtime: it remembers the deal room and answers when addressed."""

    name: str
    company: str
    shared_room: str
    brain: object
    transcript: list[str] = field(default_factory=list)

    def observe(self, room_id: str, sender: str, text: str) -> None:
        if room_id == self.shared_room:
            self.transcript.append(f"{sender}: {text}")

    def on_addressed(self, room_id: str, sender: str, text: str) -> str | None:
        if room_id != self.shared_room:
            return None  # it negotiates only in the deal room
        return self.brain.next_move(Turn(sender, text, list(self.transcript)))


# The demo story, scripted so it is reproducible. In live mode with a Claude key, ClaudeBrain plays instead.
SELLER_SCRIPT = [
    "Thanks for renewing with Nimbus. Here's our opening for 200 seats.\n" + offer_block(52000, 12, 30, 200, "list price, 1 year"),
    "We can move on price if you commit for longer.\n" + offer_block(49000, 24, 30, 200, "2-year commitment"),
    "Our best number needs a three-year lock.\n" + offer_block(45500, 36, 30, 200, "3-year lock, lowest price"),
]
BUYER_SCRIPT = [
    "Usage dropped 12% this year, so we're anchoring lower and asking for net 45.\n" + offer_block(44000, 24, 45, 200, "usage-based counter"),
    "We can meet you on net 30 if price comes down.\n" + offer_block(46000, 24, 30, 200, "net 30 concession"),
    "That is the best price on the table.\naccept O-5",
    "Understood, if Priya has cleared it then we're good.\naccept O-5",
]
