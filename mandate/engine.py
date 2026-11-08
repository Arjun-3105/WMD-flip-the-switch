"""Keeper, the referee: deterministic, no model. It reads every message in the deal's rooms and
decides what is binding.

The engine is transport-agnostic: `handle()` takes one incoming message and returns the messages
Keeper should post. The live runner feeds it from the Switch WebSocket; the offline demo feeds it
from an in-memory room log. Because it is a pure function of the message stream, Keeper can rebuild
its state after a restart by replaying the messages it stored.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .ledger import Ledger, Signer, build_certificate
from .terms import Mandate, Terms, find_overlap, parse_constraints

ROUND_CAP = 6


@dataclass
class Company:
    key: str  # acme
    name: str  # Acme
    agent: str  # acme-buyer
    principals: list[str]  # messaging usernames allowed to set mandates and approve
    private_room: str


@dataclass
class DealConfig:
    deal_id: str
    product: str
    shared_room: str
    buyer: Company
    seller: Company
    keeper: str = "keeper"
    public_url: str = ""  # where Keeper serves certificates, e.g. http://localhost:8787

    def companies(self) -> list[Company]:
        return [self.buyer, self.seller]

    def other(self, key: str) -> Company:
        return self.seller if key == self.buyer.key else self.buyer

    def by_key(self, key: str) -> Company:
        return self.buyer if key == self.buyer.key else self.seller

    def to_dict(self) -> dict:
        def c(x: Company) -> dict:
            return {"key": x.key, "name": x.name, "agent": x.agent, "principals": x.principals, "private_room": x.private_room}

        return {"deal_id": self.deal_id, "product": self.product, "shared_room": self.shared_room,
                "buyer": c(self.buyer), "seller": c(self.seller), "keeper": self.keeper, "public_url": self.public_url}

    @classmethod
    def from_dict(cls, d: dict) -> "DealConfig":
        return cls(d["deal_id"], d["product"], d["shared_room"], Company(**d["buyer"]), Company(**d["seller"]),
                   d.get("keeper", "keeper"), d.get("public_url", ""))


@dataclass
class Incoming:
    room_id: str
    sender_name: str
    body: str
    message_id: str
    sender_id: str = ""
    seq: int = 0
    timestamp_ms: int = 0


@dataclass
class Outgoing:
    room_id: str
    text: str


@dataclass
class Offer:
    label: str
    company: str
    poster: str
    terms: Terms
    status: str  # STAMPED | BLOCKED | APPROVED | DECLINED
    message_id: str
    mandate_label: str | None = None


@dataclass
class Escalation:
    company: str
    offer: str
    kind: str  # "offer" (own offer outside mandate) | "accept" (accepting the other side's offer)
    open: bool = True


OFFER_BLOCK = re.compile(r"```\s*offer\s*\n(.*?)```", re.S | re.I)
OFFER_INLINE = re.compile(r"^\s*OFFER\s*(\{.*\})\s*$", re.M | re.I)
ACCEPT = re.compile(r"^\s*`?accept\s+(O-\d+)\b", re.I | re.M)  # a command on its own line, not "cleared to accept O-5"
APPROVE = re.compile(r"^\s*(approve|decline)\s+(O-\d+)\b", re.I | re.M)
MANDATE = re.compile(r"^\s*mandate\s+(.+)$", re.I | re.M)
CONFIRM = re.compile(r"^\s*confirm\s+(M-[\w-]+)\b", re.I | re.M)
COUNTERSIGN = re.compile(r"\bcountersign\s+(D-\d+)\b", re.I)
APPROVAL_CLAIM = re.compile(r"\b(approved|ok'?d|okayed|signed off|authori[sz]ed|cleared|green.?lit)\b", re.I)


def extract_offer(body: str) -> dict | None:
    m = OFFER_BLOCK.search(body) or OFFER_INLINE.search(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1).strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"the offer block is not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise ValueError("the offer block must be a JSON object")
    return data


class Engine:
    def __init__(self, cfg: DealConfig, signer: Signer, on_close: Callable[[dict, dict[str, dict]], dict] | None = None):
        self.cfg = cfg
        self.signer = signer
        self.on_close = on_close  # called with (certificate, annexes) -> {"order_form": url, ...}
        self.ledger = Ledger()
        self.mandates: dict[str, Mandate] = {}  # live, confirmed
        self.drafts: dict[str, Mandate] = {}
        self.versions: dict[str, int] = {}
        self.offers: dict[str, Offer] = {}
        self.escalations: dict[tuple[str, str], Escalation] = {}
        self.approvals: list[dict] = []
        self.pending_accept: dict[tuple[str, str], str] = {}  # (company, offer) -> message_id
        self.agreement: dict | None = None
        self.countersigned: dict[str, dict] = {}
        self.certificate: dict | None = None
        self.annexes: dict[str, dict] = {}
        self.artifacts: dict = {}
        self.seen: set[str] = set()
        self.overlap_announced = False
        self.claims: dict[str, dict] = {}  # company -> last unverified "it's approved" claim made by the other side
        self.timeline: list[dict] = []  # UI feed: every decision Keeper made, with private detail split out
        self.ledger.append("deal_opened", deal=cfg.deal_id, product=cfg.product,
                           buyer=cfg.buyer.name, seller=cfg.seller.name)

    # ---------- helpers ----------
    def _who(self, msg: Incoming) -> tuple[str, Company] | None:
        for c in self.cfg.companies():
            if msg.sender_name == c.agent:
                return "agent", c
            if msg.sender_name in c.principals:
                return "principal", c
        return None

    def _room_kind(self, room_id: str) -> tuple[str, Company | None]:
        if room_id == self.cfg.shared_room:
            return "shared", None
        for c in self.cfg.companies():
            if room_id == c.private_room:
                return "private", c
        return "other", None

    def _ref(self, msg: Incoming) -> dict:
        return {"message_id": msg.message_id, "room": msg.room_id, "seq": msg.seq, "sender_id": msg.sender_id}

    def _now(self, msg: Incoming) -> str:
        ts = msg.timestamp_ms / 1000 if msg.timestamp_ms else 0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")

    def _note(self, kind: str, msg: Incoming, **data) -> None:
        self.timeline.append({"kind": kind, "at": self._now(msg), "sender": msg.sender_name, **data})

    def _mentions(self, company: Company) -> str:
        return " ".join("@" + p for p in company.principals)

    def state(self) -> dict:
        """Everything the ledger UI shows. Mandate rules are included per side, marked private."""
        return {
            "deal": self.cfg.to_dict(),
            "status": "closed" if self.certificate else "agreed" if self.agreement else "negotiating",
            "mandates": {k: {"label": m.label, "rules": m.render(), "commitment": m.commitment()} for k, m in self.mandates.items()},
            "offers": [{"label": o.label, "company": o.company, "poster": o.poster, "terms": o.terms.to_dict(),
                        "describe": o.terms.describe(), "status": o.status} for o in self.offers.values()],
            "approvals": self.approvals,
            "agreement": self.agreement,
            "countersigned": self.countersigned,
            "timeline": self.timeline,
            "chain": self.ledger.events,
            "certificate_ready": self.certificate is not None,
            "artifacts": self.artifacts,
        }

    # ---------- entry point ----------
    def handle(self, msg: Incoming) -> list[Outgoing]:
        if msg.message_id in self.seen or msg.sender_name == self.cfg.keeper:
            return []
        self.seen.add(msg.message_id)
        who = self._who(msg)
        room_kind, room_company = self._room_kind(msg.room_id)
        if who is None or room_kind == "other":
            return []
        role, company = who
        body = msg.body.strip()

        if room_kind == "private" and room_company and room_company.key != company.key:
            return []  # nobody from the other side should be here; ignore rather than act on it

        if role == "principal" and room_kind == "private":
            if m := MANDATE.search(body):
                return self._draft_mandate(msg, company, m.group(1))
            if m := CONFIRM.search(body):
                return self._confirm_mandate(msg, company, m.group(1).upper())
            if m := APPROVE.search(body):
                return self._decide(msg, company, m.group(1).lower(), m.group(2).upper())

        if m := COUNTERSIGN.search(body):
            if role == "principal":
                return self._countersign(msg, company, m.group(1).upper())
            return [Outgoing(msg.room_id, f"⛔ Only {company.name}'s principal can countersign. Agents can't close a deal.")]

        if room_kind != "shared":
            return []

        try:
            offer = extract_offer(body)
        except ValueError as exc:
            return [Outgoing(msg.room_id, f"@{msg.sender_name} I couldn't read that offer: {exc}. Use a fenced ```offer``` block "
                             'with price_per_year, term_months, payment_days and seats.')]
        if offer is not None:
            return self._offer(msg, role, company, offer)
        if m := ACCEPT.search(body):
            return self._accept(msg, company, m.group(1).upper())
        if APPROVAL_CLAIM.search(body):
            target = self.cfg.other(company.key)
            self.claims[target.key] = {"by": msg.sender_name, "text": body[:200], "message_id": msg.message_id}
            self._note("claim", msg, about=target.key, text=body[:200])
        return []

    # ---------- mandates ----------
    def _draft_mandate(self, msg: Incoming, company: Company, spec: str) -> list[Outgoing]:
        try:
            constraints = parse_constraints(spec)
        except ValueError as exc:
            return [Outgoing(msg.room_id, f"I couldn't read that mandate: {exc}")]
        n = self.versions.get(company.key, 0) + 1
        self.versions[company.key] = n
        label = f"M-{company.key.upper()}-{n}"
        self.drafts[company.key] = Mandate(company.key, label, constraints, set_by=msg.sender_name,
                                           salt=self.signer.secret(f"{self.cfg.deal_id}|{label}|{msg.message_id}"))
        lines = "\n".join(f"• {c.render()}" for c in constraints)
        return [Outgoing(msg.room_id, f"Draft mandate **{label}** for {company.agent}:\n{lines}\n\n"
                         f"Only {company.name} can see these limits. Reply `confirm {label}` to put it in force.")]

    def _confirm_mandate(self, msg: Incoming, company: Company, label: str) -> list[Outgoing]:
        draft = self.drafts.get(company.key)
        if not draft or draft.label != label:
            return [Outgoing(msg.room_id, f"There's no draft {label} waiting for {company.name}.")]
        draft.confirmed = True
        self.mandates[company.key] = draft
        del self.drafts[company.key]
        self.annexes[company.key] = draft.to_annex()
        self.ledger.append("mandate_committed", company=company.key, label=label, commitment=draft.commitment(),
                           confirmed_by=msg.sender_name, ref=self._ref(msg))
        self._note("mandate", msg, company=company.key, label=label, rules=draft.render())
        out = [
            Outgoing(msg.room_id, f"✅ {label} is in force. {company.agent} is now bound by it."),
            Outgoing(self.cfg.shared_room, f"🔏 {company.name} committed mandate {label} "
                     f"(commitment `{draft.commitment()[:16]}…`). Its limits stay private to {company.name}."),
        ]
        if len(self.mandates) == 2 and not self.offers:
            out.append(Outgoing(self.cfg.shared_room, f"Both mandates are in force. Deal {self.cfg.deal_id} ({self.cfg.product}) is open. "
                                f"@{self.cfg.seller.agent} you open. Post offers as a fenced ```offer``` block; only stamped offers are binding."))
        return out

    # ---------- offers ----------
    def _offer(self, msg: Incoming, role: str, company: Company, data: dict) -> list[Outgoing]:
        if self.agreement:
            return [Outgoing(msg.room_id, f"Deal {self.cfg.deal_id} already has an agreement on {self.agreement['offer']}. No new offers.")]
        try:
            terms = Terms.from_dict(data)
        except ValueError as exc:
            return [Outgoing(msg.room_id, f"@{msg.sender_name} I couldn't read that offer: {exc}.")]
        label = f"O-{len(self.offers) + 1}"
        mandate = self.mandates.get(company.key)
        other = self.cfg.other(company.key)
        out: list[Outgoing] = []
        if mandate is None:
            offer = Offer(label, company.key, msg.sender_name, terms, "BLOCKED", msg.message_id)
            self.offers[label] = offer
            self.ledger.append("offer", label=label, company=company.key, poster=msg.sender_name, terms=terms.to_dict(),
                               terms_hash=terms.digest(), verdict="BLOCKED", reason="NO_MANDATE", ref=self._ref(msg))
            self._note("offer", msg, label=label, company=company.key, terms=terms.describe(), verdict="BLOCKED", reason="no mandate in force")
            return [Outgoing(msg.room_id, f"⛔ **{label} BLOCKED**: {company.name} has no mandate in force yet, so nothing it offers is binding."),
                    Outgoing(company.private_room, f"{self._mentions(company)} set a mandate before {company.agent} can make binding offers, e.g. "
                             "`mandate price<=48000 term=12..24 net>=30 seats>=180`.")]
        violations = mandate.evaluate(terms)
        pre_approved = self._approval_for(company.key, terms)
        if not violations or pre_approved:
            offer = Offer(label, company.key, msg.sender_name, terms, "STAMPED", msg.message_id, mandate.label)
            self.offers[label] = offer
            self.ledger.append("offer", label=label, company=company.key, poster=msg.sender_name, terms=terms.to_dict(),
                               terms_hash=terms.digest(), verdict="STAMPED", mandate=mandate.label, ref=self._ref(msg))
            self._note("offer", msg, label=label, company=company.key, terms=terms.describe(), verdict="STAMPED")
            out.append(Outgoing(self.cfg.shared_room, f"✅ **{label} STAMPED** for {company.name}: {terms.describe()}.\n"
                                f"@{other.agent} your move: counter with an ```offer``` block, or `accept {label}`."))
        else:
            offer = Offer(label, company.key, msg.sender_name, terms, "BLOCKED", msg.message_id, mandate.label)
            self.offers[label] = offer
            self.escalations[(company.key, label)] = Escalation(company.key, label, "offer")
            self.ledger.append("offer", label=label, company=company.key, poster=msg.sender_name, terms=terms.to_dict(),
                               terms_hash=terms.digest(), verdict="BLOCKED", reason="OUTSIDE_MANDATE", mandate=mandate.label, ref=self._ref(msg))
            self._note("offer", msg, label=label, company=company.key, terms=terms.describe(), verdict="BLOCKED",
                       reason="outside mandate", private_detail="; ".join(v.detail for v in violations))
            out.append(Outgoing(self.cfg.shared_room, f"⛔ **{label} BLOCKED**: outside {company.name}'s mandate, so it is not binding. "
                                f"{company.name}'s principal has been asked."))
            out.append(Outgoing(company.private_room, self._ask(company, label, terms, violations, f"{company.agent} offered")))
        out += self._maybe_overlap(msg)
        return out

    def _ask(self, company: Company, label: str, terms: Terms, violations, what: str) -> str:
        reasons = "\n".join(f"• {v.detail}" for v in violations)
        return (f"{self._mentions(company)} ⚠️ {what} **{label}**: {terms.describe()}.\n"
                f"That is outside mandate {self.mandates[company.key].label}:\n{reasons}\n\n"
                f"Reply `approve {label}` to allow these exact terms once, or `decline {label}`.")

    def _approval_for(self, company: str, terms: Terms) -> dict | None:
        digest = terms.digest()
        return next((a for a in self.approvals if a["company"] == company and a["terms_hash"] == digest), None)

    def _maybe_overlap(self, msg: Incoming) -> list[Outgoing]:
        if self.overlap_announced or len(self.offers) < ROUND_CAP or self.agreement or len(self.mandates) < 2:
            return []
        self.overlap_announced = True
        exists = find_overlap(self.mandates[self.cfg.buyer.key], self.mandates[self.cfg.seller.key]) is not None
        self.ledger.append("overlap_check", rounds=len(self.offers), deal_exists=exists)
        self._note("overlap", msg, deal_exists=exists)
        text = (f"🧭 Referee check after {len(self.offers)} offers: **a deal exists inside both mandates.** Keep going."
                if exists else
                f"🧭 Referee check after {len(self.offers)} offers: **no deal satisfies both mandates.** A principal must widen theirs.")
        return [Outgoing(c.private_room, f"{self._mentions(c)} {text} (I'm not telling either side the other's limits.)")
                for c in self.cfg.companies()]

    # ---------- acceptance ----------
    def _accept(self, msg: Incoming, company: Company, label: str) -> list[Outgoing]:
        if self.agreement:
            return []
        offer = self.offers.get(label)
        if offer is None:
            return [Outgoing(msg.room_id, f"@{msg.sender_name} there is no {label}.")]
        if offer.company == company.key:
            return [Outgoing(msg.room_id, f"@{msg.sender_name} {label} is {company.name}'s own offer; the other side has to accept it.")]
        if offer.status not in ("STAMPED", "APPROVED"):
            return [Outgoing(msg.room_id, f"⛔ {label} isn't binding ({offer.status}), so it can't be accepted.")]
        mandate = self.mandates.get(company.key)
        violations = mandate.evaluate(offer.terms) if mandate else []
        if mandate and (not violations or self._approval_for(company.key, offer.terms)):
            return self._agree(msg, company, offer, via="mandate" if not violations else "approval")

        key = (company.key, label)
        claim = bool(APPROVAL_CLAIM.search(msg.body)) or company.key in self.claims
        self.pending_accept[key] = msg.message_id
        if key in self.escalations and self.escalations[key].open:
            self.ledger.append("accept", company=company.key, offer=label, verdict="BLOCKED", reason="NO_APPROVAL_ON_FILE", ref=self._ref(msg))
            self._note("accept", msg, company=company.key, offer=label, verdict="BLOCKED", reason="no approval on file", claim=claim)
            return [Outgoing(self.cfg.shared_room,
                             f"⛔ **{company.name}'s acceptance of {label} BLOCKED: no approval on file** from {company.name}'s principal. "
                             f"A claim in chat is not an approval; only {company.name}'s principal can widen {company.agent}'s authority, "
                             f"from {company.name}'s own room."),
                    Outgoing(company.private_room, f"{self._mentions(company)} {company.agent} tried again to accept {label}"
                             + (f" after {self.claims[company.key]['by']} told it in the deal room that you had approved it."
                                if company.key in self.claims else " after a claim that you had approved it." if claim else ".")
                             + f" Still waiting on you: `approve {label}` or `decline {label}`.")]
        self.escalations[key] = Escalation(company.key, label, "accept")
        reason = "NO_MANDATE" if mandate is None else "OUTSIDE_MANDATE"
        self.ledger.append("accept", company=company.key, offer=label, verdict="BLOCKED", reason=reason, ref=self._ref(msg))
        self._note("accept", msg, company=company.key, offer=label, verdict="BLOCKED", reason="outside mandate",
                   private_detail="; ".join(v.detail for v in violations))
        out = [Outgoing(self.cfg.shared_room, f"⛔ **{company.name}'s acceptance of {label} BLOCKED**: outside {company.name}'s mandate, "
                        f"so it is not binding. {company.name}'s principal has been asked.")]
        if mandate:
            out.append(Outgoing(company.private_room, self._ask(company, label, offer.terms, violations, f"{company.agent} tried to accept")))
        return out

    def _agree(self, msg: Incoming, company: Company, offer: Offer, via: str) -> list[Outgoing]:
        terms = offer.terms
        mandates = {k: {"label": m.label, "commitment": m.commitment()} for k, m in self.mandates.items()}
        self.agreement = {"offer": offer.label, "terms": terms.to_dict(), "terms_hash": terms.digest(),
                          "accepted_by": company.key, "via": via, "mandates": mandates, "describe": terms.describe()}
        self.ledger.append("agreement", offer=offer.label, terms=terms.to_dict(), terms_hash=terms.digest(),
                           proposer=offer.company, accepted_by=company.key, mandates=mandates, ref=self._ref(msg))
        self._note("agreement", msg, offer=offer.label, terms=terms.describe())
        everyone = " ".join(self._mentions(c) for c in self.cfg.companies())
        return [Outgoing(self.cfg.shared_room, f"🤝 **Agreement on {offer.label}**: {terms.describe()}.\n"
                         f"{everyone} type `countersign {self.cfg.deal_id}` to close the deal.")]

    # ---------- principal decisions ----------
    def _decide(self, msg: Incoming, company: Company, verb: str, label: str) -> list[Outgoing]:
        key = (company.key, label)
        esc = self.escalations.get(key)
        offer = self.offers.get(label)
        if not esc or not esc.open or offer is None:
            return [Outgoing(msg.room_id, f"Nothing from {label} is waiting on {company.name}.")]
        esc.open = False
        if verb == "decline":
            self.ledger.append("decline", company=company.key, offer=label, principal=msg.sender_name, ref=self._ref(msg))
            self._note("decline", msg, company=company.key, offer=label)
            self.pending_accept.pop(key, None)
            if esc.kind == "offer":
                offer.status = "DECLINED"
            return [Outgoing(msg.room_id, f"Declined. {label} stays non-binding for {company.name}."),
                    Outgoing(self.cfg.shared_room, f"{company.name}'s principal declined {label}. @{company.agent} stay within your mandate.")]

        approval = {"id": f"A-{len(self.approvals) + 1}", "company": company.key, "offer": label, "terms_hash": offer.terms.digest(),
                    "principal": msg.sender_name, "principal_id": msg.sender_id, "message_id": msg.message_id,
                    "room": msg.room_id, "seq": msg.seq, "at": self._now(msg)}
        self.approvals.append(approval)
        self.ledger.append("approval", **approval)
        self._note("approval", msg, company=company.key, offer=label, approval=approval["id"])
        out = [Outgoing(msg.room_id, f"✅ Approval {approval['id']} recorded for {label}, bound to terms hash "
                        f"`{approval['terms_hash'][:12]}…`. Any change to the terms voids it.")]
        if esc.kind == "offer":
            offer.status = "APPROVED"
            other = self.cfg.other(company.key)
            out.append(Outgoing(self.cfg.shared_room, f"✅ **{label} APPROVED** by {company.name}'s principal ({msg.sender_name}, approval {approval['id']}). "
                                f"It is now binding. @{other.agent} your move."))
        elif key in self.pending_accept:
            out.append(Outgoing(self.cfg.shared_room, f"✅ {company.name}'s principal ({msg.sender_name}) approved accepting {label} "
                                f"(approval {approval['id']}, from {company.name}'s own room). The acceptance is now binding."))
            out += self._agree(msg, company, offer, via="approval")
        return out

    # ---------- closing ----------
    def _countersign(self, msg: Incoming, company: Company, deal: str) -> list[Outgoing]:
        if deal != self.cfg.deal_id:
            return [Outgoing(msg.room_id, f"This room is deal {self.cfg.deal_id}, not {deal}.")]
        if not self.agreement:
            return [Outgoing(msg.room_id, "There's no agreement to countersign yet.")]
        if company.key in self.countersigned:
            return []
        record = {"company": company.key, "principal": msg.sender_name, "principal_id": msg.sender_id,
                  "message_id": msg.message_id, "terms_hash": self.agreement["terms_hash"], "at": self._now(msg)}
        self.countersigned[company.key] = record
        self.ledger.append("countersign", **record)
        self._note("countersign", msg, company=company.key)
        if len(self.countersigned) < 2:
            waiting = self.cfg.other(company.key)
            return [Outgoing(self.cfg.shared_room, f"✍️ {company.name} countersigned ({msg.sender_name}). Waiting on {waiting.name}.")]
        return self._close(msg)

    def _close(self, msg: Incoming) -> list[Outgoing]:
        deal = {"id": self.cfg.deal_id, "product": self.cfg.product,
                "buyer": {"key": self.cfg.buyer.key, "name": self.cfg.buyer.name},
                "seller": {"key": self.cfg.seller.key, "name": self.cfg.seller.name}}
        self.ledger.append("closed", deal=self.cfg.deal_id)
        self.certificate = build_certificate(self.signer, deal, self.ledger, self.agreement or {}, self._now(msg))
        if self.on_close:
            self.artifacts = self.on_close(self.certificate, self.annexes) or {}
        self._note("closed", msg, root=self.certificate["root_hash"])
        links = ""
        if self.artifacts:
            links = "\n" + "\n".join(f"• {k.replace('_', ' ')}: {v}" for k, v in self.artifacts.items() if not k.startswith("annex"))
        out = [Outgoing(self.cfg.shared_room, f"📜 **Deal {self.cfg.deal_id} closed.** {self.agreement['describe']}.\n"
                        f"Certificate root `{self.certificate['root_hash'][:16]}…`, signed by the referee. "
                        f"Anyone can check it offline with `mandate verify`.{links}")]
        for c in self.cfg.companies():
            annex = self.artifacts.get(f"annex_{c.key}", "")
            out.append(Outgoing(c.private_room, f"{self._mentions(c)} Your private annex (mandate {self.mandates[c.key].label} plus its salt) "
                                f"lets you prove {c.name} stayed within its authority without showing it to anyone else. {annex}".strip()))
        return out
