"""Deal terms, mandates (a principal's limits for its own agent), and the checks between them.

Everything here is pure: no I/O, no clock, no model. The referee's decisions come from these
functions, so they are what the tests pin down.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, Literal

FIELDS = {
    "price": "price_per_year",
    "term": "term_months",
    "net": "payment_days",
    "seats": "seats",
    "tcv": "total_contract_value",
}
FIELD_LABELS = {
    "price": "price per year",
    "term": "term (months)",
    "net": "payment terms (days)",
    "seats": "seats",
    "tcv": "total contract value",
}
TERM_CHOICES = (12, 24, 36)
NET_CHOICES = (15, 30, 45, 60)

Op = Literal["<=", ">="]


@dataclass(frozen=True)
class Terms:
    price_per_year: int
    term_months: int
    payment_days: int
    seats: int

    @property
    def total_contract_value(self) -> int:
        return self.price_per_year * self.term_months // 12

    def value(self, short: str) -> int:
        if short == "tcv":
            return self.total_contract_value
        return getattr(self, FIELDS[short])

    def to_dict(self) -> dict:
        return asdict(self)

    def canonical(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def describe(self) -> str:
        return (
            f"${self.price_per_year:,}/yr · {self.term_months} mo · net {self.payment_days} · "
            f"{self.seats} seats (TCV ${self.total_contract_value:,})"
        )

    @classmethod
    def from_dict(cls, data: dict) -> "Terms":
        try:
            terms = cls(
                price_per_year=int(data["price_per_year"]),
                term_months=int(data["term_months"]),
                payment_days=int(data["payment_days"]),
                seats=int(data["seats"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"offer is missing or has a bad field: {exc}") from exc
        if min(terms.price_per_year, terms.term_months, terms.payment_days, terms.seats) <= 0:
            raise ValueError("every term must be a positive number")
        return terms


@dataclass(frozen=True)
class Constraint:
    field: str  # short name: price, term, net, seats, tcv
    op: Op
    value: int
    when_term: int | None = None  # applies only when term_months equals this

    def applies(self, terms: Terms) -> bool:
        return self.when_term is None or terms.term_months == self.when_term

    def holds(self, terms: Terms) -> bool:
        if not self.applies(terms):
            return True
        actual = terms.value(self.field)
        return actual <= self.value if self.op == "<=" else actual >= self.value

    @property
    def code(self) -> str:
        return f"{self.field.upper()}_{'ABOVE_MAX' if self.op == '<=' else 'BELOW_MIN'}"

    def render(self) -> str:
        cond = f"@{self.when_term}" if self.when_term else ""
        return f"{self.field}{self.op}{self.value}{cond}"

    def explain(self, terms: Terms) -> str:
        actual = terms.value(self.field)
        bound = "max" if self.op == "<=" else "min"
        scope = f" for a {self.when_term}-month term" if self.when_term else ""
        return f"{FIELD_LABELS[self.field]} {actual:,} is outside the {bound} of {self.value:,}{scope}"


@dataclass(frozen=True)
class Violation:
    code: str
    detail: str


@dataclass
class Mandate:
    """A principal's limits for its own agent. Versioned; only one version is live at a time."""

    company: str
    label: str  # e.g. M-ACME-1
    constraints: tuple[Constraint, ...]
    set_by: str = ""
    confirmed: bool = False
    salt: str = ""  # used for the public commitment, so the hash can't be brute-forced
    meta: dict = field(default_factory=dict)

    def evaluate(self, terms: Terms) -> list[Violation]:
        return [Violation(c.code, c.explain(terms)) for c in self.constraints if not c.holds(terms)]

    def render(self) -> str:
        return " ".join(c.render() for c in self.constraints)

    def canonical(self) -> str:
        body = {"company": self.company, "label": self.label, "rules": self.render()}
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    def commitment(self) -> str:
        """Salted hash, safe to publish: it proves which mandate was used without revealing it."""
        return hashlib.sha256((self.salt + "|" + self.canonical()).encode()).hexdigest()

    def to_annex(self) -> dict:
        return {"company": self.company, "label": self.label, "rules": self.render(), "salt": self.salt}


_TOKEN = re.compile(r"^(price|term|net|seats|tcv)(<=|>=|=)(\d[\d,_]*)(?:\.\.(\d[\d,_]*))?(?:@(\d+))?$")


def parse_constraints(spec: str) -> tuple[Constraint, ...]:
    """Parse e.g. ``price<=48000 term=12..24 net>=30 seats>=180 price>=45000@36``."""
    out: list[Constraint] = []
    for raw in spec.replace(";", " ").split():
        token = raw.strip().lower().replace("$", "").replace("k", "000")
        m = _TOKEN.match(token)
        if not m:
            raise ValueError(f"can't read '{raw}'. Use forms like price<=48000, term=12..24, net>=30")
        fld, op, a, b, when = m.groups()
        lo = int(a.replace(",", "").replace("_", ""))
        when_term = int(when) if when else None
        if op == "=":
            hi = int(b.replace(",", "").replace("_", "")) if b else lo
            if hi < lo:
                raise ValueError(f"range in '{raw}' runs backwards")
            out.append(Constraint(fld, ">=", lo, when_term))
            out.append(Constraint(fld, "<=", hi, when_term))
        else:
            if b:
                raise ValueError(f"use '=' for a range in '{raw}'")
            out.append(Constraint(fld, op, lo, when_term))  # type: ignore[arg-type]
    if not out:
        raise ValueError("a mandate needs at least one limit")
    return tuple(out)


def _price_window(constraints: Iterable[Constraint], term: int, net: int, seats: int) -> tuple[int, int] | None:
    """The interval of yearly prices allowed for fixed term/net/seats, or None if non-price limits fail."""
    lo, hi = 0, 10**12
    probe = Terms(1, term, net, seats)
    for c in constraints:
        if not c.applies(probe):
            continue
        if c.field == "price":
            lo, hi = (lo, min(hi, c.value)) if c.op == "<=" else (max(lo, c.value), hi)
        elif c.field == "tcv":
            per_year = c.value * 12 / term
            lo, hi = (lo, min(hi, int(per_year))) if c.op == "<=" else (max(lo, -(-c.value * 12 // term)), hi)
        elif not c.holds(probe):
            return None
    return (lo, hi) if lo <= hi else None


def overlap_exists(a: Mandate, b: Mandate) -> bool:
    """Is there any deal both mandates allow? Answers yes/no only, so neither side learns numbers."""
    return find_overlap(a, b) is not None


def find_overlap(a: Mandate, b: Mandate) -> Terms | None:
    seat_candidates = sorted({c.value for c in a.constraints + b.constraints if c.field == "seats"} | {200})
    for term in TERM_CHOICES:
        for net in NET_CHOICES:
            for seats in seat_candidates:
                wa = _price_window(a.constraints, term, net, seats)
                wb = _price_window(b.constraints, term, net, seats)
                if wa and wb:
                    lo, hi = max(wa[0], wb[0]), min(wa[1], wb[1])
                    if lo <= hi:
                        return Terms(lo, term, net, seats)
    return None
