# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
"""Access like AI-Beach's (11-07): a participant is one person or agent across debates, each device signs in on its
own, and a seat links a participant to a role in one debate. a security review's conditions, each tested here: the role comes
from the server's seat row; a closed debate takes no writes; a visitor's sign-in reaches one debate and ends with
it; old seat tokens keep reaching exactly their old seat after the migration, and nothing merges by name; a role
that's switched off is refused; a revoked sign-in stops at once; the audience door stays audience-only."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from hall.app import create_app  # noqa: E402
from hall.core import Hall, HallError, token_hash  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class AccessCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="hall-access-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "docs").mkdir()
        (self.dir / "docs" / "paper.md").write_text("# A paper\n\nThe doohicky works.\n", encoding="utf-8")
        self.clock = Clock()
        self.hall = self.open()
        self.a = self.hall.create_debate("Does the doohicky work?")
        self.b = self.hall.create_debate("Is the whatsit round?")

    def open(self) -> Hall:
        h = Hall(self.dir / "hall.db", self.dir / "docs", now=self.clock)
        self.addCleanup(h.db.close)
        return h

    def web(self, door=None):
        if door:
            return TestClient(create_app(self.hall, 8812, door=door), base_url=f"http://{door['host']}")
        return TestClient(create_app(self.hall, 8810), base_url="http://127.0.0.1:8810")

    @staticmethod
    def bearer(tok):
        return {"Authorization": f"Bearer {tok}"}


class OneIdentityManyDebatesTests(AccessCase):
    def test_a_device_sign_in_reaches_each_seat_and_the_role_comes_from_the_server(self):
        self.hall.add_seat(self.a, "Sandy", "advocate", "agent")
        pid = self.hall.participant_named("Sandy", "agent")["id"]
        self.hall.add_seat(self.b, "Sandy", "adversary", "agent", participant=pid)   # the keeper picks them
        self.assertEqual(self.hall.db.one("SELECT COUNT(*) n FROM participants")["n"], 1)
        tok = self.hall.new_sign_in(pid, "Sandy's laptop")                 # a device, not scoped to one debate
        with self.assertRaises(HallError):
            self.hall.authenticate(tok)                                     # two open seats: say which
        self.assertEqual(self.hall.authenticate(tok, self.a)["role"], "advocate")
        self.assertEqual(self.hall.authenticate(tok, self.b)["role"], "adversary")
        web = self.web()
        self.assertEqual(web.get("/me", headers={**self.bearer(tok), "X-Debate": str(self.b)}).json()["seat"]["role"],
                         "adversary")
        self.assertEqual(web.get(f"/me?debate={self.a}", headers=self.bearer(tok)).json()["seat"]["role"], "advocate")
        self.assertEqual(web.get("/me", headers=self.bearer(tok)).status_code, 400)

    def test_a_seat_token_reaches_its_own_debate_only(self):
        _, tok = self.hall.add_seat(self.a, "Pebble", "witness", "agent")
        self.hall.add_seat(self.b, "", "witness", participant=self.hall.participant_named("Pebble", "agent")["id"])
        self.assertEqual(self.hall.authenticate(tok)["debate_id"], self.a)
        self.assertIsNone(self.hall.authenticate(tok, self.b))

    def test_two_people_with_one_name_stay_two_unless_the_keeper_picks_the_id(self):
        # a security review, 11-07: a name match must never hand a new seat to someone else's devices.
        _, first = self.hall.add_seat(self.a, "Sam", "advocate", "person")
        device = self.hall.new_sign_in(self.hall.participant_named("Sam", "person")["id"], "first Sam's phone")
        with self.assertRaises(HallError):
            self.hall.add_seat(self.b, "Sam", "adversary", "person")          # refused, not merged
        self.hall.add_seat(self.b, "Sam B", "adversary", "person")            # someone new: a new name
        self.assertEqual(self.hall.db.one("SELECT COUNT(*) n FROM participants")["n"], 2)
        self.assertIsNone(self.hall.authenticate(device, self.b))               # first Sam's devices: not that seat
        with self.assertRaises(HallError):
            self.hall.add_seat(self.b, "x", "witness", participant=9999)       # an id that isn't anyone

    def test_a_revoked_sign_in_stops_at_once(self):
        _, tok = self.hall.add_seat(self.a, "Coral", "audience", "person")
        self.assertIsNotNone(self.hall.authenticate(tok))
        self.hall.db.run("UPDATE sign_ins SET revoked_at = ? WHERE token_hash = ?", (self.clock(), token_hash(tok)))
        self.assertIsNone(self.hall.authenticate(tok))


class VisitorTests(AccessCase):
    def test_a_visitor_reaches_one_debate_and_the_sign_in_ends_with_it(self):
        pid = self.hall.add_participant("Drift", "person", access="visitor")
        self.hall.db.run("INSERT INTO seats (debate_id, name, role, kind, token_hash, created_at, participant_id) "
                         "VALUES (?, 'Drift', 'audience', 'person', 'x1', 0, ?)", (self.a, pid))
        self.hall.db.run("INSERT INTO seats (debate_id, name, role, kind, token_hash, created_at, participant_id) "
                         "VALUES (?, 'Drift', 'audience', 'person', 'x2', 0, ?)", (self.b, pid))
        code = self.hall.new_invite(pid, self.a)
        tok = self.web().post("/claim", json={"code": code}).json()["token"]
        self.assertEqual(self.hall.authenticate(tok)["debate_id"], self.a)
        self.assertIsNone(self.hall.authenticate(tok, self.b))              # refused on every other debate
        self.hall.db.run("UPDATE debates SET status = 'closed' WHERE id = ?", (self.a,))
        self.assertIsNone(self.hall.authenticate(tok))                      # ended with its debate


class ClosedDebateTests(AccessCase):
    def test_a_closed_debate_can_be_read_not_changed(self):
        self.hall.point_at_paper(self.a, "paper.md")
        _, judge = self.hall.add_seat(self.a, "Sandy", "judge", "person")
        web = self.web()
        self.hall.db.run("UPDATE debates SET status = 'closed' WHERE id = ?", (self.a,))
        self.assertEqual(web.get("/me", headers=self.bearer(judge)).status_code, 200)
        self.assertEqual(web.post("/say", json={"room": "hall", "text": "one more thing"},
                                  headers=self.bearer(judge)).status_code, 403)


class RolesOffTests(AccessCase):
    def test_a_switched_off_role_is_refused(self):
        self.hall.set_roles_off(self.a, ["clerk", "aide"])
        with self.assertRaises(HallError):
            self.hall.add_seat(self.a, "Kelp", "clerk", "agent")
        _, jt = self.hall.add_seat(self.a, "Sandy", "judge", "person")
        self.hall.add_seat(self.a, "Kelp", "witness", "agent")
        with self.assertRaises(HallError):
            self.hall.set_role(self.hall.authenticate(jt), "Kelp", "aide")
        with self.assertRaises(HallError):
            self.hall.set_roles_off(self.a, ["judge"])                      # the judge always sits
        with self.assertRaises(HallError):
            self.hall.set_roles_off(self.a, ["witness"])                    # someone holds it
        self.assertEqual(self.hall.set_roles_off(self.a, []), [])
        self.hall.set_roles_off(self.a, ["aide", "audience"])
        judge = self.hall.authenticate(jt)
        self.assertFalse(self.hall.can_read(judge, "aide"))                   # no aide: no aide room tab
        self.assertFalse(self.hall.can_read(judge, "gallery"))                # no audience: no Gallery


class MigrationTests(AccessCase):
    def test_old_seat_tokens_keep_their_seat_and_nothing_merges_by_name(self):
        # Seats as they were before sign-ins: the token on the seat row, no participant.
        db = self.hall.db
        db.run("DELETE FROM sign_ins")
        db.run("DELETE FROM participants")
        old = {}
        for did, name, role in ((self.a, "Shell", "advocate"), (self.b, "Shell", "adversary"),
                                (self.a, "Kelp", "audience")):
            tok = f"old-token-{did}-{name}"
            old[(did, name)] = (tok, role)
            db.run("INSERT INTO seats (debate_id, name, role, kind, token_hash, created_at) VALUES (?, ?, ?, 'agent', "
                   "?, 0)", (did, name, role, token_hash(tok)))
        db.run("UPDATE seats SET claim_hash = ?, claim_expires = ? WHERE name = 'Kelp'",
               (token_hash("ABCD-1234-ABCD-1234"), self.clock() + 3600))
        self.hall.db.close()
        self.hall = self.open()                                             # the migration runs on start
        for (did, _name), (tok, role) in old.items():
            s = self.hall.authenticate(tok)
            self.assertEqual((s["debate_id"], s["role"]), (did, role))
        names = sorted(r["name"] for r in self.hall.db.q("SELECT name FROM participants"))
        self.assertEqual(names, ["Kelp", "Shell", f"Shell (debate {self.b})"])   # two holders, never merged
        got = self.web().post("/claim", json={"code": "abcd-1234-abcd-1234"}).json()
        self.assertEqual((got["name"], got["role"]), ("Kelp", "audience"))   # a waiting code became an invite
        self.hall.db.close()
        self.hall = self.open()                                             # runs once: nothing doubles
        self.assertEqual(self.hall.db.one("SELECT COUNT(*) n FROM participants")["n"], 3)


class KeeperTests(AccessCase):
    def keeper(self):
        web = self.web()
        tok = web.post("/claim", json={"code": self.hall.keeper_invite("Warden")}).json()["token"]
        return web, self.bearer(tok)

    def test_only_the_keeper_reaches_the_keeper_routes(self):
        web, k = self.keeper()
        self.assertEqual(web.get("/admin/overview", headers=k).status_code, 200)
        _, seat = self.hall.add_seat(self.a, "Sandy", "judge", "person")
        self.assertEqual(web.get("/admin/overview", headers=self.bearer(seat)).status_code, 403)
        self.assertEqual(web.get("/admin/overview").status_code, 403)
        door = self.web({"host": "192.168.1.10:8812", "debate": self.a})
        self.assertEqual(door.get("/admin/overview", headers=k).status_code, 404)    # never through the door
        self.hall.db.run("UPDATE participants SET removed_at = 1 WHERE is_admin = 1")
        self.assertEqual(web.get("/admin/overview", headers=k).status_code, 403)    # a removed keeper is out

    def test_the_keeper_seats_someone_new_or_someone_here_and_gets_a_code_not_a_token(self):
        web, k = self.keeper()
        r = web.post(f"/admin/debates/{self.a}/seats", json={"name": "Kelp", "kind": "agent", "role": "witness"},
                     headers=k).json()
        self.assertNotIn("token", r)
        self.assertRegex(r["code"], r"^[0-9A-F]{4}(-[0-9A-F]{4}){3}$")
        self.assertTrue(r["link"].startswith("/#c="))
        got = web.post("/claim", json={"code": r["code"]}).json()
        self.assertEqual((got["name"], got["role"], got["debate"]), ("Kelp", "witness", self.a))
        pid = self.hall.participant_named("Kelp", "agent")["id"]
        r2 = web.post(f"/admin/debates/{self.b}/seats", json={"participant": pid, "role": "advocate"}, headers=k)
        self.assertEqual(r2.status_code, 200)
        clash = web.post(f"/admin/debates/{self.b}/seats", json={"name": "Kelp", "kind": "agent", "role": "witness"},
                         headers=k)
        self.assertEqual(clash.status_code, 409)                                  # a name in use: pick, never merged

    def test_a_visitor_is_seated_for_one_debate_and_their_invite_says_which(self):
        web, k = self.keeper()
        r = web.post(f"/admin/debates/{self.a}/seats", json={"name": "Drift", "kind": "person", "access": "visitor",
                                                          "role": "audience"}, headers=k).json()
        tok = web.post("/claim", json={"code": r["code"]}).json()["token"]
        self.assertIsNone(self.hall.authenticate(tok, self.b))
        pid = self.hall.participant_named("Drift", "person")["id"]
        self.assertEqual(web.post(f"/admin/people/{pid}/invite", json={}, headers=k).status_code, 400)

    def test_roles_off_and_device_sign_out_from_the_page(self):
        web, k = self.keeper()
        self.assertEqual(web.post(f"/admin/debates/{self.a}/roles-off", json={"roles": ["aide"]}, headers=k).json(),
                         {"roles_off": ["aide"]})
        ov = web.get("/admin/overview", headers=k).json()
        self.assertEqual([d["roles_off"] for d in ov["debates"] if d["id"] == self.a], [["aide"]])
        self.assertNotIn("token_hash", str(ov))
        _, tok = self.hall.add_seat(self.a, "Pebble", "witness", "agent")
        sid = self.hall.db.one("SELECT id FROM sign_ins WHERE token_hash = ?", (token_hash(tok),))["id"]
        web.post(f"/admin/sign-ins/{sid}/revoke", headers=k)
        self.assertIsNone(self.hall.authenticate(tok))


class DoorTests(AccessCase):
    def test_a_residents_full_sign_in_at_the_door_reaches_only_audience_routes_of_that_debate(self):
        self.hall.point_at_paper(self.a, "paper.md")
        self.hall.add_seat(self.a, "Coral", "audience", "person")
        pid = self.hall.participant_named("Coral", "person")["id"]
        self.hall.add_seat(self.b, "Coral", "advocate", "person", participant=pid)
        tok = self.hall.new_sign_in(pid, "Coral's phone")
        door = self.web({"host": "192.168.1.10:8812", "debate": self.a})
        self.assertEqual(door.get("/me", headers={**self.bearer(tok), "X-Debate": str(self.b)}).json()["seat"]["role"],
                         "audience")                                         # the door picks its own debate
        self.assertEqual(door.post("/floor/request", headers=self.bearer(tok)).status_code, 404)


if __name__ == "__main__":
    unittest.main()
