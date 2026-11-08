# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
"""Run and set up the Debate Hall.

    py -m hall init                                  make the data folder (readable only by you on Windows)
    py -m hall debate "Title" [--paper x.md]         start a debate (with its paper set); prints its id
    py -m hall seat <debate> <name> <role> [--person]  give someone a seat; the token goes in a file under data/tokens
    py -m hall seats <debate>                        list the seats
    py -m hall serve [--port 8810]                   run the hall on http://127.0.0.1:<port> only
        [--audience-door 192.168.x.y:8812 --audience-debate N]   + the audience door, for one open debate
    py -m hall claim <debate> <name>                 a one-time claim code for a seat (instead of its token)
    py -m hall revoke <debate> <name>                revoke a seat now

Roles: judge, clerk, moderator, advocate, adversary, reader_a, reader_b, witness, audience.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("DEBATE_HALL_DATA", ROOT / "data"))
DOCS = Path(os.environ.get("DEBATE_HALL_DOCS", ROOT / "debate_docs"))   # where the papers are (11-07: a setting)
DEFAULT_PORT = 8810


def lock_down(path: Path) -> None:
    """On Windows, only you, SYSTEM and Administrators may read the data folder (tokens, the log)."""
    if os.name != "nt":
        path.chmod(0o700)
        return
    user = os.environ.get("USERNAME", "")
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F", "SYSTEM:(OI)(CI)F",
                    "Administrators:(OI)(CI)F"], check=True, capture_output=True)


def hall():
    from .core import Hall
    DATA.mkdir(parents=True, exist_ok=True)
    return Hall(DATA / "hall.db", DOCS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hall", description="The Debate Hall")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    d = sub.add_parser("debate")
    d.add_argument("title")
    d.add_argument("--paper", help="a .md file in debate_docs/, set from the start so every seat reads the same text")
    sub.add_parser("people", help="list participants (id, name, person or agent, resident or visitor)")
    s = sub.add_parser("seat")
    s.add_argument("debate", type=int)
    s.add_argument("name")
    s.add_argument("role")
    s.add_argument("--person", action="store_true", help="a person (signs in on the page) rather than an agent")
    s.add_argument("--participant", type=int, help="seat this existing participant (by id; `hall people` lists "
                   "them). Without it a NEW participant is made, and a name in use is refused")
    rl = sub.add_parser("role", help="change a seat's role (the keeper; the judge can also do it on the page)")
    rl.add_argument("debate", type=int)
    rl.add_argument("name")
    rl.add_argument("role")
    ls = sub.add_parser("seats")
    ls.add_argument("debate", type=int)
    kp = sub.add_parser("keeper", help="a one-time code for the keeper's page (makes the keeper on a fresh hall)")
    kp.add_argument("--name", default="Keeper", help="the keeper's name, the first time")
    cl = sub.add_parser("claim", help="a one-time claim code for a seat (its old token stops working)")
    cl.add_argument("debate", type=int)
    cl.add_argument("name")
    rv = sub.add_parser("revoke", help="revoke a seat's token now (the judge can also do it on the page)")
    rv.add_argument("debate", type=int)
    rv.add_argument("name")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=DEFAULT_PORT)
    sv.add_argument("--canaries", type=Path, default=None,
                    help="the household-words list exports are checked against (default data/canaries.txt); "
                         "lines starting # are comments, a cs: prefix matches case-sensitively")
    sv.add_argument("--audience-door", metavar="LAN_IP:PORT",
                    help="also open the AUDIENCE DOOR on this home-network address, for one debate's audience only")
    sv.add_argument("--audience-debate", type=int, metavar="DEBATE", help="the debate the audience door is for")
    a = ap.parse_args(argv)

    if a.cmd == "init":
        fresh = not DATA.exists()
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "tokens").mkdir(exist_ok=True)
        if fresh:
            lock_down(DATA)
        hall().db.close()
        print(f"Data folder ready: {DATA}" + (" (locked to you)" if fresh else ""))
        return 0
    if a.cmd == "debate":
        h = hall()
        did = h.create_debate(a.title)
        print(did)
        if a.paper:
            p = h.point_at_paper(did, a.paper)
            print(f"The paper is {p['paper']} (sha256 {p['sha'][:12]}...).", file=sys.stderr)
        return 0
    if a.cmd == "people":
        for r in hall().db.q("SELECT * FROM participants WHERE removed_at IS NULL ORDER BY id"):
            print(f"{r['id']:>4}  {r['name']}  ({r['kind']}, {r['access']}{', keeper' if r['is_admin'] else ''})")
        return 0
    if a.cmd == "seat":
        h = hall()
        sid, token = h.add_seat(a.debate, a.name, a.role, "person" if a.person else "agent",
                                participant=a.participant)
        (DATA / "tokens").mkdir(exist_ok=True)
        f = DATA / "tokens" / f"debate{a.debate}-{a.name.replace(' ', '_')}.token"
        f.write_text(token + "\n", encoding="utf-8")
        print(f"Seat {sid}: {a.name} ({a.role}). Token file: {f}")
        print("Hand the file's path to the seat; never paste the token into a chat or a URL.")
        return 0
    if a.cmd == "role":
        from .core import ROLES
        h = hall()
        if a.role not in ROLES:
            print(f"role is one of: {', '.join(ROLES)}", file=sys.stderr)
            return 2
        target = h.seat_by_name(a.debate, a.name)
        if a.role in ("judge", "clerk", "moderator", "aide") and h.db.one(
                "SELECT 1 FROM seats WHERE debate_id = ? AND role = ? AND id != ?", (a.debate, a.role, target["id"])):
            print(f"this debate already has a {a.role}; change that seat first", file=sys.stderr)
            return 2
        h.db.run("UPDATE seats SET role = ? WHERE id = ?", (a.role, target["id"]))
        h._log(a.debate, "hall", None, "notice", f"The keeper makes {target['name']} the {a.role} (was {target['role']})",
               {"role_change": target["name"], "role": a.role})
        print(f"{target['name']} is now the {a.role}.")
        return 0
    if a.cmd == "seats":
        for x in hall().seats(a.debate):
            print(f"{x['id']:>4}  {x['name']:<20} {x['role']:<10} {x['kind']}")
        return 0
    if a.cmd == "keeper":
        code = hall().keeper_invite(a.name)
        print(f"The keeper's code: {code}")
        print("Open the hall's /keeper page on this computer and enter it there. It works once, within 7 days.")
        print("Run this again for another of the keeper's devices.")
        return 0
    if a.cmd == "claim":
        code = hall().issue_claim(a.debate, a.name)
        print(f"Claim code for {a.name}: {code}")
        print("Give it to them privately (in person or by voice is best). It works once, within 7 days: they enter it")
        print("on the hall page where it asks for a token. Their old token no longer works.")
        return 0
    if a.cmd == "revoke":
        h = hall()
        target = h.seat_by_name(a.debate, a.name)
        h.db.run("UPDATE seats SET revoked_at = ?, claim_hash = NULL WHERE id = ?", (h.now(), target["id"]))
        h._log(a.debate, "hall", None, "notice", f"The keeper revokes {target['name']}'s seat", {"revoked": target["name"]})
        print(f"{target['name']}'s seat is revoked.")
        return 0
    if a.cmd == "serve":
        return serve(a)
    return 2


def door_address(raw: str) -> tuple[str, int]:
    """The audience door listens only on a private home-network IPv4 address with an explicit port: never 0.0.0.0,
    never loopback (that's the main hall), never a public address."""
    import ipaddress
    host, _, port = (raw or "").rpartition(":")
    try:
        ip, p = ipaddress.IPv4Address(host), int(port)
    except ValueError:
        raise SystemExit("--audience-door is <home-network IPv4>:<port>, e.g. 192.168.1.10:8812") from None
    if not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_link_local or not 1024 <= p <= 65535:
        raise SystemExit("--audience-door must be this PC's private home-network address and a port 1024-65535")
    return str(ip), p


def serve(a) -> int:
    import asyncio
    import uvicorn
    from .app import create_app
    from .core import FIREWALL_RULE, FIREWALL_OFF
    canaries = a.canaries or DATA / "canaries.txt"
    if not canaries.is_file():
        print(f"Note: no household-words list at {canaries}; exports won't be checked until there is one.")
    h = hall()
    opts = {"log_level": "warning", "access_log": False, "proxy_headers": False}
    door = None
    if a.audience_door or a.audience_debate:          # checked before anything starts
        if not (a.audience_door and a.audience_debate):
            raise SystemExit("the audience door needs both --audience-door and --audience-debate")
        ip, port = door_address(a.audience_door)
        d = h.debate(a.audience_debate)
        if d["status"] == "closed":
            raise SystemExit(f"debate {d['id']} is closed: the audience door opens only while a debate is open")
    servers = [uvicorn.Server(uvicorn.Config(create_app(h, a.port, canary_file=canaries, canary_allow=DATA / "canary_allow.txt"), host="127.0.0.1",
                                             port=a.port, **opts))]
    print(f"The Debate Hall is on http://127.0.0.1:{a.port}/ (this computer only).", flush=True)
    if a.audience_door:
        door = uvicorn.Server(uvicorn.Config(create_app(h, port, door={"host": f"{ip}:{port}", "debate": d["id"]}),
                                             host=ip, port=port, **opts))
        servers.append(door)
        h._log(d["id"], "hall", None, "notice", "The audience door is open for this debate", {"door_open": True})
        print(f"AUDIENCE DOOR open on http://{ip}:{port}/ for debate {d['id']} ({d['title']}), audience seats only.", flush=True)
        print(f"Keeper: turn the firewall rule on (admin PowerShell: Enable-NetFirewallRule -DisplayName "
              f"'{FIREWALL_RULE}'). It closes by itself when the judge closes the debate.", flush=True)

    async def watch_door():
        # The door shuts when its debate closes (the keeper + a security review: open only during the debate).
        while door is not None and not door.should_exit:
            await asyncio.sleep(3)
            if h.debate(a.audience_debate)["status"] == "closed":
                door.should_exit = True
                print(f"AUDIENCE DOOR CLOSED (the debate ended; audience seats expired). Keeper: {FIREWALL_OFF}", flush=True)

    async def run_all():
        await asyncio.gather(*(s.serve() for s in servers), watch_door())

    asyncio.run(run_all())
    return 0


if __name__ == "__main__":
    sys.exit(main())
