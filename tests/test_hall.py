# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
"""The hall's rules, enforced on the server, and its local-only protections (security review, 10-25)."""

from __future__ import annotations

import json
import importlib.util
import io
import contextlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from hall.app import create_app, render_paper  # noqa: E402
from hall.core import Hall, HallError  # noqa: E402

PAPER = """# A small paper

## 1 Introduction

We claim the sky is blue.

## 4.2 Results

| case | value |
|---|---|
| a | 1 |

<script>alert(1)</script> and [a link](javascript:alert(1)).
"""


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class HallCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="hall-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "docs").mkdir()
        (self.dir / "docs" / "paper.md").write_text(PAPER, encoding="utf-8")
        (self.dir / "outside.md").write_text("# not in docs\n", encoding="utf-8")
        self.clock = Clock()
        self.hall = Hall(self.dir / "hall.db", self.dir / "docs", now=self.clock)
        self.addCleanup(self.hall.db.close)
        self.did = self.hall.create_debate("Is the sky blue?")
        self.tok = {}
        for name, role, kind in (("Marlin", "judge", "person"), ("Shell", "clerk", "agent"),
                                 ("Kelp", "moderator", "agent"), ("Pebble", "advocate", "agent"),
                                 ("Drift", "adversary", "agent"), ("ReaderA", "reader_a", "agent"),
                                 ("ReaderB", "reader_b", "agent"), ("Tide", "witness", "agent"),
                                 ("Guest", "audience", "person")):
            _, self.tok[name] = self.hall.add_seat(self.did, name, role, kind)
        self.s = {n: self.hall.authenticate(t) for n, t in self.tok.items()}


class FloorTests(HallCase):
    def test_only_the_floor_holder_and_the_chair_speak_in_the_hall(self):
        with self.assertRaises(HallError) as e:
            self.hall.post(self.s["Drift"], "hall", "I object!")
        self.assertEqual(e.exception.code, "not_your_floor")
        self.hall.post(self.s["Kelp"], "hall", "Welcome.")
        self.hall.post(self.s["Marlin"], "hall", "Order.")
        self.hall.give_floor(self.s["Kelp"], "Drift")
        self.hall.post(self.s["Drift"], "hall", "Now I object.")
        with self.assertRaises(HallError):
            self.hall.post(self.s["Pebble"], "hall", "Not my turn.")
        self.hall.yield_floor(self.s["Drift"])
        with self.assertRaises(HallError):
            self.hall.post(self.s["Drift"], "hall", "Again?")

    def test_only_the_chair_moves_the_floor(self):
        for who in ("Pebble", "Drift", "Shell", "Tide", "Guest"):
            with self.assertRaises(HallError, msg=who):
                self.hall.give_floor(self.s[who], who)
        self.hall.give_floor(self.s["Marlin"], "Pebble")
        with self.assertRaises(HallError):
            self.hall.take_floor(self.s["Drift"])
        self.hall.take_floor(self.s["Kelp"])
        self.assertIsNone(self.hall.floor_state(self.did)["holder"])

    def test_a_person_judge_paces_the_debate(self):
        # the keeper, 10-25: with a human judge, the debate waits for her Continue after each turn ("otherwise you guys
        # could write a book in about 30 seconds"); she sees "waiting on your Continue".
        self.assertTrue(self.hall.paced(self.did))                      # a person judges: on by default
        self.hall.give_floor(self.s["Kelp"], "Pebble")
        st = self.hall.yield_floor(self.s["Pebble"])
        self.assertTrue(st["waiting"])
        self.assertEqual(self.hall.attention(self.s["Kelp"]), [])     # the moderator waits for the judge
        with self.assertRaises(HallError):
            self.hall.nudge_moderator(self.s["Pebble"])
        st = self.hall.nudge_moderator(self.s["Marlin"])
        self.assertFalse(st["waiting"])
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Kelp"])], ["nudge"])
        self.assertIn("Moderator, please continue", self.hall.feed(self.s["Drift"])[-1]["text"])
        self.hall.give_floor(self.s["Kelp"], "Drift")
        self.hall.yield_floor(self.s["Drift"])
        self.assertTrue(self.hall.floor_state(self.did)["waiting"])
        self.hall.give_floor(self.s["Kelp"], "Pebble")                    # the debate moving on clears it too
        self.assertFalse(self.hall.floor_state(self.did)["waiting"])

    def test_an_unpaced_debate_runs_at_full_speed(self):
        # "After we're done, if you guys want to have a debate, you shouldn't have to slow down for the human."
        with self.assertRaises(HallError):
            self.hall.set_paced(self.s["Kelp"], False)
        self.hall.set_paced(self.s["Marlin"], False)
        self.hall.give_floor(self.s["Kelp"], "Pebble")
        st = self.hall.yield_floor(self.s["Pebble"])
        self.assertFalse(st["waiting"])
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Kelp"])], ["yielded"])
        h = Hall(self.dir / "agents.db", self.dir / "docs", now=self.clock)       # an agent judge: off by default
        self.addCleanup(h.db.close)
        d = h.create_debate("agents only")
        h.add_seat(d, "Judgebot", "judge", "agent")
        self.assertFalse(h.paced(d))

    def test_a_timed_floor_runs_out(self):
        self.hall.give_floor(self.s["Kelp"], "Pebble", minutes=2)
        self.hall.post(self.s["Pebble"], "hall", "Quickly.")
        self.clock.t += 121
        with self.assertRaises(HallError):
            self.hall.post(self.s["Pebble"], "hall", "Too late.")

    def test_requests_queue_and_the_audience_uses_the_gallery(self):
        self.hall.request_floor(self.s["Drift"])
        self.hall.request_floor(self.s["Pebble"])
        self.assertEqual(self.hall.floor_state(self.did)["queue"], ["Drift", "Pebble"])
        self.hall.give_floor(self.s["Kelp"], "Drift")
        self.assertEqual(self.hall.floor_state(self.did)["queue"], ["Pebble"])
        with self.assertRaises(HallError):
            self.hall.request_floor(self.s["Guest"])
        self.hall.post(self.s["Guest"], "gallery", "Go Drift!")

    def test_being_given_the_floor_or_named_by_the_chair_asks_for_attention(self):
        self.hall.give_floor(self.s["Kelp"], "Pebble")
        self.hall.post(self.s["Kelp"], "hall", "@Tide, what did the scenario do?")
        self.hall.give_floor(self.s["Kelp"], "Drift")
        self.hall.post(self.s["Drift"], "hall", "@Pebble is wrong")                 # not the chair: wakes nobody
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Pebble"])], ["floor", "huddle_prompt"])   # + confer (11-07)
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Tide"])], ["named"])

    def test_a_full_width_at_sign_names_a_seat_too(self):
        # 10-30: an agent wrote the full-width form (U+FF20); it looks the same, so it must work the same.
        self.hall.post(self.s["Kelp"], "hall", chr(0xFF20) + "Tide, please check 3.2.")
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Tide"])], ["named"])


class HuddleTests(HallCase):
    def test_who_reads_what_during_the_debate(self):
        # the keeper, 10-25: huddles stay with their own team until the debate is over (what a team decided to say,
        # not its thrown-out brainstorms); the Gallery is the audience's, read by the moderator; the judge focuses
        # on the Hall ("if I see the Gallery it will influence me"); clerk and witness read the Hall.
        self.hall.post(self.s["Pebble"], "team_a", "Our weak spot is 4.2.")
        self.hall.post(self.s["ReaderB"], "team_b", "Attack 4.2.")
        self.hall.post(self.s["Guest"], "gallery", "Ask them about 3.1?")
        self.hall.post(self.s["Kelp"], "hall", "Opening.")
        texts = lambda who: [m["text"] for m in self.hall.feed(self.s[who])]     # noqa: E731
        sees = {who: set(texts(who)) for who in self.s}
        want = {"Marlin": {"Opening."}, "Shell": {"Opening."}, "Tide": {"Opening."},
                "Kelp": {"Opening.", "Ask them about 3.1?"}, "Guest": {"Opening.", "Ask them about 3.1?"},
                "Pebble": {"Opening.", "Our weak spot is 4.2."}, "ReaderA": {"Opening.", "Our weak spot is 4.2."},
                "Drift": {"Opening.", "Attack 4.2."}, "ReaderB": {"Opening.", "Attack 4.2."}}
        for who, lines in want.items():
            self.assertEqual(sees[who], lines, who)
        for who, room in (("Drift", "team_a"), ("Pebble", "gallery"), ("Marlin", "gallery"), ("Shell", "gallery"),
                          ("Marlin", "team_a")):
            with self.assertRaises(HallError, msg=f"{who} in {room}"):
                self.hall.post(self.s[who], room, "Sneaky.")
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Kelp"])], ["gallery"])
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["ReaderA"])], ["huddle"])   # an advocate's line
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Drift"])], ["huddle"])     # ReaderB's
        self.assertEqual(self.hall.attention(self.s["Pebble"]), [])                                      # not your own
        with self.assertRaises(HallError):
            self.hall.close_debate(self.s["Kelp"])
        self.hall.close_debate(self.s["Marlin"])
        for who in ("Drift", "Guest", "Marlin", "Shell"):
            self.assertIn("Our weak spot is 4.2.", texts(who), who)
            self.assertIn("Ask them about 3.1?", texts(who), who)
        with self.assertRaises(HallError):
            self.hall.post(self.s["Marlin"], "hall", "After close.")

    def test_the_judge_and_moderator_have_their_own_room(self):
        # the keeper, 10-25: "a 3rd tab next to aide" so the moderator can get her to do something; and an ask for the
        # floor reaches the moderator (nobody saw a security review's).
        self.hall.request_floor(self.s["Drift"])
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Kelp"])], ["request"])
        self.hall.request_floor(self.s["Drift"])                                   # asking twice wakes once
        self.assertEqual(len(self.hall.attention(self.s["Kelp"])), 1)
        with self.assertRaises(HallError):
            self.hall.ping_judge(self.s["Pebble"], "hi")
        self.hall.ping_judge(self.s["Kelp"], "Drift asks for the floor; may I give it?")
        self.hall.post(self.s["Marlin"], "chair", "Yes, go ahead.")
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Kelp"])], ["request", "chair"])
        for who in ("Pebble", "Drift", "Guest", "Tide"):
            seen = [m["text"] for m in self.hall.feed(self.s[who])]
            self.assertNotIn("Yes, go ahead.", seen, who)
            with self.assertRaises(HallError, msg=who):
                self.hall.post(self.s[who], "chair", "me too")
        self.assertIn("Drift asks for the floor; may I give it?", [m["text"] for m in self.hall.feed(self.s["Marlin"])])

    def test_the_clerk_asks_the_judge_and_moderator_in_the_chair_room(self):
        # the keeper, 10-30: the clerk had no way to ask for clarification; now the chair room is the clerk's too.
        self.hall.post(self.s["Shell"], "chair", "#4 was answered twice: which answer do I draft against?")
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Kelp"])], ["chair"])
        self.assertIn("#4 was answered twice: which answer do I draft against?",
                      [m["text"] for m in self.hall.feed(self.s["Marlin"])])
        self.hall.post(self.s["Kelp"], "chair", "@Shell the latest one; @Pebble please re-read #4.")
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Shell"])], ["chair"])
        self.assertEqual(self.hall.attention(self.s["Pebble"]), [])               # can't read the room: never woken to it
        self.assertNotIn("@Shell the latest one; @Pebble please re-read #4.", [m["text"] for m in self.hall.feed(self.s["Pebble"])])

    def test_the_moderator_brings_a_gallery_question_into_the_hall(self):
        q = self.hall.post(self.s["Guest"], "gallery", "Does 3.2 contradict 4?")
        with self.assertRaises(HallError):
            self.hall.relay(self.s["Marlin"], q["id"])
        hb_line = self.hall.post(self.s["Pebble"], "team_a", "private")
        with self.assertRaises(HallError):
            self.hall.relay(self.s["Kelp"], hb_line["id"])                 # only Gallery lines
        self.hall.relay(self.s["Kelp"], q["id"])
        self.assertEqual(self.hall.feed(self.s["Drift"])[-1]["text"], "From the Gallery, Guest asks: Does 3.2 contradict 4?")

    def test_paging_never_skips_lines_behind_private_ones(self):
        for i in range(30):
            self.hall.post(self.s["Pebble"], "team_a", f"private {i}")
        self.hall.post(self.s["Kelp"], "hall", "public")
        page = self.hall.feed(self.s["Drift"], 0, limit=5)
        self.assertEqual([m["text"] for m in page], ["public"])


class ReaderToolTests(HallCase):
    """What the cold readers asked for after the a test debate (10-25)."""

    def setUp(self):
        super().setUp()
        import importlib.util
        spec = importlib.util.spec_from_file_location("hall_tool", ROOT / "tools" / "hall.py")
        self.tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.tool)

    def out(self, fn, *a):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*a)
        return buf.getvalue()

    def test_the_helper_reaches_only_this_pc_or_the_one_named_door(self):
        ok = self.tool.loopback
        self.assertEqual(ok("http://127.0.0.1:8810"), "http://127.0.0.1:8810")
        door = "http://192.168.1.10:8812"
        self.assertEqual(ok(door, door), door)
        for bad, named in (("http://192.168.1.10:8812", None),            # a door nobody named
                           ("http://192.168.1.11:8812", door),            # not the named one
                           ("http://8.8.8.8:8812", "http://8.8.8.8:8812"),  # named, but not a home address
                           ("https://192.168.1.10:8812", door), ("http://192.168.1.10:8812/x", door)):
            with self.assertRaises(SystemExit, msg=bad):
                ok(bad, named)

    def test_a_read_folds_your_own_lines(self):
        self.hall.post(self.s["ReaderA"], "team_a", "A long note of mine, For-1 to For-5 ...")
        self.hall.post(self.s["Pebble"], "team_a", "Thanks, agreed on For-2.")
        items = self.hall.feed(self.s["ReaderA"])
        self.assertEqual([m["mine"] for m in items], [True, False])
        text = self.out(self.tool.show_items, items)
        self.assertIn("(your own line)", text)
        self.assertNotIn("A long note of mine", text)
        self.assertIn("Thanks, agreed on For-2.", text)

    def test_every_reading_says_how_much_there_is_and_where_it_ends(self):
        # the keeper, 10-25: a ledger piped through head -40 hid a ruling, and an agent may report as if it read it all.
        head, tail = self.tool.bookends("objections", 6, [1, 2, 3, 4, 5, 6])
        self.assertIn("6 objections, #1 to #6", head)
        self.assertEqual(tail, "(end of objections: 6 shown, nothing left out)")
        head, tail = self.tool.bookends("new lines", 200, list(range(1, 201)), more=True)
        self.assertIn("MORE are waiting", head)
        self.assertIn("MORE are waiting: run it again", tail)

    def test_an_objection_as_it_stands_now(self):
        o = self.hall.object(self.s["Drift"], "2.2", "belongs", "for whom?", "a topping belongs")
        self.hall.answer(self.s["Pebble"], o["id"], "first", "partial", proposed="Old wording.")
        self.hall.answer(self.s["Pebble"], o["id"], "second", "partial", proposed="Newer wording.")
        self.hall.rule(self.s["Marlin"], o["id"], "revise", "Say by whom.")
        led = self.hall.ledger(self.did)[0]
        now = self.out(self.tool.show_objection, led, True)
        self.assertIn("current proposed text: Newer wording. (replaces 1 earlier)", now)
        self.assertNotIn("Old wording.", now)
        self.assertIn("latest answer (Pebble): second", now)
        self.assertIn("ruling (revise): Say by whom.", now)
        self.assertIn("Old wording.", self.out(self.tool.show_objection, led, False))   # --history keeps it all


class LinkAndRevisionTests(HallCase):
    """Marlin, 10-25: "I should know when there's a revision ready, their huddle is their huddle. Objections should
    be able to link because complex topics may have complex objections." """

    def test_a_revision_ready_notice_names_no_source(self):
        o = self.hall.object(self.s["Drift"], "3.3", "sales", "unsourced")
        self.hall.post(self.s["ReaderA"], "team_a", "Suggest: cite the trade figures.")
        self.hall.answer(self.s["Pebble"], o["id"], "fair", "partial", proposed="Sales (Trade Report 2025) show ...")
        notice = [m for m in self.hall.feed(self.s["Marlin"]) if m["kind"] == "notice"][-1]["text"]
        self.assertEqual(notice, "A revision is ready on #1: proposed text in the ledger, ready for a ruling")
        self.assertNotIn("ReaderA", notice)
        self.assertTrue(self.hall.ledger(self.did)[0]["revision_ready"])
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Shell"])], ["revision"])
        self.hall.rule(self.s["Marlin"], o["id"], "revise", "")
        self.assertFalse(self.hall.ledger(self.did)[0]["revision_ready"])
        b = self.hall.object(self.s["Drift"], "4", "c", "w")
        self.hall.answer(self.s["Pebble"], b["id"], "no change needed")                 # a plain answer: no notice
        self.assertFalse(self.hall.ledger(self.did)[1]["revision_ready"])
        # ... but the clerk is still woken to draft (a test debate: the clerk slept through the whole debate).
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Shell"])], ["revision", "draft"])

    def test_objections_link_both_ways_and_a_ruling_flags_what_depends_on_it(self):
        five = self.hall.object(self.s["Drift"], "3.3", "sales", "unsourced")["id"]
        with self.assertRaises(HallError):
            self.hall.object(self.s["Drift"], "2.2", "c", "w", depends_on=[9])  # no #9: nothing filed
        self.assertEqual(len(self.hall.ledger(self.did)), 1)
        six = self.hall.object(self.s["Drift"], "2.2", "belongs", "relies on 3.3", depends_on=[five])["id"]
        led = {o["id"]: o for o in self.hall.ledger(self.did)}
        self.assertEqual((led[six]["depends_on"], led[five]["needed_by"]), ([five], [six]))
        self.assertIn(f"(depends on #{five})", [m["text"] for m in self.hall.feed(self.s["Marlin"])][-1])
        with self.assertRaises(HallError):
            self.hall.link(self.s["Guest"], six, five)                          # the audience doesn't link
        with self.assertRaises(HallError):
            self.hall.link(self.s["Shell"], six, six)
        seven = self.hall.object(self.s["ReaderB"], "4", "c", "w")["id"]
        self.hall.link(self.s["Shell"], seven, five)                            # the clerk may link later
        self.hall.rule(self.s["Marlin"], five, "revise", "Source the sales.")
        last = [m["text"] for m in self.hall.feed(self.s["Pebble"])][-1]
        self.assertEqual(last, f"#{five} was just ruled (revise); #{six}, #{seven} depend on it and may need a second look")
        self.hall.link(self.s["Shell"], seven, five, remove=True)
        self.assertEqual(self.hall.ledger(self.did)[0]["needed_by"], [six])
        from hall.app import ledger_md
        self.assertIn(f"**Linked:** depends on #{five}", ledger_md(self.hall, self.did))


class AdversaryNotesTests(HallCase):
    """a security review's notes from the adversary's seat, a test debate (10-25)."""

    def test_a_moot_ask_for_the_floor_can_be_withdrawn(self):
        with self.assertRaises(HallError):
            self.hall.withdraw_request(self.s["Drift"])
        self.hall.request_floor(self.s["Drift"])
        self.assertEqual(self.hall.withdraw_request(self.s["Drift"])["queue"], [])

    def test_the_objecting_side_replies_once_to_each_new_answer(self):
        o = self.hall.object(self.s["Drift"], "2.2", "belongs", "for whom?")["id"]
        with self.assertRaises(HallError):
            self.hall.reply(self.s["Drift"], o, "nothing to reply to yet")
        with self.assertRaises(HallError):
            self.hall.reply(self.s["Pebble"], o, "the advocate answers, it doesn't reply")
        self.hall.answer(self.s["Pebble"], o, "first", "partial", proposed="For diners who choose it.")
        self.hall.reply(self.s["ReaderB"], o, "'who choose it' brings back the self-selection of #2")
        with self.assertRaises(HallError):
            self.hall.reply(self.s["Drift"], o, "and another thing")                 # one per answer
        self.assertIn("reply", [a["kind"] for a in self.hall.attention(self.s["Pebble"])])
        self.hall.answer(self.s["Pebble"], o, "second", "partial", proposed="For a substantial number of diners.")
        self.hall.reply(self.s["Drift"], o, "better")                                # a new answer: a new reply
        self.assertEqual(self.hall.ledger(self.did)[0]["status"], "partial")         # replies don't change status

    def test_a_revise_ruling_carries_the_agreed_wording_into_the_export(self):
        from hall.app import ledger_md
        o = self.hall.object(self.s["Drift"], "2.2", "belongs", "for whom?")["id"]
        self.hall.answer(self.s["Pebble"], o, "first", "partial", proposed="Old wording.")
        self.hall.answer(self.s["Pebble"], o, "second", "partial", proposed="Agreed wording.")
        self.hall.rule(self.s["Marlin"], o, "revise", "Say by whom.")
        md = ledger_md(self.hall, self.did)
        self.assertIn("**Agreed revision text:** Agreed wording.", md)
        self.assertIn("Proposed text (superseded): ~~Old wording.~~", md)
        self.assertNotIn("~~Agreed wording.~~", md)


class AudienceDoorTests(HallCase):
    """The audience door (Marlin + a security review, 10-28): audience seats from other household devices, during the debate only."""

    DOOR = "192.168.1.10:8812"

    def setUp(self):
        super().setUp()
        self.hall.point_at_paper(self.did, "paper.md")
        self.door = TestClient(create_app(self.hall, 8812, door={"host": self.DOOR, "debate": self.did}),
                               base_url=f"http://{self.DOOR}")
        _, self.tok["Bot"] = self.hall.add_seat(self.did, "Bot", "audience", "agent")
        self.s["Bot"] = self.hall.authenticate(self.tok["Bot"])

    def h(self, who):
        return {"Authorization": f"Bearer {self.tok[who]}"}

    def test_only_the_audience_of_that_debate_gets_in(self):
        self.assertEqual(self.door.get("/me", headers=self.h("Guest")).status_code, 200)
        self.assertEqual(self.door.get("/me", headers=self.h("Pebble")).status_code, 403)       # an advocate: not here
        self.assertEqual(self.door.get("/me", headers=self.h("Marlin")).status_code, 403)    # nor the judge
        self.hall.set_role(self.s["Marlin"], "Guest", "witness")                            # moved: refused at once
        self.assertEqual(self.door.get("/me", headers=self.h("Guest")).status_code, 403)

    def test_only_audience_routes_exist_and_only_the_gallery(self):
        for path in ("/objections", "/floor/give", "/close", "/phase", "/seats/role", "/export/ledger.md"):
            self.assertEqual(self.door.post(path, json={}, headers=self.h("Guest")).status_code, 404, path)
        self.assertEqual(self.door.post("/say", json={"room": "hall", "text": "hi"}, headers=self.h("Guest")).status_code, 403)
        self.assertEqual(self.door.post("/say", json={"room": "gallery", "text": "why pineapple?"},
                                        headers=self.h("Guest")).status_code, 200)
        self.assertEqual(self.door.get("/me", headers={**self.h("Guest"), "Host": "evil.example"}).status_code, 421)
        self.assertEqual(self.door.get("/", headers={"Host": "127.0.0.1:8812"}).status_code, 421)

    def test_gallery_posts_are_rate_limited(self):
        codes = [self.door.post("/say", json={"room": "gallery", "text": f"q{i}"}, headers=self.h("Guest")).status_code
                 for i in range(7)]
        self.assertEqual(codes, [200] * 6 + [429])

    def test_a_person_claims_a_seat_once_with_a_code(self):
        code = self.hall.issue_claim(self.did, "Guest")
        self.assertEqual(self.door.get("/me", headers=self.h("Guest")).status_code, 401)   # the old token stopped
        got = self.door.post("/claim", json={"code": code.lower()}).json()
        self.assertEqual(got["name"], "Guest")
        self.assertEqual(self.door.get("/me", headers={"Authorization": f"Bearer {got['token']}"}).status_code, 200)
        self.assertEqual(self.door.post("/claim", json={"code": code}).status_code, 403)   # once only
        code2 = self.hall.issue_claim(self.did, "Pebble")                                      # not an audience seat
        self.assertEqual(self.door.post("/claim", json={"code": code2}).status_code, 403)
        # a security review: refused at the door WITHOUT being used, so it still works on the hall's own computer.
        main = TestClient(create_app(self.hall, 8810), base_url="http://127.0.0.1:8810")
        got = main.post("/claim", json={"code": code2}).json()
        self.assertEqual(got["name"], "Pebble")
        self.assertEqual(main.get("/me", headers={"Authorization": f"Bearer {got['token']}"}).json()["seat"]["role"],
                         "advocate")

    def test_the_judge_revokes_and_closing_expires_every_audience_seat(self):
        self.hall.revoke(self.s["Marlin"], "Bot")
        self.assertEqual(self.door.get("/me", headers=self.h("Bot")).status_code, 401)
        with self.assertRaises(HallError):
            self.hall.revoke(self.s["Kelp"], "Guest")
        self.hall.close_debate(self.s["Marlin"])
        self.assertEqual(self.door.get("/me", headers=self.h("Guest")).status_code, 401)   # expired at the close
        last = self.hall.feed(self.s["Marlin"])[-1]["text"]
        self.assertIn("The audience door is closed", last)
        self.assertIn("Disable-NetFirewallRule", last)

    def test_audience_agents_are_woken_for_a_speaker_a_phase_or_a_shown_passage(self):
        self.hall.set_phase(self.s["Kelp"], "openings")
        self.hall.give_floor(self.s["Kelp"], "Pebble")
        self.hall.show(self.s["Pebble"], "4.2")
        self.hall.post(self.s["Pebble"], "hall", "an ordinary line wakes nobody in the audience")
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Bot"])], ["phase", "speaker", "show"])
        self.assertEqual(self.hall.attention(self.s["Guest"]), [])                          # a person: no wake

    def test_the_paper_and_citations_as_text(self):
        whole = self.door.get("/paper/text", headers=self.h("Guest")).json()
        self.assertIn("We claim the sky is blue.", whole["text"])
        part = self.door.get("/paper/text?section=4.2", headers=self.h("Guest")).json()["text"]
        self.assertTrue(part.startswith("## 4.2 Results"))
        self.assertNotIn("sky is blue", part)
        (self.dir / "docs" / "citations").mkdir()
        (self.dir / "docs" / "citations" / "survey.md").write_text("# The survey\n40 fans.\n", encoding="utf-8")
        self.assertEqual(self.door.get("/citations", headers=self.h("Guest")).json()["citations"], ["survey.md"])
        self.assertIn("40 fans", self.door.get("/citations/survey.md", headers=self.h("Guest")).json()["text"])
        self.assertEqual(self.door.get("/citations/..%5Cpaper.md", headers=self.h("Guest")).status_code, 404)

    def test_the_door_address_must_be_a_private_lan_address(self):
        from hall.__main__ import door_address
        self.assertEqual(door_address("192.168.1.10:8812"), ("192.168.1.10", 8812))
        for bad in ("0.0.0.0:8812", "127.0.0.1:8812", "8.8.8.8:8812", "192.168.1.10:80", "nonsense"):
            with self.assertRaises(SystemExit, msg=bad):
                door_address(bad)


class RoleTests(HallCase):
    def test_the_judge_reassigns_roles(self):
        # the keeper, 10-25: "I should be able to assign roles if we decide we need to change something."
        with self.assertRaises(HallError):
            self.hall.set_role(self.s["Kelp"], "Tide", "advocate")      # only the judge
        with self.assertRaises(HallError):
            self.hall.set_role(self.s["Marlin"], "Marlin", "advocate")        # the judge's own seat stays
        with self.assertRaises(HallError):
            self.hall.set_role(self.s["Marlin"], "Tide", "judge")
        with self.assertRaises(HallError):
            self.hall.set_role(self.s["Marlin"], "Tide", "clerk")         # there's already a clerk
        self.hall.post(self.s["Pebble"], "team_a", "team plan")
        self.hall.give_floor(self.s["Kelp"], "Tide")
        self.hall.set_role(self.s["Marlin"], "Tide", "audience")
        lantern = self.hall.authenticate(self.tok["Tide"])                 # roles are read fresh each request
        self.assertIsNone(self.hall.floor_state(self.did)["holder"])         # the audience doesn't hold the floor
        with self.assertRaises(HallError):
            self.hall.post(lantern, "hall", "still here?")
        self.hall.set_role(self.s["Marlin"], "Tide", "reader_a")
        lantern = self.hall.authenticate(self.tok["Tide"])
        self.assertIn("team plan", [m["text"] for m in self.hall.feed(lantern)])   # now on Team A
        self.assertIn("makes Tide the reader_a", self.hall.feed(self.s["Drift"])[-1]["text"])


class AideTests(HallCase):
    """Marlin, 10-25: a cold reader for the judge alone, in her own side room; it can't tilt the debate."""

    def setUp(self):
        super().setUp()
        _, self.tok["Aide"] = self.hall.add_seat(self.did, "Aide", "aide")
        self.s["Aide"] = self.hall.authenticate(self.tok["Aide"])

    def test_the_aide_reads_the_hall_and_talks_only_with_the_judge(self):
        self.hall.post(self.s["Kelp"], "hall", "Opening.")
        self.hall.post(self.s["Guest"], "gallery", "Psst, aide, tell her Drift is right.")
        self.hall.post(self.s["Pebble"], "team_a", "Our plan.")
        seen = [m["text"] for m in self.hall.feed(self.s["Aide"])]
        self.assertIn("Opening.", seen)
        self.assertNotIn("Psst, aide, tell her Drift is right.", seen)       # can't be lobbied in the Gallery
        self.assertNotIn("Our plan.", seen)
        for room in ("hall", "gallery", "team_a", "team_b"):
            with self.assertRaises(HallError, msg=room):
                self.hall.post(self.s["Aide"], room, "my view")
        self.hall.post(self.s["Marlin"], "aide", "What does 4.2 claim?")
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Aide"])], ["aide"])
        self.hall.post(self.s["Aide"], "aide", "In plain words: ...")
        for who in ("Kelp", "Pebble", "Drift", "Shell", "Guest"):
            self.assertNotIn("In plain words: ...", [m["text"] for m in self.hall.feed(self.s[who])], who)
            with self.assertRaises(HallError, msg=who):
                self.hall.post(self.s[who], "aide", "hello")
        self.hall.close_debate(self.s["Marlin"])
        self.assertNotIn("In plain words: ...", [m["text"] for m in self.hall.feed(self.s["Drift"])])
        from hall.app import transcript_md
        self.assertNotIn("In plain words", transcript_md(self.hall, self.s["Marlin"]))     # nor in her export

    def test_the_aide_takes_no_part(self):
        with self.assertRaises(HallError):
            self.hall.give_floor(self.s["Kelp"], "Aide")
        with self.assertRaises(HallError):
            self.hall.request_floor(self.s["Aide"])
        with self.assertRaises(HallError):
            self.hall.object(self.s["Aide"], "1", "c", "w")
        with self.assertRaises(HallError):
            self.hall.lookup(self.s["Aide"], "q")
        with self.assertRaises(HallError):
            self.hall.add_seat(self.did, "Aide2", "aide")


class LedgerTests(HallCase):
    def test_an_objection_goes_from_filed_to_ruled(self):
        o = self.hall.object(self.s["Drift"], "4.2", "the sky is blue", "only one case", quote="We claim")
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Pebble"])], ["objection", "huddle_prompt"])   # + confer (11-07)
        with self.assertRaises(HallError):
            self.hall.answer(self.s["Drift"], o["id"], "answering myself")
        self.hall.answer(self.s["Pebble"], o["id"], "Table 2 has more cases.")
        with self.assertRaises(HallError):
            self.hall.draft(self.s["Pebble"], o["id"], "overruled", "nope")
        self.hall.draft(self.s["Shell"], o["id"], "revise", "Table 2 exists but isn't cited in 4.2.")
        with self.assertRaises(HallError):
            self.hall.rule(self.s["Shell"], o["id"], "revise", "clerks don't rule")
        self.hall.rule(self.s["Marlin"], o["id"], "revise", "Cite table 2 in 4.2.")
        led = self.hall.ledger(self.did)[0]
        self.assertEqual(led["status"], "revise")
        self.assertEqual([e["kind"] for e in led["events"]], ["answer", "draft", "ruling"])
        self.assertIn("ruling", [a["kind"] for a in self.hall.attention(self.s["Drift"])])
        with self.assertRaises(HallError):
            self.hall.answer(self.s["Pebble"], o["id"], "too late")

    def test_each_debate_numbers_its_objections_from_one(self):
        # the moderator, a test debate 10-25: the dry run had taken #1, so the debate's first objection was #2.
        other = self.hall.create_debate("another")
        _, t = self.hall.add_seat(other, "X", "adversary")
        x = self.hall.authenticate(t)
        self.assertEqual(self.hall.object(x, "1", "c", "w")["id"], 1)
        self.assertEqual(self.hall.object(self.s["Drift"], "4", "c", "w")["id"], 1)
        self.assertEqual(self.hall.object(self.s["Drift"], "2", "c", "w")["id"], 2)
        self.hall.answer(self.s["Pebble"], 2, "answered")
        self.assertIn("objection #2", self.hall.feed(self.s["Drift"])[-1]["text"])
        self.assertEqual([o["id"] for o in self.hall.ledger(self.did)], [1, 2])
        self.assertEqual(self.hall.ledger(other)[0]["status"], "open")       # the other debate's #1 untouched

    def test_a_new_phase_clears_stale_asks_for_the_floor(self):
        self.hall.request_floor(self.s["Drift"])
        self.hall.set_phase(self.s["Kelp"], "rebuttals")
        self.assertEqual(self.hall.floor_state(self.did)["queue"], [])

    def test_a_ruling_needs_no_reason(self):
        # the keeper, 10-25: optional; a blank ruling adopts the clerk's draft, so the record still says why.
        a = self.hall.object(self.s["Drift"], "4", "c1", "w1")
        b = self.hall.object(self.s["Drift"], "2", "c2", "w2")
        self.hall.draft(self.s["Shell"], a["id"], "sustained", "3.2 contradicts 4.")
        self.hall.rule(self.s["Marlin"], a["id"], "sustained", "")
        self.hall.rule(self.s["Marlin"], b["id"], "overruled", "  ")
        led = {o["id"]: o for o in self.hall.ledger(self.did)}
        self.assertEqual(led[a["id"]]["events"][-1]["text"], "Adopts the clerk's draft (it proposed sustained).")
        self.assertEqual(led[b["id"]]["events"][-1]["text"], "(no reason given)")
        self.assertEqual(led[b["id"]]["status"], "overruled")

    def test_who_may_object_and_concede(self):
        for who in ("Marlin", "Shell", "Kelp", "Guest"):
            with self.assertRaises(HallError, msg=who):
                self.hall.object(self.s[who], "1", "x", "y")
        o = self.hall.object(self.s["ReaderB"], "1", "intro claim", "unsupported")
        with self.assertRaises(HallError):
            self.hall.answer(self.s["ReaderA"], o["id"], "conceding for the team", "concede")
        self.hall.answer(self.s["Pebble"], o["id"], "fair", "concede", proposed="We test whether cats prefer boxes.")
        led = self.hall.ledger(self.did)[0]
        self.assertEqual(led["status"], "conceded")
        self.assertEqual(led["events"][0]["proposed"], "We test whether cats prefer boxes.")

    def test_the_status_keeps_the_strongest_outcome_so_far(self):
        # an advocate, dry run 10-25: a testimony after a partial answer had overwritten the status back to "answered".
        o = self.hall.object(self.s["Drift"], "4", "c", "w")
        self.hall.answer(self.s["Pebble"], o["id"], "partly", "partial", proposed="narrower")
        self.hall.answer(self.s["Pebble"], o["id"], "the record", "testimony",
                         evidence=[{"path": "paper.md", "sha": "abc1234"}])
        self.assertEqual(self.hall.ledger(self.did)[0]["status"], "partial")
        self.hall.answer(self.s["Pebble"], o["id"], "fine", "concede", proposed="dropped",
                         evidence=[{"path": "paper.md", "sha": "abc1234"}])   # a concession may carry evidence
        led = self.hall.ledger(self.did)[0]
        self.assertEqual(led["status"], "conceded")
        self.assertEqual(led["events"][-1]["evidence"][0]["path"], "paper.md")
        self.hall.answer(self.s["Tide"], o["id"], "more context")
        self.assertEqual(self.hall.ledger(self.did)[0]["status"], "conceded")

    def test_a_partial_answer_brings_its_rewording(self):
        # an advocate, 10-25: a concession or partial answer arrives with the fix drafted, so the revision list is ready.
        o = self.hall.object(self.s["Drift"], "4", "cats prefer boxes", "night two")
        with self.assertRaises(HallError):
            self.hall.answer(self.s["Pebble"], o["id"], "partly right", "partial")
        self.hall.answer(self.s["Pebble"], o["id"], "partly right", "partial", proposed="Cats prefer new boxes.")
        self.assertEqual(self.hall.ledger(self.did)[0]["status"], "partial")

    def test_testimony_must_cite_evidence_by_path_and_sha(self):
        # an advocate, 10-25: the advocate is also the runner, so "how was it measured" answers cite checkable evidence.
        o = self.hall.object(self.s["Drift"], "2", "12 cats", "how were they chosen?")
        for bad in (None, [], [{"path": "manifest.json"}], [{"path": "m.json", "sha": "zzz"}],
                    [{"path": "m.json", "sha": "abc1234", "note": "x" * 601}]):
            with self.assertRaises(HallError, msg=repr(bad)):
                self.hall.answer(self.s["Pebble"], o["id"], "from the manifest", "testimony", evidence=bad)
        self.hall.answer(self.s["Tide"], o["id"], "from the manifest", "testimony",
                         evidence=[{"path": "runs/manifest.json", "sha": "ABC1234", "note": "the roster"}])
        ev = self.hall.ledger(self.did)[0]["events"][0]
        self.assertEqual(ev["kind"], "testimony")
        self.assertEqual(ev["evidence"][0]["sha"], "abc1234")

    def test_quotes_are_capped(self):
        with self.assertRaises(HallError):
            self.hall.object(self.s["Drift"], "4", "claim", "why", quote="x" * 601)


class RecoveryTests(HallCase):
    def test_one_call_tells_a_seat_where_it_stands(self):
        # an advocate, 10-25: a seat squished mid-debate recovers from the ledger, not from its summary.
        a = self.hall.object(self.s["Drift"], "4", "c1", "w1")
        self.hall.object(self.s["Drift"], "2", "c2", "w2")
        self.hall.answer(self.s["Pebble"], a["id"], "answered")
        self.hall.give_floor(self.s["Kelp"], "Drift", minutes=5)
        plumb = self.hall.status(self.s["Drift"])
        self.assertEqual([o["status"] for o in plumb["mine"]], ["answered", "open"])
        self.assertIn("Drift holds the floor", plumb["who_may_speak"])
        self.assertEqual([o["anchor"] for o in self.hall.status(self.s["Pebble"])["to_answer"]], ["2"])
        self.assertEqual(len(self.hall.status(self.s["Shell"])["to_draft"]), 2)
        self.assertEqual(len(self.hall.status(self.s["Marlin"])["to_rule"]), 2)

    def test_lookups_are_logged_in_the_gallery_for_all_to_see(self):
        # an advocate, 10-25: the moderator stays visibly even-handed (the Gallery's audience sees every lookup; everyone
        # does after the debate).
        self.hall.lookup(self.s["ReaderB"], "Is box preference in prior work?")
        line = [m for m in self.hall.feed(self.s["Guest"]) if m["room"] == "gallery"][-1]
        self.assertIn("Lookup for Team B (ReaderB)", line["text"])
        self.assertEqual([a["kind"] for a in self.hall.attention(self.s["Kelp"])], ["lookup"])
        with self.assertRaises(HallError):
            self.hall.lookup(self.s["Guest"], "can I?")


class PaperTests(HallCase):
    def test_the_paper_must_be_a_markdown_file_inside_debate_docs_and_is_versioned(self):
        for bad in ("../outside.md", "nope.md", "../hall.db"):
            with self.assertRaises(HallError, msg=bad):
                self.hall.set_paper(self.s["Kelp"], bad)
        with self.assertRaises(HallError):
            self.hall.set_paper(self.s["Drift"], "paper.md")
        sha = self.hall.set_paper(self.s["Kelp"], "paper.md")["sha"]
        self.assertEqual(self.hall.paper_text(self.did)[1], sha)
        (self.dir / "docs" / "paper.md").write_text(PAPER + "\nedited\n", encoding="utf-8")
        with self.assertRaises(HallError):
            self.hall.paper_text(self.did)

    def test_the_keeper_sets_the_paper_before_anyone_prepares(self):
        # Reader B, a test debate: status said "No paper set yet" while the readers prepared from a file path.
        sha = self.hall.point_at_paper(self.did, "paper.md")["sha"]
        self.assertEqual(self.hall.paper_text(self.did)[1], sha)
        with self.assertRaises(HallError):
            self.hall.point_at_paper(self.did, "../outside.md")

    def test_rendering_anchors_numbered_sections_and_passes_no_raw_html(self):
        html, heads = render_paper(PAPER)
        self.assertIn('id="s-4.2"', html)
        self.assertIn("<table>", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="javascript:', html)
        self.assertEqual([h["section"] for h in heads], [None, "1", "4.2"])

    def test_the_papers_own_figures_arrive_inside_the_page(self):
        figs = self.dir / "docs" / "figures"
        figs.mkdir(parents=True)
        (figs / "figure1.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (self.dir / "docs" / "secret.png").write_bytes(b"\x89PNG\r\n\x1a\nnot a figure")
        text = ("![one](figures/figure1.png) ![missing](figures/nope.png) ![up](figures/../secret.png) "
                "![away](http://example.com/x.png)")
        html, _ = render_paper(text, figs)
        self.assertIn('src="data:image/png;base64,iVBORw0KGgpmYWtl"', html)
        self.assertIn('src="figures/nope.png"', html)            # left as written; the CSP won't fetch it
        self.assertNotIn("bm90IGEgZmlndXJl", html)                # the file outside figures/ never inlined
        self.assertIn('src="http://example.com/x.png"', html)

    def test_show_is_for_whoever_may_speak(self):
        self.hall.set_paper(self.s["Kelp"], "paper.md")
        with self.assertRaises(HallError):
            self.hall.show(self.s["Drift"], "4.2")
        self.hall.give_floor(self.s["Kelp"], "Drift")
        self.assertEqual(self.hall.show(self.s["Drift"], "4.2")["section"], "4.2")
        self.assertEqual(self.hall.show(self.s["Drift"], '"sky is blue"')["phrase"], "sky is blue")


class HttpTests(HallCase):
    def setUp(self):
        super().setUp()
        self.http = TestClient(create_app(self.hall, 8810), base_url="http://127.0.0.1:8810")

    def h(self, who, **extra):
        return {"Authorization": f"Bearer {self.tok[who]}", **extra}

    def test_every_route_needs_a_token_and_the_page_does_not(self):
        self.assertEqual(self.http.get("/").status_code, 200)
        self.assertEqual(self.http.get("/me").status_code, 401)
        self.assertEqual(self.http.get("/me", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(self.http.get("/me", headers=self.h("Pebble")).json()["seat"]["role"], "advocate")

    def test_a_page_of_the_feed_says_when_more_are_waiting(self):
        for i in range(5):
            self.http.post("/say", json={"room": "team_a", "text": f"line {i}"}, headers=self.h("Pebble"))
        r = self.http.get("/feed?limit=3", headers=self.h("ReaderA")).json()
        self.assertEqual((len(r["items"]), r["more"]), (3, True))
        r = self.http.get(f"/feed?after={r['items'][-1]['id']}&limit=3", headers=self.h("ReaderA")).json()
        self.assertEqual((len(r["items"]), r["more"]), (2, False))

    def test_the_page_and_its_files_are_rechecked_after_an_update(self):
        # the keeper's Chrome paired a cached old hall.css with the new page (10-25).
        for path in ("/", "/web/hall.css", "/web/hall.js", "/web/paper.html", "/web/paper.js"):
            self.assertEqual(self.http.get(path).headers.get("cache-control"), "no-cache", path)

    def test_wrong_host_and_cross_site_origin_are_refused(self):
        self.assertEqual(self.http.get("/me", headers=self.h("Pebble", Host="evil.example")).status_code, 421)
        self.assertEqual(self.http.get("/me", headers=self.h("Pebble", Host="127.0.0.1:9999")).status_code, 421)
        r = self.http.post("/say", json={"room": "team_a", "text": "hi"}, headers=self.h("Pebble", Origin="https://evil.example"))
        self.assertEqual(r.status_code, 403)
        r = self.http.post("/say", json={"room": "team_a", "text": "hi"}, headers=self.h("Pebble", Origin="http://127.0.0.1:8810"))
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("access-control-allow-origin", self.http.get("/me", headers=self.h("Pebble")).headers)

    def test_the_server_refuses_what_the_page_would_hide(self):
        r = self.http.post("/say", json={"room": "hall", "text": "I object"}, headers=self.h("Drift"))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.http.post("/floor/give", json={"name": "Drift"}, headers=self.h("Pebble")).status_code, 403)
        self.assertEqual(self.http.post("/close", headers=self.h("Kelp")).status_code, 403)

    def test_exports(self):
        self.hall.object(self.s["Drift"], "4.2", "claim", "why")
        self.assertIn("# Objection ledger", self.http.get("/export/ledger.md", headers=self.h("Marlin")).text)
        self.assertIn("Objection #1", self.http.get("/export/transcript.md", headers=self.h("Marlin")).text)

    def test_exports_name_seats_by_role(self):
        # an advocate, 10-25: the judge's own name is on the household list, and exports travel.
        canaries = self.dir / "canaries.txt"
        canaries.write_text("Marlin\nWhomever\n", encoding="utf-8")
        http = TestClient(create_app(self.hall, 8810, canary_file=canaries), base_url="http://127.0.0.1:8810")
        self.hall.post(self.s["Kelp"], "hall", "Welcome. @Drift, you first; Marlin will rule.")
        self.hall.object(self.s["Drift"], "4", "claim", "why")
        self.hall.rule(self.s["Marlin"], 1, "revise", "Fix it.")
        t = http.get("/export/transcript.md", headers=self.h("Marlin"))
        self.assertEqual(t.status_code, 200, t.text)
        self.assertIn("Moderator: Welcome. Adversary, you first; Judge will rule.", t.text)
        self.assertNotIn("Marlin", t.text)
        led = http.get("/export/ledger.md", headers=self.h("Marlin")).text
        self.assertIn("(Adversary)", led)
        self.assertIn("Judge, ruling (revise)", led)

    def test_the_canary_list_format(self):
        canaries = self.dir / "canaries.txt"
        canaries.write_text("# a long comment line naming nobody\ncs:Rose\nlily\n", encoding="utf-8")
        http = TestClient(create_app(self.hall, 8810, canary_file=canaries), base_url="http://127.0.0.1:8810")
        # (A transcript is what the exporting seat may read: the moderator reads the Gallery.)
        self.hall.post(self.s["Guest"], "gallery", "a rose by any other name")      # lower case: not the name
        self.assertEqual(http.get("/export/transcript.md", headers=self.h("Kelp")).status_code, 200)
        self.hall.post(self.s["Guest"], "gallery", "Rose says hi")
        flagged = http.get("/export/transcript.md", headers=self.h("Kelp"))
        self.assertTrue(flagged.text.startswith("<!-- CANARY CHECK: 1 household term(s) found: Rose."))

    def test_the_halls_allow_list_drops_a_term_without_touching_the_shared_list(self):
        canaries = self.dir / "canaries.txt"
        canaries.write_text("cs:Rose\nlily\n", encoding="utf-8")
        allow = self.dir / "canary_allow.txt"
        allow.write_text("# words that are fine here\nLILY\n", encoding="utf-8")
        http = TestClient(create_app(self.hall, 8810, canary_file=canaries, canary_allow=allow),
                          base_url="http://127.0.0.1:8810")
        self.hall.post(self.s["Guest"], "gallery", "lily wrote this")
        self.assertEqual(http.get("/export/transcript.md", headers=self.h("Kelp")).status_code, 200)
        self.hall.post(self.s["Guest"], "gallery", "Rose says hi")
        self.assertIn("found: Rose.", http.get("/export/transcript.md", headers=self.h("Kelp")).text.splitlines()[0])
        self.assertEqual(canaries.read_text(encoding="utf-8"), "cs:Rose\nlily\n")

    def test_an_export_names_the_household_words_it_holds_on_its_first_line(self):
        # the keeper, 10-30: export anyway, and name the terms on the first line so they can be searched and dealt with.
        canaries = self.dir / "canaries.txt"
        canaries.write_text("# household words\n\nWhiskers\n", encoding="utf-8")
        http = TestClient(create_app(self.hall, 8810, canary_file=canaries), base_url="http://127.0.0.1:8810")
        ok = http.get("/export/ledger.md", headers=self.h("Marlin"))
        self.assertEqual(ok.status_code, 200)
        self.assertIn("canary check: passed (1 terms)", ok.text)
        self.hall.post(self.s["Guest"], "gallery", "my cat whiskers says hi")
        flagged = http.get("/export/transcript.md", headers=self.h("Kelp"))
        self.assertEqual(flagged.status_code, 200)
        self.assertEqual(flagged.text.splitlines()[0], "<!-- CANARY CHECK: 1 household term(s) found: Whiskers. "
                         "Search for each below and deal with it before sharing this file. -->")
        self.assertIn("my cat whiskers says hi", flagged.text)
        self.assertNotIn("canary check: passed", flagged.text)
        self.hall.post(self.s["Guest"], "gallery", "Whiskersville is a town")   # not a whole word: no extra hit
        self.assertIn("1 household term", http.get("/export/transcript.md", headers=self.h("Kelp")).text)

    def test_the_export_shows_the_current_wording_and_what_the_revision_must_do(self):
        # an advocate, a test debate: three improving answers left the reader to work out which proposed text stood.
        from hall.app import ledger_md
        o = self.hall.object(self.s["Drift"], "2.2", "belongs", "for whom?")
        self.hall.answer(self.s["Pebble"], o["id"], "first try", "partial", proposed="Old wording.")
        self.hall.answer(self.s["Pebble"], o["id"], "better", "partial", proposed="New wording.")
        self.assertIn("**Current proposed text:** New wording.", ledger_md(self.hall, self.did))   # before the ruling
        self.hall.rule(self.s["Marlin"], o["id"], "revise", "Say by whom.")
        md = ledger_md(self.hall, self.did)
        self.assertIn("**Agreed revision text:** New wording.", md)                               # after it (security review)
        self.assertIn("Proposed text (superseded): ~~Old wording.~~", md)
        self.assertIn("**What the revision must do:** Say by whom.", md)

    def test_status_and_lookup_routes(self):
        self.assertIn("who_may_speak", self.http.get("/status", headers=self.h("Drift")).json())
        self.assertIn("who_may_speak", self.http.get("/me", headers=self.h("Drift")).json())
        r = self.http.post("/lookup", json={"question": "prior work?"}, headers=self.h("Drift"))
        self.assertEqual(r.status_code, 200)
        r = self.http.post("/objections/99/answer", json={"text": "x", "mode": "testimony"}, headers=self.h("Pebble"))
        self.assertIn(r.status_code, (400, 404))



class HelperClaimOrderTests(unittest.TestCase):
    def test_token_file_works_on_either_side_of_the_claim_code(self):
        # A clean-install test (11-08): the docs say "claim <code> --token-file <file>", which the helper refused.
        spec = importlib.util.spec_from_file_location("hall_tool_order", ROOT / "tools" / "hall.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        with tempfile.TemporaryDirectory() as d:
            seen = Path(d) / "seat.token"
            seen.write_text("already here", encoding="utf-8")       # refused before any network: proves the path arrived
            for argv in (["claim", "ABCD", "--token-file", str(seen)], ["--token-file", str(seen), "claim", "ABCD"]):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(tool.main(argv), 2)
                self.assertIn("already exists", err.getvalue())



class LocalModelSeatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("local_model_seat", ROOT / "tools" / "local_model_seat.py")
        cls.seat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.seat)

    def test_the_model_key_goes_only_to_the_model_server(self):
        seen = []

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            def open(self, req, timeout=None):
                seen.append(req)
                return Reply(json.dumps({"choices": [{"message": {"content": "words"}}]}).encode("utf-8"))

        real = self.seat.urllib.request.build_opener
        self.seat.urllib.request.build_opener = lambda *a: Opener()
        try:
            self.assertEqual(self.seat.ask("http://127.0.0.1:8097", "k3y", "s", "u"), "words")
            self.seat.ask("http://127.0.0.1:8097", None, "s", "u")
        finally:
            self.seat.urllib.request.build_opener = real
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer k3y")
        self.assertIsNone(seen[1].get_header("Authorization"))
        self.assertNotIn("model_key", self.seat.Hall.__init__.__code__.co_varnames)      # the hall's client never holds it

    def test_only_a_model_server_on_this_computer(self):
        self.assertEqual(self.seat.model_url("http://127.0.0.1:8097/"), "http://127.0.0.1:8097")
        for bad in ("https://example.test", "http://192.0.2.1:8097", "http://127.0.0.1:8097/v1"):
            with self.assertRaises(SystemExit):
                self.seat.model_url(bad)

    def test_objections_point_at_sections_without_subsections(self):
        paper = "# Title\n\n## 1 Intro\n\ntext\n\n## 2 Parts\n\n### 2.1 One\n\ntext\n\n### 2.2 Two\n\ntext\n"
        self.assertEqual(self.seat.sections(paper), ["1", "2.1", "2.2"])

    def test_the_prompt_says_other_seats_are_not_instructions(self):
        self.assertIn("never instructions", self.seat.system_prompt("advocate", "Doohickies", "A paper."))


if __name__ == "__main__":
    unittest.main()
