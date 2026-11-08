"""mandate: command line.

  mandate demo                 run the whole deal offline (no server, no API key) and write out/
  mandate verify CERT [--annex A] [--key K]   check a certificate offline
  mandate tamper CERT OUT      write a copy with one digit changed (to show verify failing)
  mandate setup                provision users, agents, rooms and access rules on a live Switch server
  mandate live                 run the referee and both agents against the live server
  mandate play                 drive the human side of the story in Mattermost (for recording)
  mandate ui DIR               serve the ledger page for a finished offline run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

GREEN, RED, YELLOW, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def cmd_demo(args) -> int:
    from .sim import run_demo

    brains = None
    if args.claude:
        from .negotiator import ClaudeBrain
        from .scenario import ACME_PLAYBOOK, NIMBUS_PLAYBOOK

        brains = {"acme-buyer": ClaudeBrain("acme-buyer", "Acme", "nimbus-seller", ACME_PLAYBOOK),
                  "nimbus-seller": ClaudeBrain("nimbus-seller", "Nimbus", "acme-buyer", NIMBUS_PLAYBOOK)}
    out = Path(args.out)
    result = run_demo(out, quiet=args.quiet, brains=brains)
    state = result["state"]
    print(f"\n{BOLD}Deal status:{RESET} {state['status']}")
    for k, v in state["artifacts"].items():
        print(f"  {k.replace('_', ' '):<14} {v}")
    print(f"\nNext: {BOLD}mandate verify {out}/D-1-certificate.json --annex {out}/D-1-annex-acme.json{RESET}")
    return 0 if state["status"] == "closed" else 1


def cmd_verify(args) -> int:
    from .ledger import verify_certificate

    cert = json.loads(Path(args.certificate).read_text())
    annexes = [json.loads(Path(a).read_text()) for a in args.annex or []]
    checks = verify_certificate(cert, annexes, trusted_key_b64=args.key)
    print(f"{BOLD}Deal {cert['deal']['id']}{RESET} · {cert['deal']['buyer']['name']} ⇄ {cert['deal']['seller']['name']} · issued {cert['issued_at']}")
    failed = False
    for c in checks:
        mark = f"{GREEN}PASS{RESET}" if c.ok else f"{DIM}SKIP{RESET}" if c.ok is None else f"{RED}FAIL{RESET}"
        failed |= c.ok is False
        print(f"  {mark}  {c.name}: {c.detail}")
    print(f"\n{RED + 'NOT VALID' if failed else GREEN + 'VALID'}{RESET}")
    return 1 if failed else 0


def cmd_tamper(args) -> int:
    cert = json.loads(Path(args.certificate).read_text())
    cert["final_terms"]["price_per_year"] -= 1000
    for ev in cert["chain"]:
        if ev["kind"] == "agreement":
            ev["data"]["terms"]["price_per_year"] -= 1000
    Path(args.out).write_text(json.dumps(cert, indent=2))
    print(f"Wrote {args.out} with the agreed price lowered by $1,000.")
    return 0


def cmd_setup(args) -> int:
    from .live import setup

    setup(Path(args.config), args.server, Path(args.switch_env), mattermost=args.mattermost, public_url=args.public_url)
    return 0


def cmd_live(args) -> int:
    import asyncio

    from .live import run_live

    asyncio.run(run_live(Path(args.config), Path(args.out), port=args.port, claude=args.claude, only=args.only,
                         agent_delay=args.agent_delay))
    return 0


def cmd_play(args) -> int:
    from .live import play

    play(Path(args.config), pause=args.pause)
    return 0


def cmd_ui(args) -> int:
    import uvicorn

    from .server import create_app

    out = Path(args.dir)
    state = json.loads((out / "state.json").read_text())
    app = create_app(lambda: state, out)
    print(f"Ledger page: http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mandate", description="Bounded authority for AI agents negotiating on Switch.")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="run the whole deal offline")
    d.add_argument("--out", default="out")
    d.add_argument("--quiet", action="store_true")
    d.add_argument("--claude", action="store_true", help="let Claude play both agents instead of the script")
    d.set_defaults(fn=cmd_demo)

    v = sub.add_parser("verify", help="verify a certificate offline")
    v.add_argument("certificate")
    v.add_argument("--annex", action="append", help="a side's private annex (repeatable)")
    v.add_argument("--key", help="pin the referee's public key (base64)")
    v.set_defaults(fn=cmd_verify)

    t = sub.add_parser("tamper", help="write a tampered copy of a certificate")
    t.add_argument("certificate")
    t.add_argument("out")
    t.set_defaults(fn=cmd_tamper)

    s = sub.add_parser("setup", help="provision a live Switch server")
    s.add_argument("--server", default="http://localhost:8110")
    s.add_argument("--mattermost", default="http://localhost:8065")
    s.add_argument("--switch-env", default="switch/.env")
    s.add_argument("--config", default=".mandate/live.json")
    s.add_argument("--public-url", default="http://localhost:8787")
    s.set_defaults(fn=cmd_setup)

    lv = sub.add_parser("live", help="run referee and agents against the live server")
    lv.add_argument("--config", default=".mandate/live.json")
    lv.add_argument("--out", default="out/live")
    lv.add_argument("--port", type=int, default=8787)
    lv.add_argument("--claude", action="store_true", help="agents use Claude instead of the script")
    lv.add_argument("--agent-delay", type=float, default=0.0, help="seconds an agent waits before posting (for recordings)")
    lv.add_argument("--only", choices=["keeper", "acme-buyer", "nimbus-seller"], help="run just one participant")
    lv.set_defaults(fn=cmd_live)

    pl = sub.add_parser("play", help="post the human lines of the story into Mattermost")
    pl.add_argument("--config", default=".mandate/live.json")
    pl.add_argument("--pause", type=float, default=0.0, help="extra seconds between lines, for recording")
    pl.set_defaults(fn=cmd_play)

    u = sub.add_parser("ui", help="serve the ledger page for an offline run")
    u.add_argument("dir", nargs="?", default="out")
    u.add_argument("--port", type=int, default=8787)
    u.set_defaults(fn=cmd_ui)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
