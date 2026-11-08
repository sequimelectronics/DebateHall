# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
"""The Debate Hall's rules: seats and roles, rooms, the floor, the log, and the objection ledger.

Everything a seat may do is checked here, on the server (security review, 10-25): a post from someone who doesn't hold the
floor is refused, huddles stay private until the judge closes the debate, and only the chair moves the floor.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from typing import Callable

from .db import Database

ROLES = ("judge", "clerk", "moderator", "advocate", "adversary", "reader_a", "reader_b", "witness", "aide",
         "audience")
TEAM = {"advocate": "a", "reader_a": "a", "adversary": "b", "reader_b": "b"}
CHAIR = ("judge", "moderator")                  # move the floor, set the phase, speak at any time
ROOMS = ("hall", "gallery", "team_a", "team_b", "aide", "chair")   # chair: the judge and moderator, procedural
PHASES = ("setup", "openings", "objections", "rebuttals", "rulings", "closings", "done")
# The audience door's firewall rule (tools/audience_door_firewall.ps1 makes it, disabled). The keeper turns it on
# when a debate opens and off when it closes; turning it off needs admin, so the hall prints the line.
FIREWALL_RULE = "Debate Hall audience door"
FIREWALL_OFF = f"admin PowerShell: Disable-NetFirewallRule -DisplayName '{FIREWALL_RULE}'"
VERDICTS = ("sustained", "overruled", "revise")
MAX_TEXT = 8000
MAX_QUOTE = 600              # cite by reference; quote only a little (an advocate: deployed traces never leave the house)
SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,31}$")
MENTION = re.compile(r"[@\uFF20]([A-Za-z][A-Za-z0-9_-]{0,31})")   # plain @ or full-width (U+FF20), 10-30


class HallError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Hall:
    def __init__(self, db_path: Path | str, docs_dir: Path | str, now: Callable[[], float] = time.time):
        self.db = Database(db_path)
        self.docs_dir = Path(docs_dir).resolve()
        self.now = now
        # Objections are numbered from #1 within each debate (the moderator, a test debate 10-25: a dry run in the same
        # hall had taken #1, so the debate's first objection was #2). Number any older rows once.
        for r in self.db.q("SELECT id, debate_id FROM objections WHERE num IS NULL ORDER BY id"):
            self.db.run("UPDATE objections SET num = (SELECT COALESCE(MAX(num), 0) + 1 FROM objections WHERE "
                        "debate_id = ?) WHERE id = ?", (r["debate_id"], r["id"]))
        self._migrate_people()

    # ------------------------------------------------------------------ people (access like AI-Beach's, 11-07)
    def _migrate_people(self) -> None:
        """Once, for seats made before participants existed: each seat becomes its OWN participant (security review: never
        merge by name; two seats called the same in different debates may not be one holder; the keeper merges by
        hand). The seat's token moves into a sign-in for that debate only, so an old token reaches exactly its old
        debate and role; a waiting claim code becomes an invite."""
        rows = self.db.q("SELECT * FROM seats WHERE participant_id IS NULL ORDER BY id")
        if not rows:
            return
        with self.db.lock:
            self.db.conn.execute("BEGIN")
            try:
                for s in rows:
                    name = s["name"]
                    if self.db.one("SELECT 1 FROM participants WHERE lower(name) = lower(?)", (name,)):
                        name = f"{s['name']} (debate {s['debate_id']})"
                    pid = self.db.run("INSERT INTO participants (name, kind, created_at) VALUES (?, ?, ?)",
                                      (name, s["kind"], s["created_at"]))
                    self.db.run("INSERT INTO sign_ins (participant_id, token_hash, label, debate_id, created_at, "
                                "revoked_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (pid, s["token_hash"], f"seat {s['id']} (from before sign-ins)", s["debate_id"],
                                 s["created_at"], s["revoked_at"]))
                    if s["claim_hash"] and s["claim_expires"]:
                        self.db.run("INSERT INTO invites (participant_id, code_hash, debate_id, expires_at, "
                                    "created_at) VALUES (?, ?, ?, ?, ?)",
                                    (pid, s["claim_hash"], s["debate_id"], s["claim_expires"], s["created_at"]))
                    self.db.run("UPDATE seats SET participant_id = ? WHERE id = ?", (pid, s["id"]))
                self.db.conn.execute("COMMIT")
            except Exception:
                self.db.conn.execute("ROLLBACK")
                raise

    def participant_named(self, name: str, kind: str):
        return self.db.one("SELECT * FROM participants WHERE lower(name) = lower(?) AND kind = ? AND removed_at IS NULL",
                           (name.strip(), kind))

    def add_participant(self, name: str, kind: str, access: str = "resident", is_admin: bool = False) -> int:
        name = (name or "").strip()
        if not NAME.match(name):
            raise HallError("invalid", "a name is letters, digits, spaces, _ or -, starting with a letter")
        if kind not in ("person", "agent"):
            raise HallError("invalid", "kind is person or agent")
        if access not in ("resident", "visitor"):
            raise HallError("invalid", "access is resident or visitor")
        if self.db.one("SELECT 1 FROM participants WHERE lower(name) = lower(?) AND removed_at IS NULL", (name,)):
            raise HallError("conflict", f"someone is already called {name}")
        return self.db.run("INSERT INTO participants (name, kind, access, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                           (name, kind, access, int(is_admin), self.now()))

    def new_sign_in(self, pid: int, label: str, debate_id: int | None = None) -> str:
        token = secrets.token_urlsafe(32)
        self.db.run("INSERT INTO sign_ins (participant_id, token_hash, label, debate_id, created_at) VALUES "
                    "(?, ?, ?, ?, ?)", (pid, token_hash(token), label[:60], debate_id, self.now()))
        return token

    # ------------------------------------------------------------------ the keeper (as AI-Beach's, 11-07)
    def keeper_invite(self, name: str = "Keeper") -> str:
        """A one-time code for the keeper's device: makes the keeper on a fresh hall, else invites another of the
        keeper's devices. Printed by `hall keeper` on the hall's own computer, never sent anywhere."""
        k = self.db.one("SELECT id FROM participants WHERE is_admin = 1 AND removed_at IS NULL ORDER BY id LIMIT 1")
        pid = k["id"] if k else self.add_participant(name, "person", is_admin=True)
        return self.new_invite(pid, None)

    def keeper_for(self, token: str | None):
        si = self.sign_in_for(token)
        if si is None or si["debate_id"] is not None:
            return None
        p = self.db.one("SELECT * FROM participants WHERE id = ? AND is_admin = 1 AND removed_at IS NULL",
                        (si["participant_id"],))   # sign_in_for refuses a removed holder too; this says so here (security review)
        return p

    def overview(self) -> dict:
        """Everything the keeper page shows: debates with their seats, people with their devices, and the papers
        in the docs folder. Token hashes and codes never leave the hall."""
        debates = []
        for d in self.db.q("SELECT * FROM debates ORDER BY id DESC"):
            seats = [{"id": s["id"], "name": s["name"], "role": s["role"], "kind": s["kind"],
                      "participant": s["participant_id"], "revoked": s["revoked_at"] is not None}
                     for s in self.db.q("SELECT * FROM seats WHERE debate_id = ? ORDER BY id", (d["id"],))]
            debates.append({"id": d["id"], "title": d["title"], "status": d["status"], "phase": d["phase"],
                            "paper": d["paper_path"], "roles_off": sorted(self.roles_off(d["id"])), "seats": seats})
        people = []
        for p in self.db.q("SELECT * FROM participants WHERE removed_at IS NULL ORDER BY lower(name)"):
            devices = [{"id": i["id"], "label": i["label"], "debate": i["debate_id"], "created": i["created_at"],
                        "last_used": i["last_used"], "revoked": i["revoked_at"] is not None}
                       for i in self.db.q("SELECT * FROM sign_ins WHERE participant_id = ? ORDER BY id DESC", (p["id"],))]
            people.append({"id": p["id"], "name": p["name"], "kind": p["kind"], "access": p["access"],
                           "keeper": bool(p["is_admin"]), "devices": devices})
        papers = sorted(str(f.relative_to(self.docs_dir)).replace("\\", "/") for f in self.docs_dir.glob("*.md")
                        if f.name.lower() != "readme.md")
        return {"debates": debates, "people": people, "papers": papers, "roles": list(ROLES)}

    def revoke_sign_in(self, sid: int) -> dict:
        r = self.db.one("SELECT * FROM sign_ins WHERE id = ?", (sid,))
        if r is None:
            raise HallError("not_found", "no such sign-in")
        self.db.run("UPDATE sign_ins SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (self.now(), sid))
        return {"revoked": sid}

    def roles_off(self, did: int) -> set[str]:
        raw = self.debate(did)["roles_off"]
        return set(json.loads(raw)) if raw else set()

    def set_roles_off(self, did: int, roles) -> list[str]:
        """Which roles this debate leaves out (fixed roles with on/off, the keeper 11-07). The judge can't be off; a role
        that's off is refused for new seats and reassignments (security review: refused on the server, not just skipped)."""
        roles = sorted(set(roles or []))
        bad = [r for r in roles if r not in ROLES or r == "judge"]
        if bad:
            raise HallError("invalid", f"these can't be switched off: {', '.join(bad)} (the judge always sits)")
        held = [r for r in roles if self.db.one("SELECT 1 FROM seats WHERE debate_id = ? AND role = ? AND "
                                               "revoked_at IS NULL", (did, r))]
        if held:
            raise HallError("conflict", f"someone holds {', '.join(held)}: reassign or revoke that seat first")
        self.db.run("UPDATE debates SET roles_off = ? WHERE id = ?", (json.dumps(roles) if roles else None, did))
        return roles

    # ------------------------------------------------------------------ debates and seats
    def create_debate(self, title: str) -> int:
        title = (title or "").strip()
        if not title or len(title) > 200:
            raise HallError("invalid", "a debate needs a title (up to 200 characters)")
        return self.db.run("INSERT INTO debates (title, created_at) VALUES (?, ?)", (title, self.now()))

    def debate(self, did: int):
        d = self.db.one("SELECT * FROM debates WHERE id = ?", (did,))
        if d is None:
            raise HallError("not_found", "no such debate")
        return d

    def add_seat(self, did: int, name: str, role: str, kind: str = "agent",
                 participant: int | None = None, sign_in: bool = True) -> tuple[int, str | None]:
        """Seat someone in a debate. `participant`: an existing participant's id, chosen by the keeper; without it a
        NEW participant is made, and a name already in use is refused, never merged (security review, 11-07: a name match
        would hand the new seat to the other person's devices)."""
        self.debate(did)
        if participant is not None:
            p = self.db.one("SELECT * FROM participants WHERE id = ? AND removed_at IS NULL", (int(participant),))
            if p is None:
                raise HallError("not_found", "no such participant")
            name, kind = p["name"], p["kind"]
        name = (name or "").strip()
        if not NAME.match(name):
            raise HallError("invalid", "a name is letters, digits, spaces, _ or -, starting with a letter")
        if role not in ROLES:
            raise HallError("invalid", f"role is one of: {', '.join(ROLES)}")
        if kind not in ("person", "agent"):
            raise HallError("invalid", "kind is person or agent")
        if role in self.roles_off(did):
            raise HallError("forbidden", f"this debate leaves out the {role}")
        if role in ("judge", "clerk", "moderator", "aide") and self.db.one(
                "SELECT 1 FROM seats WHERE debate_id = ? AND role = ?", (did, role)):
            raise HallError("conflict", f"this debate already has a {role}")
        if self.db.one("SELECT 1 FROM seats WHERE debate_id = ? AND lower(name) = lower(?)", (did, name)):
            raise HallError("conflict", f"{name} already has a seat in this debate")
        if participant is None and self.db.one("SELECT 1 FROM participants WHERE lower(name) = lower(?) AND "
                                               "removed_at IS NULL", (name,)):
            raise HallError("conflict", f"someone is already called {name}: to seat them, pick them by id "
                                        "(--participant); for someone new, choose another name")
        pid = int(participant) if participant is not None else self.add_participant(name, kind)
        if self.db.one("SELECT 1 FROM seats WHERE debate_id = ? AND participant_id = ?", (did, pid)):
            raise HallError("conflict", f"{name} already has a seat in this debate")
        sid = self.db.run("INSERT INTO seats (debate_id, name, role, kind, token_hash, created_at, participant_id) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?)", (did, name, role, kind,
                                                           token_hash(secrets.token_urlsafe(32)), self.now(), pid))
        # A token for this seat's debate only (as a seat token always was); devices invited by the keeper get
        # sign-ins that reach every debate the participant sits in.
        return sid, (self.new_sign_in(pid, f"seat in debate {did}", debate_id=did) if sign_in else None)

    def set_role(self, actor, name: str, role: str) -> dict:
        """The judge reassigns a seat's role if the debate needs it (the keeper, 10-25). The judge's own seat stays the
        judge's (a change of judge is done by the keeper from the command line); one clerk, moderator and aide."""
        if actor["role"] != "judge":
            raise HallError("forbidden", "only the judge reassigns roles")
        if role not in ROLES or role == "judge":
            raise HallError("invalid", f"a new role is one of: {', '.join(r for r in ROLES if r != 'judge')}")
        did = actor["debate_id"]
        if role in self.roles_off(did):
            raise HallError("forbidden", f"this debate leaves out the {role}")
        target = self.seat_by_name(did, name)
        if target["role"] == "judge":
            raise HallError("invalid", "the judge's own seat stays the judge's")
        if role in ("clerk", "moderator", "aide") and self.db.one(
                "SELECT 1 FROM seats WHERE debate_id = ? AND role = ? AND id != ?", (did, role, target["id"])):
            raise HallError("conflict", f"this debate already has a {role}; reassign that seat first")
        if target["role"] == role:
            return {"name": target["name"], "role": role}
        self.db.run("UPDATE seats SET role = ? WHERE id = ?", (role, target["id"]))
        if role in ("audience", "aide") and self.floor_holder(did) == target["id"]:
            self.db.run("UPDATE debates SET floor_seat_id = NULL, floor_until = NULL WHERE id = ?", (did,))
        self.db.run("DELETE FROM floor_requests WHERE debate_id = ? AND seat_id = ?", (did, target["id"]))
        self._log(did, "hall", None, "notice", f"{actor['name']} makes {target['name']} the {role} "
                  f"(was {target['role']})", {"role_change": target["name"], "role": role})
        return {"name": target["name"], "role": role}

    def sign_in_for(self, token: str | None):
        """The live sign-in for a token, or None (revoked, removed holder, or a visitor whose debate has ended)."""
        if not token:
            return None
        si = self.db.one("SELECT i.*, p.access, p.removed_at AS p_removed FROM sign_ins i JOIN participants p "
                         "ON p.id = i.participant_id WHERE i.token_hash = ?", (token_hash(token),))
        if si is None or si["revoked_at"] is not None or si["p_removed"] is not None:
            return None
        if si["access"] == "visitor" and (si["debate_id"] is None
                                          or self.debate(si["debate_id"])["status"] == "closed"):
            return None   # a visitor's sign-in is for one debate and ends with it (security review, 11-07)
        now = self.now()
        if si["last_used"] is None or now - si["last_used"] > 60:
            self.db.run("UPDATE sign_ins SET last_used = ? WHERE id = ?", (now, si["id"]))
        return si

    def authenticate(self, token: str | None, debate: int | None = None):
        """The seat this token speaks for, read from the server's own seat row (the role never comes from the
        client). `debate`: which debate, when the holder sits in more than one; a sign-in scoped to a debate
        reaches that debate only. Raises "invalid" when the debate must be named."""
        si = self.sign_in_for(token)
        if si is None:
            return None
        if si["debate_id"] is not None:
            if debate is not None and int(debate) != si["debate_id"]:
                return None
            debate = si["debate_id"]
        if debate is not None:
            s = self.db.one("SELECT * FROM seats WHERE participant_id = ? AND debate_id = ?",
                            (si["participant_id"], int(debate)))
        else:
            rows = self.db.q("SELECT s.* FROM seats s JOIN debates d ON d.id = s.debate_id WHERE s.participant_id = ?"
                             " AND s.revoked_at IS NULL ORDER BY d.status = 'open' DESC, s.id DESC",
                             (si["participant_id"],))
            open_rows = [r for r in rows if self.debate(r["debate_id"])["status"] == "open"]
            # No open seat: the most recent closed one answers (a resting wake mod sees "closed" and waits), never
            # an error that would keep it from noticing the next debate (11-07).
            pick = open_rows if open_rows else rows[:1]
            if len(pick) > 1:
                raise HallError("invalid", "you sit in more than one debate: say which, with the X-Debate header "
                                           "or ?debate=<id>")
            s = pick[0] if pick else None
        return None if s is None or s["revoked_at"] is not None else s

    # ------------------------------------------------------------------ claim codes and revoking (10-28)
    CLAIM_DAYS = 7

    def issue_claim(self, did: int, name: str) -> str:
        """A one-time claim code for a seat (security review: a person never gets the token itself in a chat or a URL). The
        seat's old token stops working; the code swaps for a fresh token on first use, within CLAIM_DAYS."""
        s = self.seat_by_name(did, name)
        self.db.run("UPDATE seats SET revoked_at = NULL WHERE id = ?", (s["id"],))
        # The seat's old token stops, as before: its sign-ins for this debate (other debates' are untouched).
        self.db.run("UPDATE sign_ins SET revoked_at = ? WHERE participant_id = ? AND debate_id = ? AND revoked_at IS NULL",
                    (self.now(), s["participant_id"], did))
        return self.new_invite(s["participant_id"], did)

    def new_invite(self, pid: int, did: int | None = None) -> str:
        """A one-time claim code (an invite, as on AI-Beach): swapped once for a device sign-in, within CLAIM_DAYS."""
        code = "-".join(secrets.token_hex(2).upper() for _ in range(4))          # e.g. 3F9A-07C2-B1E4-5D6A
        self.db.run("INSERT INTO invites (participant_id, code_hash, debate_id, expires_at, created_at) VALUES "
                    "(?, ?, ?, ?, ?)", (pid, token_hash(code), did, self.now() + self.CLAIM_DAYS * 86400, self.now()))
        return code

    def claim(self, code: str, audience_of: int | None = None) -> dict:
        """audience_of: the audience door's debate. There the code is checked BEFORE it's used, so a judge's or
        advocate's code tried at the door is refused and still works on the hall's own computer (security review, 10-28:
        otherwise the door could burn any seat's code and lock that seat out)."""
        code = (code or "").strip().upper()
        inv = self.db.one("SELECT i.*, p.access, p.removed_at AS p_removed, p.name AS p_name FROM invites i JOIN "
                          "participants p ON p.id = i.participant_id WHERE i.code_hash = ?",
                          (token_hash(code),)) if code else None
        if inv is None or inv["used_at"] is not None or inv["expires_at"] < self.now() or inv["p_removed"]:
            raise HallError("forbidden", "that claim code isn't valid (used, expired or mistyped)")
        s = self.db.one("SELECT * FROM seats WHERE participant_id = ? AND debate_id = ?",
                        (inv["participant_id"], inv["debate_id"])) if inv["debate_id"] is not None else None
        # Checked BEFORE the code is used (security review, 10-28): a speaking seat's code tried at the audience door is
        # refused and still works on the hall's own computer.
        if audience_of is not None and (s is None or s["role"] != "audience" or s["debate_id"] != audience_of):
            raise HallError("forbidden", "this door is for the audience; claim this seat on the hall's own computer")
        if s is not None and s["revoked_at"] is not None:
            raise HallError("forbidden", "that claim code isn't valid (used, expired or mistyped)")
        self.db.run("UPDATE invites SET used_at = ? WHERE id = ?", (self.now(), inv["id"]))
        # A visitor's device reaches its one debate; a resident's device every debate it sits in. Seat claims made
        # for one debate (the judge's `hall claim`) stay scoped to it, as seat codes always were.
        scope = inv["debate_id"] if (inv["access"] == "visitor" or audience_of is not None or s is not None) else None
        token = self.new_sign_in(inv["participant_id"], "claimed device", debate_id=scope)
        return {"token": token, "name": s["name"] if s else inv["p_name"], "role": s["role"] if s else None,
                "debate": s["debate_id"] if s else None}

    def revoke(self, actor, name: str) -> dict:
        """The judge revokes a seat's token at once (security review: any audience token, any time)."""
        if actor["role"] != "judge":
            raise HallError("forbidden", "only the judge revokes a seat")
        target = self.seat_by_name(actor["debate_id"], name)
        if target["role"] == "judge":
            raise HallError("invalid", "the judge's own seat can't be revoked here")
        self.db.run("UPDATE seats SET revoked_at = ?, claim_hash = NULL WHERE id = ?", (self.now(), target["id"]))
        self._log(actor["debate_id"], "hall", None, "notice", f"The judge revokes {target['name']}'s seat",
                  {"revoked": target["name"]})
        return {"revoked": target["name"]}

    def seats(self, did: int) -> list[dict]:
        return [{"id": s["id"], "name": s["name"], "role": s["role"], "kind": s["kind"]}
                for s in self.db.q("SELECT * FROM seats WHERE debate_id = ? ORDER BY id", (did,))]

    def seat_by_name(self, did: int, name: str):
        s = self.db.one("SELECT * FROM seats WHERE debate_id = ? AND lower(name) = lower(?)", (did, name.strip()))
        if s is None:
            raise HallError("not_found", f"no seat named {name!r} in this debate")
        return s

    # ------------------------------------------------------------------ who may read and post where
    # Who reads what during a debate (the keeper, 10-25). Huddles stay with their own team, so a team shares what it
    # decided to say, not the brainstorms it threw out. The Gallery is the audience's room, read by the moderator,
    # who may bring a question from it into the Hall. The aide room is the judge and their aide alone.
    READS = {
        "judge": ("hall", "aide", "chair"), "aide": ("hall", "aide"),
        "moderator": ("hall", "gallery", "chair"), "audience": ("hall", "gallery"),
        "clerk": ("hall", "chair"), "witness": ("hall",),
        "advocate": ("hall", "team_a"), "reader_a": ("hall", "team_a"),
        "adversary": ("hall", "team_b"), "reader_b": ("hall", "team_b"),
    }

    def can_read(self, seat, room: str) -> bool:
        off = self.roles_off(seat["debate_id"])
        # A room whose role is switched off isn't shown at all (11-07): no aide, no aide room; no audience, no Gallery.
        if (room == "aide" and "aide" in off) or (room == "gallery" and "audience" in off):
            return False
        if room == "aide":
            return seat["role"] in ("judge", "aide")     # stays private after the debate too
        if self.debate(seat["debate_id"])["status"] == "closed" and seat["role"] != "aide":
            return room in ROOMS                         # after the debate, the huddles and Gallery open for review
        return room in self.READS.get(seat["role"], ("hall",))

    def floor_holder(self, did: int) -> int | None:
        d = self.debate(did)
        if d["floor_seat_id"] and (d["floor_until"] is None or d["floor_until"] > self.now()):
            return d["floor_seat_id"]
        return None

    def can_post(self, seat, room: str) -> bool:
        d = self.debate(seat["debate_id"])
        if d["status"] == "closed":
            return False
        if room == "aide" or seat["role"] == "aide":     # the aide speaks only with the judge, never in the debate
            return room == "aide" and seat["role"] in ("aide", "judge")
        if room == "gallery":
            return seat["role"] in ("audience", "moderator")
        if room == "chair":            # the judge and the moderator, about running the debate (the keeper, 10-25); the
            return seat["role"] in ("judge", "moderator", "clerk")   # clerk asks them there (the keeper, 10-30)
        if room in ("team_a", "team_b"):
            return TEAM.get(seat["role"]) == room[-1]
        if room == "hall":
            return seat["role"] in CHAIR or self.floor_holder(d["id"]) == seat["id"]
        return False

    def _require_chair(self, seat) -> None:
        if seat["role"] not in CHAIR:
            raise HallError("forbidden", "only the judge or the moderator can do that")

    # ------------------------------------------------------------------ the log
    def _log(self, did: int, room: str, seat_id: int | None, kind: str, text: str, data: dict | None = None) -> int:
        sha = self.debate(did)["paper_sha"]
        return self.db.run("INSERT INTO messages (debate_id, room, seat_id, kind, text, data, paper_sha, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (did, room, seat_id, kind, text, json.dumps(data) if data else None, sha, self.now()))

    def _attend(self, did: int, seat_id: int, kind: str, ref: int | None) -> None:
        self.db.run("INSERT INTO attention (debate_id, seat_id, kind, ref, created_at) VALUES (?, ?, ?, ?, ?)",
                    (did, seat_id, kind, ref, self.now()))

    def post(self, seat, room: str, text: str) -> dict:
        if room not in ROOMS:
            raise HallError("invalid", f"room is one of: {', '.join(ROOMS)}")
        text = (text or "").strip()
        if not text:
            raise HallError("invalid", "say something")
        if len(text) > MAX_TEXT:
            raise HallError("invalid", f"at most {MAX_TEXT} characters a line")
        if not self.can_post(seat, room):
            if seat["role"] == "aide":
                raise HallError("forbidden", "the judge's aide speaks only with the judge, in the aide room")
            if room == "hall" and self.debate(seat["debate_id"])["status"] == "open":
                raise HallError("not_your_floor", "only the floor holder (or the judge or moderator) speaks in the "
                                "Hall; ask for the floor with POST /floor/request")
            raise HallError("forbidden", f"you can't post in {room}")
        did = seat["debate_id"]
        mid = self._log(did, room, seat["id"], "say", text)
        if room in ("team_a", "team_b"):                        # a teammate's line in the huddle wakes the others
            team = room[-1]
            for s in self.db.q("SELECT id, role FROM seats WHERE debate_id = ? AND id != ?", (did, seat["id"])):
                if TEAM.get(s["role"]) == team:
                    self._attend(did, s["id"], "huddle", mid)
        if room == "gallery" and seat["role"] == "audience":   # the moderator reads the Gallery for questions
            for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role = 'moderator'", (did,)):
                self._attend(did, s["id"], "gallery", mid)
        if room == "chair" and seat["role"] in ("judge", "clerk"):   # the judge or the clerk asking wakes the moderator
            self._wake_moderator(did, "chair", mid)
        if room == "aide" and seat["role"] == "judge":     # the judge asking their aide wakes it
            for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role = 'aide'", (did,)):
                self._attend(did, s["id"], "aide", mid)
        # The chair calling on someone wakes them: in the Hall, and in the chair room (where the clerk is called on).
        if seat["role"] in CHAIR and room in ("hall", "chair"):
            for name in set(MENTION.findall(text)):
                s = self.db.one("SELECT * FROM seats WHERE debate_id = ? AND lower(name) = lower(?)", (did, name))
                if s and s["id"] != seat["id"] and self.can_read(s, room):   # never wake a seat to a room it can't read
                    # In the chair room the answer belongs there too, so it wakes as "chair" ("say chair to answer"),
                    # not "named" (which tells a seat to ask for the Hall's floor).
                    self._attend(did, s["id"], "chair" if room == "chair" else "named", mid)
        return {"id": mid}

    def relay(self, actor, message_id: int) -> dict:
        """The moderator brings a question from the Gallery into the Hall, credited to who asked it (the keeper, 10-25)."""
        if actor["role"] != "moderator":
            raise HallError("forbidden", "the moderator brings Gallery questions into the Hall")
        did = actor["debate_id"]
        m = self.db.one("SELECT m.*, s.name AS who FROM messages m LEFT JOIN seats s ON s.id = m.seat_id "
                        "WHERE m.id = ? AND m.debate_id = ? AND m.room = 'gallery' AND m.kind = 'say'", (message_id, did))
        if m is None:
            raise HallError("not_found", "no such line in the Gallery")
        if self.debate(did)["status"] == "closed":
            raise HallError("forbidden", "the debate is closed")
        mid = self._log(did, "hall", actor["id"], "relay", f"From the Gallery, {m['who']} asks: {m['text']}",
                        {"from": m["id"]})
        return {"id": mid}

    def feed(self, seat, after: int = 0, limit: int = 200) -> list[dict]:
        rooms = [r for r in ROOMS if self.can_read(seat, r)]     # filtered in the query, so paging never skips
        marks = ",".join("?" * len(rooms))
        rows = self.db.q("SELECT m.*, s.name AS who, s.role AS who_role FROM messages m LEFT JOIN seats s "
                         f"ON s.id = m.seat_id WHERE m.debate_id = ? AND m.id > ? AND m.room IN ({marks}) "
                         "ORDER BY m.id LIMIT ?", (seat["debate_id"], after, *rooms, max(1, min(limit, 1000))))
        return [{"id": r["id"], "room": r["room"], "who": r["who"] or "the hall", "role": r["who_role"],
                 "kind": r["kind"], "text": r["text"], "data": json.loads(r["data"]) if r["data"] else None,
                 "at": r["created_at"], "mine": r["seat_id"] == seat["id"]} for r in rows]

    def attention(self, seat, after: int = 0) -> list[dict]:
        self._sweep_huddle(seat["debate_id"])
        if seat["wake_off"] is not None:
            return []            # switched off by the seat itself: the items wait, unread, for `wake on`
        return [{"id": r["id"], "kind": r["kind"], "ref": r["ref"], "at": r["created_at"]}
                for r in self.db.q("SELECT * FROM attention WHERE seat_id = ? AND id > ? ORDER BY id",
                                   (seat["id"], after))]

    # ------------------------------------------------------------------ the floor
    def give_floor(self, actor, name: str, minutes: float | None = None) -> dict:
        self._require_chair(actor)
        did = actor["debate_id"]
        running = self._running_huddle(did)
        if running:
            raise HallError("conflict", "a huddle is running: the Hall waits until both teams are done, its time is up, "
                                        "or the chair ends it (huddle-end)")
        target = self.seat_by_name(did, name)
        if target["role"] in ("audience", "aide"):
            raise HallError("invalid", "the audience speaks in the Gallery, and the judge's aide only with the judge")
        if minutes is not None and not (0 < float(minutes) <= 120):
            raise HallError("invalid", "minutes is between 0 and 120")
        until = self.now() + float(minutes) * 60 if minutes else None
        self.db.run("UPDATE debates SET floor_seat_id = ?, floor_until = ?, waiting = 0 WHERE id = ?",
                    (target["id"], until, did))
        self.db.run("DELETE FROM floor_requests WHERE debate_id = ? AND seat_id = ?", (did, target["id"]))
        mid = self._log(did, "hall", None, "notice", f"{actor['name']} gives the floor to {target['name']}"
                        + (f" for {minutes:g} minutes" if minutes else ""), {"floor": target["name"], "until": until})
        self._attend(did, target["id"], "floor", mid)
        self._wake_audience(did, "speaker", mid)
        side = TEAM.get(target["role"])
        if side:
            self._huddle_prompt(did, side, f"{target['name']} has the floor now. Teammates: post your best points here. "
                                           f"{target['name']}: read this huddle before you speak.")
        return self.floor_state(did)

    def _wake_audience(self, did: int, kind: str, ref: int) -> None:
        """Agents in the audience are FULLY PRESENT (the keeper, 10-28: "I want agents to be able to ask questions as it
        presents"): woken when a new speaker has the floor, a phase opens, or the presenter shows a passage; never on
        every line. People in the audience are watching the page and need no wake."""
        for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role = 'audience' AND kind = 'agent' "
                           "AND revoked_at IS NULL", (did,)):
            self._attend(did, s["id"], kind, ref)

    def take_floor(self, actor) -> dict:
        self._require_chair(actor)
        did = actor["debate_id"]
        self.db.run("UPDATE debates SET floor_seat_id = NULL, floor_until = NULL WHERE id = ?", (did,))
        self._log(did, "hall", None, "notice", f"{actor['name']} takes the floor back", {"floor": None})
        return self.floor_state(did)

    def yield_floor(self, seat) -> dict:
        did = seat["debate_id"]
        if self.floor_holder(did) != seat["id"]:
            raise HallError("invalid", "you don't hold the floor")
        self.db.run("UPDATE debates SET floor_seat_id = NULL, floor_until = NULL WHERE id = ?", (did,))
        mid = self._log(did, "hall", None, "notice", f"{seat['name']} yields the floor", {"floor": None})
        if self.paced(did):
            # A person judges: the debate waits for their Continue, so it moves at a pace they can read (the keeper, 10-25:
            # "otherwise you guys could write a book in about 30 seconds").
            self.db.run("UPDATE debates SET waiting = 1 WHERE id = ?", (did,))
            self._log(did, "hall", None, "notice", "Waiting on the judge's Continue.", {"waiting": True})
        else:
            self._wake_moderator(did, "yielded", mid)    # agents only: the moderator moves things along at once
        return self.floor_state(did)

    def paced(self, did: int) -> bool:
        """Does the debate wait for the judge's Continue after each turn? Set by the judge; by default, yes when the
        judge is a person, no when it's an agent (an all-agent debate shouldn't slow down for nobody)."""
        d = self.debate(did)
        if d["paced"] is not None:
            return bool(d["paced"])
        judge = self.db.one("SELECT kind FROM seats WHERE debate_id = ? AND role = 'judge'", (did,))
        return bool(judge and judge["kind"] == "person")

    def set_paced(self, actor, on: bool) -> dict:
        if actor["role"] != "judge":
            raise HallError("forbidden", "only the judge sets the pace")
        did = actor["debate_id"]
        self.db.run("UPDATE debates SET paced = ?, waiting = CASE WHEN ? THEN waiting ELSE 0 END WHERE id = ?",
                    (1 if on else 0, 1 if on else 0, did))
        self._log(did, "hall", None, "notice", "The judge paces the debate: after each turn it waits for their Continue."
                  if on else "The debate moves on without waiting for the judge's Continue.", {"paced": bool(on)})
        return self.floor_state(did)

    def _wake_moderator(self, did: int, kind: str, ref: int) -> None:
        for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role = 'moderator'", (did,)):
            self._attend(did, s["id"], kind, ref)

    def nudge_moderator(self, actor) -> dict:
        """The judge's "Moderator, please continue" (the keeper, 10-25: they shouldn't have to run the floor themselves)."""
        if actor["role"] != "judge":
            raise HallError("forbidden", "only the judge sends the moderator on")
        did = actor["debate_id"]
        self.db.run("UPDATE debates SET waiting = 0 WHERE id = ?", (did,))
        mid = self._log(did, "hall", actor["id"], "notice", f"{actor['name']}: Moderator, please continue.", {"nudge": True})
        self._wake_moderator(did, "nudge", mid)
        return self.floor_state(did)

    def request_floor(self, seat) -> dict:
        did = seat["debate_id"]
        if seat["role"] in ("audience", "aide"):
            raise HallError("invalid", "the audience asks in the Gallery, and the judge's aide doesn't take the floor")
        fresh = not self.db.one("SELECT 1 FROM floor_requests WHERE debate_id = ? AND seat_id = ?", (did, seat["id"]))
        self.db.run("INSERT OR IGNORE INTO floor_requests (debate_id, seat_id, requested_at) VALUES (?, ?, ?)",
                    (did, seat["id"], self.now()))
        if fresh:   # the moderator hears about it (the keeper, a test debate: nobody saw a seat's ask)
            mid = self._log(did, "hall", None, "notice", f"{seat['name']} asks for the floor", {"asks": seat["name"]})
            self._wake_moderator(did, "request", mid)
        return self.floor_state(did)

    def withdraw_request(self, seat) -> dict:
        """Take back your own ask for the floor once it's moot (security review, a test debate: the judge's rulings covered
        the point, and the ask sat in the queue)."""
        did = seat["debate_id"]
        if not self.db.one("SELECT 1 FROM floor_requests WHERE debate_id = ? AND seat_id = ?", (did, seat["id"])):
            raise HallError("invalid", "you haven't asked for the floor")
        self.db.run("DELETE FROM floor_requests WHERE debate_id = ? AND seat_id = ?", (did, seat["id"]))
        self._log(did, "hall", None, "notice", f"{seat['name']} withdraws their ask for the floor", {"withdraws": seat["name"]})
        return self.floor_state(did)

    def ping_judge(self, actor, note: str) -> dict:
        """The moderator asks the judge for something ("Judge, Whomever asks for the floor: may I give it?"), shown
        on their page as a banner they can't miss (the keeper, 10-25)."""
        if actor["role"] != "moderator":
            raise HallError("forbidden", "the moderator pings the judge")
        note = (note or "").strip()
        if not note or len(note) > 500:
            raise HallError("invalid", "say what you need from the judge (up to 500 characters)")
        did = actor["debate_id"]
        if self.debate(did)["status"] == "closed":
            raise HallError("forbidden", "the debate is closed")
        mid = self._log(did, "chair", actor["id"], "say", note, {"ping": True})   # lights up their Moderator tab
        return {"id": mid}

    def floor_state(self, did: int) -> dict:
        d = self.debate(did)
        holder = self.floor_holder(did)
        name = self.db.one("SELECT name FROM seats WHERE id = ?", (holder,))["name"] if holder else None
        queue = [r["name"] for r in self.db.q("SELECT s.name FROM floor_requests r JOIN seats s ON s.id = r.seat_id "
                                              "WHERE r.debate_id = ? ORDER BY r.requested_at, r.rowid", (did,))]
        return {"holder": name, "until": d["floor_until"] if holder else None, "queue": queue,
                "phase": d["phase"], "status": d["status"], "paced": self.paced(did), "waiting": bool(d["waiting"]),
                "huddle": self.huddle_state(did) if d["status"] == "open" and self._running_huddle(did) else None}

    def set_phase(self, actor, phase: str) -> dict:
        self._require_chair(actor)
        if phase not in PHASES:
            raise HallError("invalid", f"phase is one of: {', '.join(PHASES)}")
        did = actor["debate_id"]
        self.db.run("UPDATE debates SET phase = ?, waiting = 0 WHERE id = ?", (phase, did))
        # A new phase starts with a clean queue: asks for the floor belong to the phase they were made in (the moderator,
        # a test debate: "asking: Whomever" lingered when that seat no longer owed anything).
        self.db.run("DELETE FROM floor_requests WHERE debate_id = ?", (did,))
        mid = self._log(did, "hall", None, "notice", f"{actor['name']} opens the phase: {phase}", {"phase": phase})
        self._wake_audience(did, "phase", mid)
        self._wake_debaters(did, "phase", mid)
        if phase not in ("done",):
            for team in ("a", "b"):
                self._huddle_prompt(did, team, f"New phase: {phase}. Plan it together here before anyone speaks.")
        return self.floor_state(did)

    DEBATERS = ("advocate", "adversary", "reader_a", "reader_b", "witness", "clerk")

    def _wake_debaters(self, did: int, kind: str, ref: int, roles=None) -> None:
        """The release blocker (the keeper's debrief, 10-30: "would be considered a broken app"): the readers were
        never woken, so both huddles and the Gallery stayed empty. Every debating seat now hears each phase open, and
        a side's reader hears when that side has the floor or an objection to answer."""
        roles = roles or self.DEBATERS
        for s in self.db.q("SELECT id, role FROM seats WHERE debate_id = ? AND revoked_at IS NULL", (did,)):
            if s["role"] in roles:
                self._attend(did, s["id"], kind, ref)

    # ------------------------------------------------------------------ the huddle step (debrief, 10-30)
    def _huddle_prompt(self, did: int, team: str, text: str) -> None:
        """The hall reminds BOTH halves of a team to confer, inside their own huddle (the keeper, 11-07: in the first
        real debate the main debaters forgot their readers, so the huddles were never spoken to). The note sits in
        the huddle, so it's in the record, and it wakes every seat on that team."""
        mid = self._log(did, f"team_{team}", None, "notice", text, {"huddle_prompt": True})
        self._wake_debaters(did, "huddle_prompt", mid, roles=tuple(r for r, t in TEAM.items() if t == team))

    HUDDLE_MINUTES = (1, 60)

    def call_huddle(self, actor, minutes: float = 10) -> dict:
        """The chair grants a block of time for the teams to huddle (the keeper, 11-07; a cold reader's idea, 10-30:
        after openings, before objections are filed, when a reader's ranked list can still shape what's filed). The
        Hall waits: the floor is cleared and can't be given until both teams are done, the time is up, or the chair
        ends it. Both halves of each team are prompted in their huddle."""
        self._require_chair(actor)
        did = actor["debate_id"]
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            raise HallError("invalid", "minutes is a number") from None
        if not self.HUDDLE_MINUTES[0] <= minutes <= self.HUDDLE_MINUTES[1]:
            raise HallError("invalid", "a huddle lasts 1 to 60 minutes")
        if self._running_huddle(did):
            raise HallError("conflict", "a huddle is already running")
        now = self.now()
        hid = self.db.run("INSERT INTO huddles (debate_id, called_by, called_at, ends_at) VALUES (?, ?, ?, ?)",
                          (did, actor["id"], now, now + minutes * 60))
        self.db.run("UPDATE debates SET floor_seat_id = NULL, floor_until = NULL, waiting = 0 WHERE id = ?", (did,))
        mid = self._log(did, "hall", None, "notice", f"{actor['name']} calls a huddle for {minutes:g} minutes: the Hall "
                        "waits while each team talks in its own huddle, then says it's done", {"huddle": hid})
        for team in ("a", "b"):
            note = self._log(did, f"team_{team}", None, "notice", f"Huddle time ({minutes:g} minutes): talk it through "
                             "here, both of you, then run huddle-done.", {"huddle": hid})
            self._wake_debaters(did, "huddle_call", note, roles=tuple(r for r, t in TEAM.items() if t == team))
        return self.huddle_state(did)

    def _running_huddle(self, did: int):
        self._sweep_huddle(did)
        return self.db.one("SELECT * FROM huddles WHERE debate_id = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                           (did,))

    def _sweep_huddle(self, did: int) -> None:
        """A huddle whose time is up ends on the next look (no timer thread): a notice, and the chair is woken."""
        h = self.db.one("SELECT * FROM huddles WHERE debate_id = ? AND ended_at IS NULL AND ends_at IS NOT NULL AND "
                        "ends_at <= ? ORDER BY id DESC LIMIT 1", (did, self.now()))
        if h:
            self._end_huddle(did, h["id"], "Huddle time is up: the Hall is open again")

    def _end_huddle(self, did: int, hid: int, why: str) -> None:
        self.db.run("UPDATE huddles SET ended_at = ? WHERE id = ? AND ended_at IS NULL", (self.now(), hid))
        mid = self._log(did, "hall", None, "notice", why, {"huddle_ended": hid})
        self._wake_chair_seats(did, "huddle_done", mid)

    def end_huddle(self, actor) -> dict:
        self._require_chair(actor)
        h = self._running_huddle(actor["debate_id"])
        if h is None:
            raise HallError("conflict", "no huddle is running")
        self._end_huddle(actor["debate_id"], h["id"], f"{actor['name']} ends the huddle: the Hall is open again")
        return self.huddle_state(actor["debate_id"])

    def huddle_done(self, seat) -> dict:
        team = TEAM.get(seat["role"])
        if team is None:
            raise HallError("forbidden", "only a team seat says its team is done huddling")
        did = seat["debate_id"]
        h = self.db.one("SELECT * FROM huddles WHERE debate_id = ? ORDER BY id DESC LIMIT 1", (did,))
        if h is None:
            raise HallError("conflict", "no huddle has been called")
        if h[f"done_{team}"] is None and h["ended_at"] is None:
            self.db.run(f"UPDATE huddles SET done_{team} = ? WHERE id = ?", (self.now(), h["id"]))
            mid = self._log(did, "hall", None, "notice", f"Team {team.upper()} is done huddling", {"huddle_done": team})
            other = "b" if team == "a" else "a"
            if h[f"done_{other}"] is not None:
                self._end_huddle(did, h["id"], "Both teams are done huddling: the Hall is open again")
            else:
                self._wake_chair_seats(did, "huddle_done", mid)
        return self.huddle_state(did)

    def huddle_state(self, did: int) -> dict | None:
        self._sweep_huddle(did)
        h = self.db.one("SELECT * FROM huddles WHERE debate_id = ? ORDER BY id DESC LIMIT 1", (did,))
        if h is None:
            return None
        return {"called_at": h["called_at"], "ends_at": h["ends_at"], "running": h["ended_at"] is None,
                "team_a": "done" if h["done_a"] else "still talking",
                "team_b": "done" if h["done_b"] else "still talking"}

    def _wake_chair_seats(self, did: int, kind: str, ref: int) -> None:
        for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role IN ('moderator', 'judge') AND kind = 'agent'"
                           " AND revoked_at IS NULL", (did,)):
            self._attend(did, s["id"], kind, ref)

    def huddle_summary(self, did: int, team: str) -> str:
        """One line about a team's huddle, for its seats' status (the judge's aide's idea, 10-30)."""
        room = f"team_{team}"
        n = self.db.one("SELECT COUNT(*) AS n, MAX(created_at) AS t FROM messages WHERE debate_id = ? AND room = ? "
                        "AND kind = 'say'", (did, room))
        if not n["n"]:
            return f"Huddle (Team {team.upper()}): no posts yet"
        who = self.db.one("SELECT s.name FROM messages m JOIN seats s ON s.id = m.seat_id WHERE m.debate_id = ? AND "
                          "m.room = ? AND m.kind = 'say' ORDER BY m.id DESC LIMIT 1", (did, room))
        return f"Huddle (Team {team.upper()}): {n['n']} posts, the latest by {who['name'] if who else 'someone'}"

    def set_wake(self, seat, on: bool) -> dict:
        """An agent switches its own wakes off or on (the keeper, 10-30). While off, /attention answers empty and the
        items wait; the chair's check and a Hall notice say who switched off and when. The keeper's own switch in
        the wake mod stays the override."""
        if seat["kind"] != "agent":
            raise HallError("invalid", "wakes are for AI seats; a person watches the page")
        did = seat["debate_id"]
        now_off = seat["wake_off"] is not None
        if on and now_off:
            self.db.run("UPDATE seats SET wake_off = NULL WHERE id = ?", (seat["id"],))
            self._log(did, "hall", None, "notice", f"{seat['name']} switched their wakes back on", {"wake": "on"})
        elif not on and not now_off:
            self.db.run("UPDATE seats SET wake_off = ? WHERE id = ?", (self.now(), seat["id"]))
            self._log(did, "hall", None, "notice", f"{seat['name']} switched their wakes off", {"wake": "off"})
        return {"wakes": "on" if on else "off"}

    def draft_now(self, actor, oid: int | None = None) -> dict:
        """The judge's "Clerk, draft now" (debrief, 10-30), for one objection or every one still awaiting a ruling."""
        if actor["role"] != "judge":
            raise HallError("forbidden", "the judge asks the clerk to draft")
        did = actor["debate_id"]
        led = [o for o in self.ledger(did) if o["status"] not in VERDICTS]
        if oid is not None:
            led = [o for o in led if o["id"] == int(oid)]
            if not led:
                raise HallError("not_found", "no objection with that number awaits a ruling")
        if not led:
            raise HallError("conflict", "nothing awaits a ruling")
        nums = ", ".join(f"#{o['id']}" for o in led)
        mid = self._log(did, "chair", actor["id"], "say", f"Clerk, please draft now: {nums}", {"draft_now": [o["id"] for o in led]})
        for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role = 'clerk' AND revoked_at IS NULL", (did,)):
            for o in led:
                self._attend(did, s["id"], "draft", o["id"])
        return {"asked": [o["id"] for o in led]}

    def listening(self, did: int) -> list[dict]:
        """Who's listening (the chair's pre-start check, 10-30): each AI seat's last check for wakes."""
        now = self.now()
        out = []
        for s in self.db.q("SELECT name, role, last_check, wake_off FROM seats WHERE debate_id = ? AND kind = 'agent' "
                           "AND revoked_at IS NULL ORDER BY id", (did,)):
            ago = None if s["last_check"] is None else round(now - s["last_check"])
            out.append({"name": s["name"], "role": s["role"], "seconds_ago": ago,
                        "listening": ago is not None and ago < 300 and s["wake_off"] is None,
                        "wakes_off_since": s["wake_off"]})
        return out

    def close_debate(self, actor) -> dict:
        if actor["role"] != "judge":
            raise HallError("forbidden", "only the judge closes the debate")
        did = actor["debate_id"]
        self.db.run("UPDATE debates SET status = 'closed', closed_at = ?, floor_seat_id = NULL, floor_until = NULL "
                    "WHERE id = ?", (self.now(), did))
        self._log(did, "hall", None, "notice", f"{actor['name']} closes the debate; the huddles are open for everyone to read now",
                  {"status": "closed"})
        # The audience door is open only during the debate (the keeper + a security review, 10-28, a hard rule): closing it expires
        # every audience seat's token at once; the hall's door listener stops when it sees the debate closed.
        self.db.run("UPDATE seats SET revoked_at = ?, claim_hash = NULL WHERE debate_id = ? AND role = 'audience' "
                    "AND revoked_at IS NULL", (self.now(), did))
        count = self.db.one("SELECT COUNT(*) AS n FROM seats WHERE debate_id = ? AND role = 'audience' AND revoked_at "
                            "IS NOT NULL", (did,))["n"]
        self._log(did, "hall", None, "notice",
                  f"The audience door is closed: {count} audience seat{'s' if count != 1 else ''} expired. Keeper: turn "
                  f"the firewall rule off ({FIREWALL_OFF})", {"door_closed": True, "expired": count})
        return self.floor_state(did)

    # ------------------------------------------------------------------ the paper
    def set_paper(self, actor, rel_path: str) -> dict:
        """Point the debate at a document inside debate_docs/ (the judge or moderator). Its hash is recorded, and
        every line and ruling after this carries it, so rulings refer to the text everyone saw."""
        self._require_chair(actor)
        return self.point_at_paper(actor["debate_id"], rel_path)

    def point_at_paper(self, did: int, rel_path: str) -> dict:
        """set_paper without a seat: the keeper sets the paper when the debate is made, so every seat (the cold
        readers prepare first) reads the same hashed text from the start (Reader B, a test debate)."""
        path = (self.docs_dir / rel_path).resolve()
        if self.docs_dir not in path.parents or not path.is_file() or path.suffix.lower() != ".md":
            raise HallError("invalid", "the paper is a .md file inside debate_docs/")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.db.run("UPDATE debates SET paper_path = ?, paper_sha = ? WHERE id = ?",
                    (path.relative_to(self.docs_dir).as_posix(), sha, did))
        self._log(did, "hall", None, "notice", f"The paper is {path.name} (version {sha[:12]})",
                  {"paper": path.name, "sha": sha})
        return {"paper": path.name, "sha": sha}

    def paper_text(self, did: int) -> tuple[str, str] | None:
        d = self.debate(did)
        if not d["paper_path"]:
            return None
        data = (self.docs_dir / d["paper_path"]).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha != d["paper_sha"]:
            raise HallError("conflict", "the paper changed on disk since it was set; the chair sets it again to "
                            "start a new version")
        return data.decode("utf-8"), sha

    def show(self, seat, target: str) -> dict:
        """Point everyone's viewer at a section ("4.2") or a quoted phrase. Whoever may speak in the Hall may."""
        target = (target or "").strip()
        if not target or len(target) > 300:
            raise HallError("invalid", "show a section number or a short quoted phrase")
        if not self.can_post(seat, "hall"):
            raise HallError("not_your_floor", "only the floor holder (or the judge or moderator) points the viewer")
        quoted = len(target) > 1 and target[0] == target[-1] == '"'
        data = {"phrase": target[1:-1]} if quoted else {"section": target}
        mid = self._log(seat["debate_id"], "hall", seat["id"], "show", f"shows {target}", data)
        self._wake_audience(seat["debate_id"], "show", mid)
        return {"id": mid, **data}

    def paper_section(self, did: int, section: str | None) -> dict:
        """The paper as text for a reader on another machine (an audience agent can't open the local file): all of
        it, or one numbered section ("3.2" and its subsections), with its version."""
        got = self.paper_text(did)
        if got is None:
            raise HallError("not_found", "no paper is set yet")
        text, sha = got
        if not section:
            return {"text": text, "sha": sha, "section": None}
        want = section.strip().lstrip("§").strip()
        lines, out, depth = text.splitlines(), [], None
        for line in lines:
            m = re.match(r"^(#+)\s+(\d+(?:\.\d+)*)\.?\s", line)
            if m:
                level, num = len(m.group(1)), m.group(2)
                if depth is None and num == want:
                    depth = level
                elif depth is not None and level <= depth:
                    break
            if depth is not None:
                out.append(line)
        if not out:
            raise HallError("not_found", f"no section {want} in the paper")
        return {"text": "\n".join(out), "sha": sha, "section": want}

    def citations(self) -> list[str]:
        folder = self.docs_dir / "citations"
        return sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".md", ".txt")
                      and p.name.lower() != "readme.md") if folder.is_dir() else []

    def citation(self, name: str) -> dict:
        """One cited source as text (.md or .txt in debate_docs/citations), read only, with its sha."""
        if name not in self.citations():
            raise HallError("not_found", "no such citation (see the list)")
        data = (self.docs_dir / "citations" / name).read_bytes()
        return {"name": name, "text": data.decode("utf-8", "replace"), "sha": hashlib.sha256(data).hexdigest()}

    # ------------------------------------------------------------------ the objection ledger
    def object(self, seat, anchor: str, claim: str, why: str, quote: str | None = None,
               depends_on: list | None = None) -> dict:
        if seat["role"] in ("judge", "clerk", "moderator", "aide", "audience"):
            raise HallError("forbidden", "objections come from the debaters, their readers or the witness")
        did = seat["debate_id"]
        if self.debate(did)["status"] == "closed":
            raise HallError("forbidden", "the debate is closed")
        anchor, claim, why = (anchor or "").strip(), (claim or "").strip(), (why or "").strip()
        quote = (quote or "").strip() or None
        if not anchor or not claim or not why:
            raise HallError("invalid", "an objection needs a passage (section or anchor), the claim it attacks, and why")
        if max(len(anchor), len(claim), len(why)) > MAX_TEXT:
            raise HallError("invalid", f"at most {MAX_TEXT} characters a field")
        if quote and len(quote) > MAX_QUOTE:
            raise HallError("invalid", f"quote at most {MAX_QUOTE} characters; cite the passage by its anchor")
        deps = self._nums(depends_on)
        for n in deps:                                  # checked before filing, so a bad link files nothing
            self._objection(seat, n)
        # Everyone outside this table knows an objection by its number in this debate (#1, #2, ...).
        with self.db.lock:
            num = self.db.one("SELECT COALESCE(MAX(num), 0) + 1 AS n FROM objections WHERE debate_id = ?", (did,))["n"]
            self.db.run("INSERT INTO objections (debate_id, num, seat_id, anchor, quote, claim, why, paper_sha, "
                        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (did, num, seat["id"], anchor, quote, claim, why, self.debate(did)["paper_sha"], self.now()))
        for n in deps:
            self.db.run("INSERT OR IGNORE INTO objection_links (debate_id, from_num, to_num, seat_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?)", (did, num, n, seat["id"], self.now()))
        dep_note = f" (depends on {', '.join('#' + str(n) for n in deps)})" if deps else ""
        self._log(did, "hall", seat["id"], "objection", f"Objection #{num} at {anchor}{dep_note}: {claim}",
                  {"objection": num, "anchor": anchor, "depends_on": deps})
        for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role IN ('advocate', 'reader_a') AND "
                           "revoked_at IS NULL", (did,)):
            self._attend(did, s["id"], "objection", num)       # the advocate's reader too (debrief, 10-30)
        self._huddle_prompt(did, "a", f"Objection #{num} needs your side's answer: talk it over here before anyone "
                                      "answers.")
        return {"id": num}

    @staticmethod
    def _nums(raw) -> list[int]:
        if raw in (None, "", []):
            return []
        if not isinstance(raw, list) or len(raw) > 20:
            raise HallError("invalid", "depends_on is a list of up to 20 objection numbers")
        try:
            return sorted({int(n) for n in raw})
        except (TypeError, ValueError):
            raise HallError("invalid", "depends_on is a list of objection numbers") from None

    LINKERS = ("advocate", "adversary", "reader_a", "reader_b", "witness", "clerk", "judge")

    def link(self, seat, from_num: int, to_num: int, remove: bool = False) -> dict:
        """'#from depends on #to' (or take that back). The debaters, their readers, the witness, the clerk and the
        judge may link; everyone sees it in the Hall and the ledger (the keeper, 10-25)."""
        if seat["role"] not in self.LINKERS:
            raise HallError("forbidden", "links are made by the debaters, their readers, the witness, the clerk or the judge")
        did = seat["debate_id"]
        if self.debate(did)["status"] == "closed":
            raise HallError("forbidden", "the debate is closed")
        if from_num == to_num:
            raise HallError("invalid", "an objection can't depend on itself")
        self._objection(seat, from_num)
        self._objection(seat, to_num)
        if remove:
            self.db.run("DELETE FROM objection_links WHERE debate_id = ? AND from_num = ? AND to_num = ?",
                        (did, from_num, to_num))
            line = f"{seat['name']} unlinks: #{from_num} no longer depends on #{to_num}"
        else:
            self.db.run("INSERT OR IGNORE INTO objection_links (debate_id, from_num, to_num, seat_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?)", (did, from_num, to_num, seat["id"], self.now()))
            line = f"{seat['name']} links: #{from_num} depends on #{to_num}"
        self._log(did, "hall", seat["id"], "notice", line, {"link": [from_num, to_num], "removed": remove})
        return {"from": from_num, "to": to_num, "linked": not remove}

    def _links(self, did: int) -> tuple[dict, dict]:
        depends, needed = {}, {}
        for r in self.db.q("SELECT from_num, to_num FROM objection_links WHERE debate_id = ? ORDER BY to_num", (did,)):
            depends.setdefault(r["from_num"], []).append(r["to_num"])
            needed.setdefault(r["to_num"], []).append(r["from_num"])
        return depends, needed

    def _objection(self, seat, num: int):
        o = self.db.one("SELECT * FROM objections WHERE num = ? AND debate_id = ?", (num, seat["debate_id"]))
        if o is None:
            raise HallError("not_found", "no such objection in this debate")
        return o

    def _event(self, seat, o, kind: str, text: str, verdict: str | None, status: str | None,
               proposed: str | None = None, evidence: list | None = None) -> dict:
        text = (text or "").strip()
        if not text or len(text) > MAX_TEXT:
            raise HallError("invalid", f"say why (up to {MAX_TEXT} characters)")
        if self.debate(seat["debate_id"])["status"] == "closed":
            raise HallError("forbidden", "the debate is closed")
        self.db.run("INSERT INTO objection_events (objection_id, seat_id, kind, verdict, text, proposed, evidence, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (o["id"], seat["id"], kind, verdict, text, proposed,
                     json.dumps(evidence) if evidence else None, self.now()))
        if status:
            self.db.run("UPDATE objections SET status = ? WHERE id = ?", (status, o["id"]))
        word = {"answer": "answers", "partial": "partly answers", "testimony": "gives testimony on", "reply": "replies on",
                "concede": "concedes", "draft": "drafts a ruling on", "ruling": "rules on"}[kind]
        label = f" ({verdict})" if verdict else ""
        line = f"{seat['name']} {word} objection #{o['num']}{label}: {text}"
        if proposed:
            line += f"\nProposed text: {proposed}"
        if evidence:
            line += "\nEvidence: " + "; ".join(f"{e['path']} @ {e['sha']}" for e in evidence)
        self._log(seat["debate_id"], "hall", seat["id"], kind, line, {"objection": o["num"], "verdict": verdict})
        return {"objection": o["num"], "status": status or o["status"]}

    @staticmethod
    def _evidence(raw) -> list[dict]:
        """Testimony cites by reference: a path and the sha of the file or commit it relies on, so the clerk can
        check it mechanically without trusting the side that gives it (an advocate, 10-25)."""
        if not isinstance(raw, list) or not raw or len(raw) > 20:
            raise HallError("invalid", "testimony cites 1 to 20 items of evidence: {path, sha, note}")
        out = []
        for e in raw:
            if not isinstance(e, dict):
                raise HallError("invalid", "each item of evidence is {path, sha, note}")
            path, sha, note = str(e.get("path", "")).strip(), str(e.get("sha", "")).strip(), str(e.get("note", "")).strip()
            if not path or len(path) > 300 or not SHA.match(sha) or len(note) > MAX_QUOTE:
                raise HallError("invalid", f"evidence needs a path (up to 300 characters), a sha (7-64 hex) and at "
                                f"most a {MAX_QUOTE}-character note")
            out.append({"path": path, "sha": sha.lower(), "note": note})
        return out

    def answer(self, seat, oid: int, text: str, mode: str = "answer", proposed: str | None = None,
               evidence: list | None = None) -> dict:
        """mode: answer; partial (a proposed rewording is required); testimony (how something was measured:
        evidence by path + sha is required, kept apart from argument); concede (the advocate; a proposed
        rewording is welcome, so the revision list arrives ready to apply)."""
        if mode not in ("answer", "partial", "testimony", "concede"):
            raise HallError("invalid", "mode is answer, partial, testimony or concede")
        if TEAM.get(seat["role"]) != "a" and seat["role"] != "witness":
            raise HallError("forbidden", "the advocate's side (or the witness) answers objections")
        if mode == "concede" and seat["role"] != "advocate":
            raise HallError("forbidden", "only the advocate concedes")
        proposed = (proposed or "").strip() or None
        if proposed and len(proposed) > MAX_TEXT:
            raise HallError("invalid", f"a proposed rewording is at most {MAX_TEXT} characters")
        if mode == "partial" and not proposed:
            raise HallError("invalid", "a partial answer comes with the proposed rewording")
        # Evidence may come with any answer (an advocate: a concession can carry its evidence); testimony requires it.
        ev = self._evidence(evidence) if (mode == "testimony" or evidence) else None
        o = self._objection(seat, oid)
        if o["status"] in VERDICTS:
            raise HallError("conflict", "the judge has already ruled on that one")
        # The status keeps the strongest outcome so far (an advocate, dry run 10-25: a later testimony had overwritten a
        # partial answer, which then dropped out of the export's "partial first").
        rank = {"open": 0, "answered": 1, "partial": 2, "conceded": 3}
        new = {"answer": "answered", "partial": "partial", "testimony": "answered", "concede": "conceded"}[mode]
        status = new if rank[new] >= rank.get(o["status"], 0) else o["status"]
        out = self._event(seat, o, mode, text, None, status, proposed, ev)
        did = seat["debate_id"]
        self._attend(did, o["seat_id"], "answered", oid)
        clerk_kind, ref = "draft", oid
        if proposed:
            # the keeper, 10-25: "I should know when there's a revision ready; their huddle is their huddle." So the
            # notice says a revision is ready, never where the idea came from.
            ref = self._log(did, "hall", None, "notice",
                            f"A revision is ready on #{o['num']}: proposed text in the ledger, ready for a ruling",
                            {"revision_ready": o["num"]})
            clerk_kind = "revision"
        # Every answer wakes the clerk to draft (the clerk, a test debate: the clerk seat had no wake at all and slept
        # through the whole debate; not one draft in the ledger).
        for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role = 'clerk'", (did,)):
            self._attend(did, s["id"], clerk_kind, ref)
        return out

    def reply(self, seat, oid: int, text: str) -> dict:
        """The objecting side's short reply to an answer, kept with the objection in the ledger instead of waiting
        for the floor (security review, a test debate: the advocate revised twice after the filing, and the only way to respond was
        to wait for rebuttals). One reply per new answer, so it can't become a running exchange."""
        if TEAM.get(seat["role"]) != "b":
            raise HallError("forbidden", "the objecting side (the adversary or its reader) replies to an answer")
        o = self._objection(seat, oid)
        if o["status"] in VERDICTS:
            raise HallError("conflict", "the judge has already ruled on that one")
        last = self.db.one("SELECT kind FROM objection_events WHERE objection_id = ? AND kind != 'draft' "
                           "ORDER BY id DESC LIMIT 1", (o["id"],))
        if last is None or last["kind"] == "reply":
            raise HallError("conflict", "a reply answers a new answer: wait for the other side to answer first")
        out = self._event(seat, o, "reply", text, None, None)
        did = seat["debate_id"]
        for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role = 'advocate'", (did,)):
            self._attend(did, s["id"], "reply", oid)
        # A reply after the clerk's draft makes that draft stale: the clerk hears it (debrief, 10-30).
        if self.db.one("SELECT 1 FROM objection_events WHERE objection_id = ? AND kind = 'draft'", (o["id"],)):
            for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role = 'clerk'", (did,)):
                self._attend(did, s["id"], "draft", oid)
        return out

    def lookup(self, seat, question: str) -> dict:
        """A team asks the moderator (the web lane) to look something up. It's logged in the Gallery for all to
        see, so the moderator stays visibly even-handed (an advocate, 10-25)."""
        if seat["role"] in ("judge", "moderator", "aide", "audience"):
            raise HallError("forbidden", "lookups are asked by the debaters, their readers, the witness or the clerk")
        question = (question or "").strip()
        if not question or len(question) > 1000:
            raise HallError("invalid", "ask a question (up to 1000 characters)")
        team = {"a": "Team A", "b": "Team B"}.get(TEAM.get(seat["role"], ""), seat["role"])
        did = seat["debate_id"]
        mid = self._log(did, "gallery", seat["id"], "lookup", f"Lookup for {team} ({seat['name']}): {question}",
                        {"lookup": True})
        for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role = 'moderator'", (did,)):
            self._attend(did, s["id"], "lookup", mid)
        return {"id": mid}

    def who_may_speak(self, did: int) -> str:
        holder = self.floor_holder(did)
        name = self.db.one("SELECT name FROM seats WHERE id = ?", (holder,))["name"] if holder else None
        chair = [s["name"] for s in self.db.q("SELECT name FROM seats WHERE debate_id = ? AND role IN "
                                              "('judge', 'moderator') ORDER BY id", (did,))]
        if self.debate(did)["status"] == "closed":
            return "Nobody: the debate is closed."
        h = self._running_huddle(did)
        if h is not None:
            st = self.huddle_state(did)
            return (f"Huddle time: the Hall waits while the teams confer (Team A {st['team_a']}, Team B {st['team_b']}; "
                    f"until {time.strftime('%H:%M', time.localtime(h['ends_at']))} at the latest).")
        return (f"{name} holds the floor; " if name else "Nobody holds the floor; ") + \
            f"the chair ({', '.join(chair) or 'none'}) may always speak in the Hall."

    def status(self, seat) -> dict:
        """Everything a seat needs to pick up where it was, in one call, from the ledger rather than from memory
        (an advocate, 10-25: a seat may be squished mid-debate; phase boundaries are natural squish points)."""
        did = seat["debate_id"]
        led = self.ledger(did)
        role = seat["role"]
        mine = [o for o in led if o["who"] == seat["name"]]
        to_answer = [o for o in led if o["status"] == "open"] if (TEAM.get(role) == "a" or role == "witness") else []
        to_draft = [o for o in led if o["status"] not in VERDICTS and not any(e["kind"] == "draft" for e in o["events"])] \
            if role == "clerk" else []
        to_rule = [o for o in led if o["status"] not in VERDICTS] if role == "judge" else []
        brief = lambda o: {"id": o["id"], "anchor": o["anchor"], "status": o["status"], "claim": o["claim"],  # noqa: E731
                           "revision_ready": o["revision_ready"], "depends_on": o["depends_on"]}
        d = self.debate(did)
        paper = ({"path": d["paper_path"], "file": (self.docs_dir / d["paper_path"]).as_posix(), "sha": d["paper_sha"]}
                 if d["paper_path"] else None)                 # An advocate: where the paper is, for a reader's own copy
        return {"seat": {"name": seat["name"], "role": role}, "debate": d["title"], "paper": paper,
                "floor": self.floor_state(did),
                "who_may_speak": self.who_may_speak(did), "mine": [brief(o) for o in mine],
                "to_answer": [brief(o) for o in to_answer], "to_draft": [brief(o) for o in to_draft],
                "to_rule": [brief(o) for o in to_rule],
                "counts": {s: sum(1 for o in led if o["status"] == s) for s in
                           ("open", "answered", "partial", "conceded", "sustained", "overruled", "revise")},
                "huddle": self.huddle_summary(did, TEAM[role]) if role in TEAM else None,
                "huddle_step": self.huddle_state(did),
                "listening": self.listening(did) if role in CHAIR else None,
                # an agent can see its own wake mod is listening (debrief, 10-30)
                "you_checked_ago": (None if seat["last_check"] is None else round(self.now() - seat["last_check"]))
                if seat["kind"] == "agent" else None,
                "your_wakes": None if seat["kind"] != "agent" else ("off" if seat["wake_off"] is not None else "on")}

    def draft(self, seat, oid: int, verdict: str, text: str) -> dict:
        if seat["role"] != "clerk":
            raise HallError("forbidden", "the clerk drafts rulings")
        if verdict not in VERDICTS:
            raise HallError("invalid", f"a draft verdict is one of: {', '.join(VERDICTS)}")
        return self._event(seat, self._objection(seat, oid), "draft", text, verdict, None)

    def rule(self, seat, oid: int, verdict: str, text: str) -> dict:
        if seat["role"] != "judge":
            raise HallError("forbidden", "only the judge rules")
        if verdict not in VERDICTS:
            raise HallError("invalid", f"a ruling is one of: {', '.join(VERDICTS)}")
        o = self._objection(seat, oid)
        # A reason is optional (the keeper, 10-25). Left blank, the ruling adopts the clerk's latest draft when there is
        # one (so the record still says why), or says no reason was given.
        if not (text or "").strip():
            draft = self.db.one("SELECT e.id, e.verdict FROM objection_events e WHERE e.objection_id = ? AND "
                                "e.kind = 'draft' ORDER BY e.id DESC LIMIT 1", (o["id"],))
            text = (f"Adopts the clerk's draft (it proposed {draft['verdict']})." if draft else "(no reason given)")
        # A "revise" ruling carries the agreed wording (the latest proposed text), so the export IS the revision
        # list without a second pass (security review, a test debate).
        agreed = None
        if verdict == "revise":
            p = self.db.one("SELECT proposed FROM objection_events WHERE objection_id = ? AND proposed IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1", (o["id"],))
            agreed = p["proposed"] if p else None
        out = self._event(seat, o, "ruling", text, verdict, verdict, agreed)
        did = seat["debate_id"]
        advocates = [s["id"] for s in self.db.q("SELECT id FROM seats WHERE debate_id = ? AND role = 'advocate'",
                                                 (did,))]
        for sid in {o["seat_id"], *advocates}:
            self._attend(did, sid, "ruling", oid)
        # What leaned on this one may need a second look (the aide, a test debate: #6's wording relied on #5's
        # evidence, which had just been sent back for revision).
        needed = self._links(did)[1].get(o["num"], [])
        if needed:
            self._log(did, "hall", None, "notice",
                      f"#{o['num']} was just ruled ({verdict}); {', '.join('#' + str(n) for n in needed)} "
                      f"depend{'s' if len(needed) == 1 else ''} on it and may need a second look",
                      {"ruled_with_dependents": o["num"], "dependents": needed})
        return out

    @staticmethod
    def _stale(events: list[dict]) -> dict | None:
        last = max((i for i, e in enumerate(events) if e["kind"] == "draft"), default=None)
        if last is None:
            return None
        later = [e for e in events[last + 1:] if e["kind"] not in ("draft", "ruling")]
        return {"since": later[-1]["who"], "kind": later[-1]["kind"]} if later else None

    def ledger(self, did: int) -> list[dict]:
        out = []
        depends, needed = self._links(did)
        for o in self.db.q("SELECT o.*, s.name AS who FROM objections o JOIN seats s ON s.id = o.seat_id "
                           "WHERE o.debate_id = ? ORDER BY o.id", (did,)):
            events = [{"who": e["who"], "kind": e["kind"], "verdict": e["verdict"], "text": e["text"],
                       "proposed": e["proposed"], "evidence": json.loads(e["evidence"]) if e["evidence"] else None,
                       "at": e["created_at"]}
                      for e in self.db.q("SELECT e.*, s.name AS who FROM objection_events e JOIN seats s "
                                         "ON s.id = e.seat_id WHERE e.objection_id = ? ORDER BY e.id", (o["id"],))]
            out.append({"id": o["num"], "who": o["who"], "anchor": o["anchor"], "quote": o["quote"],
                        "claim": o["claim"], "why": o["why"], "status": o["status"], "paper_sha": o["paper_sha"],
                        "at": o["created_at"], "events": events,
                        "depends_on": depends.get(o["num"], []), "needed_by": needed.get(o["num"], []),
                        # Proposed text waiting for a ruling (the keeper: the judge should know a revision is ready).
                        "revision_ready": o["status"] not in VERDICTS and any(e["proposed"] for e in events),
                        # A clerk's draft written before a later answer or reply is stale (debrief, 10-30).
                        "draft_stale": self._stale(events) if o["status"] not in VERDICTS else None})
        return out
