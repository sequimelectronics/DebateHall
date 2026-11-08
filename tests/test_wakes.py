# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
"""The release blocker from the first real debate (the keeper's debrief, 10-30: "would be considered a broken app"):
the readers were never woken, so both huddles and the Gallery stayed empty. Every debating seat must be woken at
least once in each phase it has a part in; a side's reader hears when that side has the floor or an objection; the
chair can call a huddle and see each team finish; status shows each team its huddle and the chair who's listening."""

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
from hall.core import PHASES, Hall, HallError  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


SEATS = (("Marlin", "judge", "person"), ("Kelp", "moderator", "agent"), ("Shell", "clerk", "agent"),
         ("Pebble", "advocate", "agent"), ("ReaderA", "reader_a", "agent"), ("Drift", "adversary", "agent"),
         ("ReaderB", "reader_b", "agent"), ("Tide", "witness", "agent"))


class WakeCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="hall-wakes-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "docs").mkdir()
        (self.dir / "docs" / "paper.md").write_text("# A paper\n\n## Claim\n\nThe doohicky works.\n", encoding="utf-8")
        self.clock = Clock()
        self.hall = Hall(self.dir / "hall.db", self.dir / "docs", now=self.clock)
        self.addCleanup(self.hall.db.close)
        self.did = self.hall.create_debate("Does the doohicky work?")
        self.hall.point_at_paper(self.did, "paper.md")
        self.tok, self.s = {}, {}
        for name, role, kind in SEATS:
            _, self.tok[name] = self.hall.add_seat(self.did, name, role, kind)
            self.s[name] = self.hall.authenticate(self.tok[name])

    def kinds(self, name, since=0):
        return [a["kind"] for a in self.hall.attention(self.s[name], since)]

    def last_id(self):
        r = self.hall.db.one("SELECT MAX(id) AS m FROM attention")
        return r["m"] or 0


class EveryPhaseWakesTests(WakeCase):
    def test_every_debating_seat_is_woken_in_every_phase(self):
        for phase in PHASES[1:]:
            mark = self.last_id()
            self.hall.set_phase(self.s["Kelp"], phase)
            for name in ("Shell", "Pebble", "ReaderA", "Drift", "ReaderB", "Tide"):
                self.assertIn("phase", self.kinds(name, mark), f"{name} slept through {phase}")

    def test_both_halves_of_a_team_are_prompted_to_confer_in_their_huddle(self):
        # The keeper, 11-07: in the first real debate the huddles were never spoken to, because the main debaters forgot
        # their readers. At the floor, an objection and each phase, the hall prompts BOTH halves, inside the huddle.
        def prompted(names, mark):
            return [n for n in names if "huddle_prompt" in self.kinds(n, mark)]
        mark = self.last_id()
        self.hall.give_floor(self.s["Kelp"], "Drift")
        self.assertEqual(prompted(("Drift", "ReaderB", "Pebble", "ReaderA"), mark), ["Drift", "ReaderB"])
        notes = [m["text"] for m in self.hall.db.q("SELECT text FROM messages WHERE room = 'team_b' AND kind = 'notice'")]
        self.assertIn("Drift: read this huddle before you speak.", notes[-1])        # it sits in the huddle
        self.hall.set_phase(self.s["Kelp"], "objections")
        self.hall.give_floor(self.s["Kelp"], "Drift")
        mark = self.last_id()
        self.hall.object(self.s["Drift"], "§ Claim", "It overreaches", "one trial")
        self.assertEqual(prompted(("Pebble", "ReaderA", "Drift", "ReaderB"), mark), ["Pebble", "ReaderA"])
        mark = self.last_id()
        self.hall.set_phase(self.s["Kelp"], "rebuttals")
        self.assertEqual(prompted(("Pebble", "ReaderA", "Drift", "ReaderB"), mark), ["Pebble", "ReaderA", "Drift", "ReaderB"])

    def test_no_team_member_is_left_out_of_their_huddle(self):
        # The keeper, 11-07: "make sure the huddle members can't get left out". Every teammate's line wakes every
        # other teammate, both teams, both directions; the other team never hears it.
        for team, members, others in (("team_a", ("Pebble", "ReaderA"), ("Drift", "ReaderB")),
                                      ("team_b", ("Drift", "ReaderB"), ("Pebble", "ReaderA"))):
            for speaker in members:
                mark = self.last_id()
                self.hall.post(self.s[speaker], team, "A point for our side.")
                for m in members:
                    if m != speaker:
                        self.assertIn("huddle", self.kinds(m, mark), f"{m} missed {speaker}'s huddle line")
                for o in others:
                    self.assertNotIn("huddle", self.kinds(o, mark))

    def test_between_debates_the_sign_in_answers_from_the_latest_closed_seat(self):
        # A resting wake mod must see "closed", never an error, or it would miss the next debate (11-07).
        pid = self.s["Pebble"]["participant_id"]
        tok = self.hall.new_sign_in(pid, "Pebble's own device")
        other = self.hall.create_debate("Is the whatsit round?")
        self.hall.add_seat(other, "", "advocate", participant=pid)
        self.hall.db.run("UPDATE debates SET status = 'closed'")
        s = self.hall.authenticate(tok)
        self.assertEqual(s["debate_id"], other)                            # the most recent one answers
        third = self.hall.create_debate("Does the thingamajig fit?")
        self.hall.add_seat(third, "", "witness", participant=pid)        # seated again: the bell
        self.assertEqual(self.hall.authenticate(tok)["debate_id"], third)


class HuddleStepTests(WakeCase):
    def test_the_chair_grants_huddle_time_and_the_hall_waits_until_both_teams_are_done(self):
        self.hall.give_floor(self.s["Kelp"], "Pebble")
        mark = self.last_id()
        h = self.hall.call_huddle(self.s["Kelp"], 10)
        self.assertTrue(h["running"])
        self.assertEqual((h["team_a"], h["team_b"]), ("still talking", "still talking"))
        self.assertIsNone(self.hall.floor_state(self.did)["holder"])          # the floor is cleared
        for name in ("Pebble", "ReaderA", "Drift", "ReaderB"):
            self.assertIn("huddle_call", self.kinds(name, mark))
        self.assertNotIn("huddle_call", self.kinds("Tide", mark))          # the witness isn't on a team
        with self.assertRaises(HallError):
            self.hall.give_floor(self.s["Kelp"], "Drift")                    # the Hall waits
        self.assertTrue(self.hall.who_may_speak(self.did).startswith("Huddle time: the Hall waits"))
        with self.assertRaises(HallError):
            self.hall.huddle_done(self.s["Tide"])
        with self.assertRaises(HallError):
            self.hall.call_huddle(self.s["Pebble"])                         # only the chair
        mark = self.last_id()
        h = self.hall.huddle_done(self.s["ReaderB"])
        self.assertEqual((h["team_a"], h["team_b"], h["running"]), ("still talking", "done", True))
        self.assertIn("huddle_done", self.kinds("Kelp", mark))             # the moderator hears it
        h = self.hall.huddle_done(self.s["Pebble"])
        self.assertFalse(h["running"])                                       # both done: the Hall is open again
        self.hall.give_floor(self.s["Kelp"], "Drift")

    def test_huddle_time_runs_out_or_the_chair_ends_it(self):
        self.hall.call_huddle(self.s["Kelp"], 5)
        self.clock.t += 5 * 60
        mark = self.last_id()
        self.assertFalse(self.hall.huddle_state(self.did)["running"])        # time is up on the next look
        self.assertIn("huddle_done", self.kinds("Kelp", mark))
        self.hall.call_huddle(self.s["Kelp"], 5)
        self.assertFalse(self.hall.end_huddle(self.s["Kelp"])["running"])
        with self.assertRaises(HallError):
            self.hall.call_huddle(self.s["Kelp"], 0)                         # 1 to 60 minutes

    def test_status_shows_each_team_its_huddle_and_the_chair_whos_listening(self):
        self.assertEqual(self.hall.status(self.s["ReaderA"])["huddle"], "Huddle (Team A): no posts yet")
        self.hall.post(self.s["ReaderA"], "team_a", "My ranked list: the claim in section 2 is weakest.")
        self.assertIn("1 posts, the latest by ReaderA", self.hall.status(self.s["Pebble"])["huddle"])
        self.assertIsNone(self.hall.status(self.s["Tide"])["huddle"])
        web = TestClient(create_app(self.hall, 8810), base_url="http://127.0.0.1:8810")
        web.get("/attention", headers={"Authorization": f"Bearer {self.tok['ReaderB']}"})
        ls = {x["name"]: x for x in self.hall.status(self.s["Kelp"])["listening"]}
        self.assertTrue(ls["ReaderB"]["listening"])
        self.assertFalse(ls["ReaderA"]["listening"])                        # never checked: not heard from
        self.assertIsNone(self.hall.status(self.s["Pebble"])["listening"])  # the chair's view only



class OwnWakesAndDraftsTests(WakeCase):
    def test_an_agent_switches_its_own_wakes_off_and_what_is_for_it_waits(self):
        self.hall.set_wake(self.s["Tide"], False)
        tide = self.hall.authenticate(self.tok["Tide"])
        self.hall.set_phase(self.s["Kelp"], "openings")
        self.assertEqual(self.hall.attention(tide), [])                     # quiet while off
        ls = {x["name"]: x for x in self.hall.status(self.s["Kelp"])["listening"]}
        self.assertIsNotNone(ls["Tide"]["wakes_off_since"])                # the chair sees it
        self.assertEqual(self.hall.status(tide)["your_wakes"], "off")
        self.hall.set_wake(tide, True)
        tide = self.hall.authenticate(self.tok["Tide"])
        self.assertIn("phase", [a["kind"] for a in self.hall.attention(tide)])   # nothing was lost
        with self.assertRaises(HallError):
            self.hall.set_wake(self.s["Marlin"], False)                       # a person watches the page

    def test_the_judge_asks_the_clerk_to_draft_and_a_later_reply_marks_the_draft_stale(self):
        self.hall.set_phase(self.s["Kelp"], "objections")
        self.hall.give_floor(self.s["Kelp"], "Drift")
        n = self.hall.object(self.s["Drift"], "§ Claim", "The doohicky claim overreaches", "one test only")["id"]
        self.hall.answer(self.s["Pebble"], n, "It was tested twice.")
        mark = self.last_id()
        self.assertEqual(self.hall.draft_now(self.s["Marlin"], n)["asked"], [n])
        self.assertIn("draft", self.kinds("Shell", mark))
        with self.assertRaises(HallError):
            self.hall.draft_now(self.s["Kelp"])                                # only the judge
        self.hall.draft(self.s["Shell"], n, "overruled", "Two tests are in the appendix.")
        self.assertIsNone([o for o in self.hall.ledger(self.did) if o["id"] == n][0]["draft_stale"])
        mark = self.last_id()
        self.hall.reply(self.s["Drift"], n, "The appendix tests a different doohicky.")
        stale = [o for o in self.hall.ledger(self.did) if o["id"] == n][0]["draft_stale"]
        self.assertEqual(stale, {"since": "Drift", "kind": "reply"})
        self.assertIn("draft", self.kinds("Shell", mark))                   # the clerk hears it

    def test_an_agent_sees_its_own_wake_mod_check_in(self):
        self.assertIsNone(self.hall.status(self.s["Tide"])["you_checked_ago"])
        web = TestClient(create_app(self.hall, 8810), base_url="http://127.0.0.1:8810")
        web.get("/attention", headers={"Authorization": f"Bearer {self.tok['Tide']}"})
        self.assertEqual(self.hall.status(self.hall.authenticate(self.tok["Tide"]))["you_checked_ago"], 0)
        self.assertIsNone(self.hall.status(self.s["Marlin"])["you_checked_ago"])   # a person: no wake mod


if __name__ == "__main__":
    unittest.main()
