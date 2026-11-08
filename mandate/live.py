"""Running Mandate against a real Switch server.

* `setup()` provisions the demo on a Switch server: three Switch users (Priya at Acme, Sam at Nimbus, a
  neutral referee operator), their Mattermost accounts linked to them, one agent per user so the three
  agents have three different owners, the three rooms with links, and room-scoped addressing rules.
* `run_live()` runs Keeper (the referee) and the two negotiating agents over the agent protocol, and
  serves the ledger page.
* `play()` posts the human lines of the story into Mattermost as Priya and Sam, for recording.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import time
from pathlib import Path

import httpx
import uvicorn

from .artifacts import write_closing_files
from .engine import DealConfig, Engine, Incoming
from .ledger import Signer
from .negotiator import (BUYER_SCRIPT, SELLER_SCRIPT, ClaudeBrain, NegotiatorAgent, ScriptedBrain,
                         has_claude_credentials)
from .scenario import ACME_PLAYBOOK, DIRECTOR, NIMBUS_PLAYBOOK, demo_config
from .server import create_app
from .switch import AgentClient, AgentCredentials, Event, SwitchError, register_agent

log = logging.getLogger("mandate.live")

PEOPLE = [
    # key, Switch name, email, Mattermost username, display, agent, agent description
    ("acme", "Priya (Acme CFO)", "priya@acme.test", "priya", ("Priya", "Acme CFO"), "acme-buyer",
     "Negotiates Acme's software renewals inside Priya's mandate"),
    ("nimbus", "Sam (Nimbus VP Sales)", "sam@nimbus.test", "sam", ("Sam", "Nimbus Sales"), "nimbus-seller",
     "Negotiates Nimbus renewals inside Sam's mandate"),
    ("referee", "Mandate Referee", "referee@mandate.test", None, None, "keeper",
     "Neutral referee: stamps or blocks every offer against each side's private mandate"),
]

SHARED_INSTRUCTIONS = """Deal room for {product}. Acme's agent (acme-buyer) and Nimbus's agent (nimbus-seller) negotiate here.
Offers must be fenced ```offer``` blocks with price_per_year, term_months, payment_days and seats.
To accept, write `accept O-<n>` on its own line. Only offers that keeper stamps are binding.
Each agent's authority comes only from its own principal's mandate, set in that company's private room."""

PRIVATE_INSTRUCTIONS = """{company}'s private room for deal {deal}. Only {company}'s people, {agent} and the referee are here.
Set {agent}'s mandate with `mandate price<=48000 term=12..24 net>=30 seats>=180`, then `confirm M-...`.
When the referee escalates, answer `approve O-<n>` or `decline O-<n>`. Close with `countersign {deal}`."""


def _env(path: Path) -> dict:
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _gateway(server: str, email: str, password: str) -> httpx.Client:
    c = httpx.Client(base_url=server, timeout=30)
    r = c.post("/gateway/auth/login", json={"email": email, "password": password})
    if r.status_code >= 300:
        raise SwitchError(f"gateway login for {email}: {r.status_code} {r.text[:200]}")
    return c


def setup(config_path: Path, server: str, switch_env: Path, mattermost: str, public_url: str) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = _env(switch_env)
    previous = json.loads(config_path.read_text()) if config_path.exists() else {}
    password = previous.get("password") or "mandate-" + secrets.token_urlsafe(9)
    mm_password = previous.get("mm_password") or "Mm-" + secrets.token_urlsafe(9) + "!1"
    # Save the secrets first, so a run that fails halfway can be re-run.
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({**previous, "password": password, "mm_password": mm_password}, indent=2))
    config_path.chmod(0o600)
    previous = previous or {"password": password}

    admin = _gateway(server, env["GATEWAY_ADMIN_EMAIL"], env["GATEWAY_ADMIN_PASSWORD"])
    for _key, name, email, *_ in PEOPLE:
        r = admin.post("/gateway/users", json={"name": name, "email": email, "password": password})
        if r.status_code == 409 and not previous:
            raise SwitchError(f"{email} already exists but there is no saved config with its password. "
                              "Delete the user in the Gateway or reset the Switch stack.")
        log.info("Switch user %s: %s", email, "created" if r.status_code < 300 else "exists")

    mm = httpx.Client(base_url=mattermost, timeout=30)
    r = mm.post("/api/v4/users/login", json={"login_id": env["MATTERMOST_ADMIN_USER"], "password": env["MATTERMOST_ADMIN_PASSWORD"]})
    mm.headers["Authorization"] = "Bearer " + r.headers["Token"]
    team = mm.get(f"/api/v4/teams/name/{env.get('MATTERMOST_TEAM_NAME', 'switch')}").json()
    mm_ids = {}
    for key, _n, _e, username, display, *_ in PEOPLE:
        if not username:
            continue
        r = mm.post("/api/v4/users", json={"username": username, "email": f"{username}@{key}.test", "password": mm_password,
                                           "first_name": display[0], "last_name": display[1]})
        user = r.json() if r.status_code < 300 else mm.get(f"/api/v4/users/username/{username}").json()
        if r.status_code >= 300:
            mm.put(f"/api/v4/users/{user['id']}/password", json={"new_password": mm_password})
        mm.post(f"/api/v4/teams/{team['id']}/members", json={"team_id": team["id"], "user_id": user["id"]})
        mm_ids[username] = user["id"]
        log.info("Mattermost user %s ready", username)

    creds: dict[str, dict] = {}
    for key, _n, email, username, _d, agent, desc in PEOPLE:
        g = _gateway(server, email, password)
        reg_key = g.post("/gateway/api-keys", json={"label": f"mandate-{agent}", "expires_in_days": 30}).json()["key"]
        c = register_agent(server, reg_key, agent, desc, display_name=agent, overwrite=True)
        creds[agent] = c.__dict__
        log.info("agent %s registered, owned by %s", agent, email)

    rooms, bridge_id = asyncio.run(_provision_rooms(server, AgentCredentials(**creds["keeper"])))
    for _key, _n, email, username, *_ in PEOPLE:
        if username:
            _link_identity(server, mattermost, email, password, username, mm_password, mm_ids[username], bridge_id)

    shared, acme_room, nimbus_room = rooms["shared"], rooms["acme"], rooms["nimbus"]
    ids = {a: creds[a]["agent_id"] for a in creds}
    policies = {
        "acme-buyer": [{"owner": True, "users": [], "agents": []},
                       {"rooms": [shared], "users": "*", "agents": [ids["keeper"], ids["nimbus-seller"]]}],
        "nimbus-seller": [{"owner": True, "users": [], "agents": []},
                          {"rooms": [shared], "users": "*", "agents": [ids["keeper"], ids["acme-buyer"]]}],
        "keeper": [{"rooms": [shared, acme_room, nimbus_room]}],
    }
    for agent, rules in policies.items():
        r = admin.put(f"/gateway/agents/{ids[agent]}/addressing-policy", json={"policy": {"rules": rules}})
        if r.status_code >= 300:
            raise SwitchError(f"addressing policy for {agent}: {r.status_code} {r.text[:300]}")
        log.info("addressing rules set for %s (%d rules)", agent, len(rules))

    cfg = demo_config(shared=shared, acme_room=acme_room, nimbus_room=nimbus_room, public_url=public_url)
    config = {"server": server, "mattermost": mattermost, "team": team["name"], "password": password,
              "mm_password": mm_password, "agents": creds, "rooms": rooms, "deal": cfg.to_dict()}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2))
    config_path.chmod(0o600)
    log.info("\nSaved %s. Log in to Mattermost at %s as priya or sam (password in the config file).", config_path, mattermost)
    return config


async def _provision_rooms(server: str, keeper_creds: AgentCredentials) -> tuple[dict, str]:
    keeper = AgentClient(server, keeper_creds)
    existing = {r["name"]: r["room_id"] for r in await keeper.op("list_rooms")}
    cfg = demo_config()
    specs = {
        "acme": ("acme-private", "Acme only: mandate, escalations, approvals", ["keeper", "acme-buyer"], ["priya"],
                 PRIVATE_INSTRUCTIONS.format(company="Acme", deal=cfg.deal_id, agent="acme-buyer")),
        "nimbus": ("nimbus-private", "Nimbus only: mandate, escalations, approvals", ["keeper", "nimbus-seller"], ["sam"],
                   PRIVATE_INSTRUCTIONS.format(company="Nimbus", deal=cfg.deal_id, agent="nimbus-seller")),
        "shared": ("deal-acme-nimbus", f"Deal {cfg.deal_id}: {cfg.product}", ["keeper", "acme-buyer", "nimbus-seller"],
                   ["priya", "sam"], SHARED_INSTRUCTIONS.format(product=cfg.product)),
    }
    try:
        await keeper.op("create_room_group", name="deals", description="Mandate deal rooms")
    except SwitchError:
        pass  # already exists
    rooms = {}
    for key, (name, desc, agents, users, instructions) in specs.items():
        if name in existing:
            rooms[key] = existing[name]
            log.info("room %s exists", name)
            continue
        res = await keeper.op("create_room", name=name, description=desc, agent_names=agents, user_names=users,
                              channel_type="channel_private" if key != "shared" else "channel_public",
                              instructions=instructions, group_name="deals")
        rooms[key] = res["id"]
        log.info("room %s created", name)
    for key in ("acme", "nimbus"):
        try:
            await keeper.op("link_rooms", source_room_id=rooms["shared"], target_room_id=rooms[key], label=f"{key} principal room")
        except SwitchError as exc:
            log.info("link %s: %s", key, str(exc)[:120])
    bridge_id = (await keeper.op("list_bridges"))[0]["id"]
    await keeper.close()
    return rooms, bridge_id


def _link_identity(server, mattermost, email, password, username, mm_password, mm_user_id, bridge_id) -> None:
    g = _gateway(server, email, password)
    mine = g.get("/gateway/auth/me/identities").json()
    if any(i.get("bridge_id") == bridge_id and i.get("verified", True) for i in mine):
        log.info("%s already linked to Mattermost", username)
        return
    for _ in range(20):
        r = g.post(f"/gateway/collaborations/{bridge_id}/identities",
                   json={"external_user_id": mm_user_id, "username": username, "expects_confirmation": True})
        if r.status_code < 300:
            break
        time.sleep(1.5)  # the bridge directory can lag behind a newly created account
    else:
        raise SwitchError(f"could not link {username}: {r.status_code} {r.text[:200]}")
    identity = r.json()
    if identity.get("link_request"):
        mm = httpx.Client(base_url=mattermost, timeout=10)
        token = mm.post("/api/v4/users/login", json={"login_id": username, "password": mm_password}).headers["Token"]
        v = g.post(f"/gateway/collaborations/{bridge_id}/identities/{identity['id']}/verify-session", json={"token": token})
        mm.post("/api/v4/users/logout", headers={"Authorization": f"Bearer {token}"})
        if v.status_code >= 300:
            raise SwitchError(f"confirming {username}'s link: {v.status_code} {v.text[:200]}")
    log.info("%s's Mattermost account linked to %s", username, email)


class MessageStore:
    """Keeper's durable input log. On restart, replaying it rebuilds the engine exactly."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""create table if not exists messages (message_id text primary key, room text, sender text,
                           sender_id text, body text, seq integer, ts integer, received real)""")

    def add(self, m: Incoming) -> bool:
        try:
            self.db.execute("insert into messages values (?,?,?,?,?,?,?,?)",
                            (m.message_id, m.room_id, m.sender_name, m.sender_id, m.body, m.seq, m.timestamp_ms, time.time()))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def all(self) -> list[Incoming]:
        rows = self.db.execute("select room, sender, body, message_id, sender_id, seq, ts from messages order by received").fetchall()
        return [Incoming(r[0], r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows]


def _incoming(ev: Event) -> Incoming:
    return Incoming(ev.room_id, ev.sender_name, ev.body, ev.message_id, ev.sender, ev.seq, int(ev.payload.get("timestamp") or 0))


async def run_live(config_path: Path, out_dir: Path, port: int = 8787, claude: bool = False, only: str | None = None,
                   agent_delay: float = 0.0) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    config = json.loads(config_path.read_text())
    cfg = DealConfig.from_dict(config["deal"])
    server = config["server"]
    state_dir = config_path.parent
    tasks = []

    if only in (None, "keeper"):
        signer = Signer.load_or_create(state_dir / "keeper_ed25519.pem")
        engine_box: dict = {}

        def build_engine() -> Engine:
            return Engine(cfg, signer, on_close=lambda cert, annexes: write_closing_files(out_dir, cert, annexes, cfg.public_url))

        engine = build_engine()
        store = MessageStore(state_dir / "keeper.sqlite")
        replayed = store.all()
        for m in replayed:  # rebuild state; don't re-post anything
            engine.handle(m)
        engine_box["e"] = engine
        if replayed:
            log.info("keeper rebuilt state from %d stored messages", len(replayed))
        keeper = AgentClient(server, AgentCredentials(**config["agents"]["keeper"]), state_path=state_dir / "keeper.conn.json")

        async def on_keeper(ev: Event) -> None:
            if ev.kind != "message" or ev.sender_name == cfg.keeper:
                return
            m = _incoming(ev)
            if not store.add(m):
                return  # duplicate delivery after a reconnect
            for out in engine_box["e"].handle(m):
                await keeper.post(out.room_id, out.text)

        tasks.append(keeper.run(on_keeper))
        app = create_app(lambda: engine_box["e"].state(), out_dir)
        server_cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        tasks.append(uvicorn.Server(server_cfg).serve())
        log.info("ledger page: http://127.0.0.1:%d", port)

    use_claude = claude and has_claude_credentials()
    if claude and not use_claude:
        log.warning("no Claude credentials found; agents will play the scripted moves")
    for name, company, script, playbook, other in (
        (cfg.buyer.agent, "Acme", BUYER_SCRIPT, ACME_PLAYBOOK, cfg.seller.agent),
        (cfg.seller.agent, "Nimbus", SELLER_SCRIPT, NIMBUS_PLAYBOOK, cfg.buyer.agent),
    ):
        if only not in (None, name):
            continue
        brain = ClaudeBrain(name, company, other, playbook) if use_claude else ScriptedBrain(script)
        agent = NegotiatorAgent(name, company.lower(), cfg.shared_room, brain)
        client = AgentClient(server, AgentCredentials(**config["agents"][name]), state_path=state_dir / f"{name}.conn.json")
        tasks.append(client.run(_agent_handler(agent, client, agent_delay)))
        log.info("%s running with %s", name, "Claude" if use_claude else "the scripted moves")

    await asyncio.gather(*tasks)


def _agent_handler(agent: NegotiatorAgent, client: AgentClient, delay: float = 0.0):
    async def handle(ev: Event) -> None:
        if ev.kind != "message" or ev.sender_name == agent.name:
            return
        agent.observe(ev.room_id, ev.sender_name, ev.body)
        if ev.addressed:
            move = await asyncio.to_thread(agent.on_addressed, ev.room_id, ev.sender_name, ev.body)
            if move:
                if delay:
                    await asyncio.sleep(delay)  # pacing for recordings; real agents take time to think anyway
                await client.post(ev.room_id, move)
    return handle


def play(config_path: Path, pause: float = 0.0) -> None:
    """Post the human lines of the story as Priya and Sam, waiting for the referee between them."""
    config = json.loads(config_path.read_text())
    cfg = DealConfig.from_dict(config["deal"])
    mm_base = config["mattermost"]
    sessions, channels = {}, {}
    for user in ("priya", "sam"):
        c = httpx.Client(base_url=mm_base, timeout=10)
        r = c.post("/api/v4/users/login", json={"login_id": user, "password": config["mm_password"]})
        c.headers["Authorization"] = "Bearer " + r.headers["Token"]
        sessions[user] = c
        channels[user] = {ch["display_name"]: ch["id"] for ch in c.get("/api/v4/users/me/channels").json()}
    names = {"shared": "deal-acme-nimbus", "acme": "acme-private", "nimbus": "nimbus-private"}
    ui = httpx.Client(base_url=cfg.public_url or "http://localhost:8787", timeout=5)

    def wait_for(pred, what: str, timeout: float = 90) -> None:
        end = time.time() + timeout
        while time.time() < end:
            try:
                if pred(ui.get("/api/state").json()):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise SystemExit(f"timed out waiting for: {what}")

    waits = {
        4: (lambda s: any(t["kind"] == "accept" for t in s["timeline"]), "the referee to block Acme's first acceptance"),
        5: (lambda s: sum(t["kind"] == "accept" for t in s["timeline"]) >= 2, "the referee to block the second acceptance"),
        6: (lambda s: s["agreement"] is not None, "the agreement"),
    }
    for i, (who, room, text) in enumerate(DIRECTOR):
        if i in waits:
            wait_for(*waits[i])
        time.sleep(1.5 + pause)
        channel_id = channels[who].get(names[room])
        if not channel_id:
            raise SystemExit(f"{who} is not in {names[room]}; run `mandate setup` again")
        message = text.replace("{buyer}", cfg.buyer.agent)
        sessions[who].post("/api/v4/posts", json={"channel_id": channel_id, "message": message})
        print(f"{who:>6} → #{names[room]}: {message}")
    wait_for(lambda s: s["status"] == "closed", "the deal to close")
    print("Deal closed. Ledger:", cfg.public_url or "http://localhost:8787")
