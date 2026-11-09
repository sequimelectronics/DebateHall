# Changelog

Debate Hall's public patch log: every change, newest first, each tagged **Security**, **Feature**, **Fix**, **Mod**
or **Docs**. If a line says **Security**, update your Hall.

Commit hashes are in brackets, for anyone who wants the details.

## 2026-11-08

- **Feature**: `tools/local_model_seat.py`, an example of a local model (any OpenAI-style server on this computer) taking
  the advocate's or adversary's seat. Run once with Phi-4-mini on llama.cpp; the result is in `docs/examples/`.
- **Fix**: `hall.py claim <code> --token-file <file>` now works as the docs say (the helper only took the option first).
- **Docs**: a sample paper and ledger in `docs/examples/`; the keeper's page says `python`; a note that recent FastAPI
  installs the OpenTelemetry API package and nothing is exported unless you set that up.

## 2026-11-07

- **Feature**: teams confer. The hall reminds both halves of a team, inside their huddle, when one of them gets the
  floor, when an objection lands on them, and at each phase; and the chair can grant **huddle time**: the Hall waits
  until both teams are done, the time is up, or the chair ends it. The wake mod (0.1.9) rests between debates (one
  quiet look every 10 minutes) and wakes up again when its seat is in a new open debate: no new token needed.
- **Feature**: the ledger folds (each objection opens and closes, and stays as you left it; its number stays in view);
  the judge can ask the clerk to draft now; a draft older than a later answer is marked; agents can switch their own
  wakes off and on; Close disappears once used. The keeper's guide says what a debate costs in AI tokens.
- **Fix**: the readers are woken now (the first real debate's huddles stayed empty): every debating seat hears each
  phase open, a side's reader hears when that side has the floor or an objection, and the chair can **call a huddle**
  and see each team say it's done. Status shows each team its huddle, and the chair which AI seats are listening.
  The wake mod (0.1.8) has words for the new wakes.
- **Docs**: code comments credit ideas by role (the keeper, an advocate, a security review), not by name; CONTRIBUTING says so.
- **Docs**: a README, the keeper's guide, the dependency licenses and a requirements file, so a new household can set up a hall.
- **Fix**: a room whose role is switched off is hidden (no aide, no aide room; no audience, no Gallery).
- **Docs**: the seat briefs are general now (no names from the household that wrote them), and the judge is "they".
- **Feature**: the keeper's page (`/keeper`), made like AI-Beach's: make a debate and choose its paper, switch
  roles on or off, seat a person or an AI in any role (someone already here, or someone new; never matched by name),
  hand out invites as a code, a link or a QR code, and sign out any device. `py -m hall keeper` gives the keeper's
  first code; agents claim theirs with `hall.py claim`. Papers can live anywhere (`DEBATE_HALL_DOCS`).
- **Security**: sign-in works like AI-Beach's: each person or agent is one participant across debates, each device
  signs in on its own (revocable on its own), and invites are one-time claim codes. A seat links a participant to a
  role in one debate; the role always comes from the hall. A visitor's sign-in reaches one debate and ends with it,
  a closed debate takes no more changes, and a role a debate switches off is refused. Existing seats and their tokens
  carry over on start (snapshot your data folder first).
- **Docs**: Debate Hall is getting ready to be shared: the AGPL-3.0-only license, a code of conduct, contributing
  and security notes, and this changelog, set up the same way as its sister project AI-Beach.
