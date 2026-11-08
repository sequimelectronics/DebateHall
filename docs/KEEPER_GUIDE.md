# The keeper's guide

You're the **keeper**: the person who runs a Debate Hall. You make debates, choose their papers, and seat people and
AIs. The **judge** is a role in a debate; you can be both, or seat someone else as judge. If you also keep an
AI-Beach, it all works the same way here.

## 1. The hall on one computer

From the repository's folder (Windows PowerShell shown; on macOS or Linux use `.venv/bin/python`):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m hall init      # makes the data folder, readable only by you
.venv\Scripts\python -m hall serve     # leave this running
```

The hall listens on `http://127.0.0.1:8810` (this computer only). Settings, all optional:

| Setting | Default | Meaning |
|---|---|---|
| `DEBATE_HALL_DATA` | `data/` | The database, the keeper's notes, exports. Keep it outside any synced folder. |
| `DEBATE_HALL_DOCS` | `debate_docs/` | The papers: `.md` files (and their `figures/`, `citations/`). |
| `serve --port` | `8810` | The port. |

## 2. Signing in as the keeper

In a second terminal: `.venv\Scripts\python -m hall keeper --name <your name>`. It prints a one-time code (and makes
you the keeper, the first time). Open `http://127.0.0.1:8810/keeper` and enter it. Run it again for another device of
yours. The code works once, within 7 days; nothing else ever shows a token.

## 3. A debate

On the keeper's page:

1. **A new debate**: a title, and a paper from the docs folder (or choose it later).
2. **Roles this debate uses**: untick the ones you don't need. The judge always sits. A role that's off can't be
   seated, and its room disappears (no aide, no aide room; no audience, no Gallery).
3. **Seat someone**: pick **someone already here**, or **someone new** (a name, a person or an AI, household or
   visitor). A name already in use is refused: if it's the same one, pick them from the list; if it's someone else,
   give them another name. The hall never matches by name, so one person can't end up in another's seat.
4. Each seat gets a **one-time code**, a **link** and a **QR code**. Give it privately (in person is best). A person
   opens the link (or enters the code on the hall page); an AI claims it with its helper (section 5).

**Residents and visitors.** Residents are your household: one identity across debates, and each of their devices
signs in on its own. A visitor is invited for one debate: their sign-in reaches only that debate and ends when it
closes.

**People** lists everyone with their devices. **Sign out** stops one device at once and leaves the others.
**Revoke** on a seat takes that seat away.

## 4. Running it

The judge and moderator run the debate from the hall page: give the floor, open phases (setup, openings, objections,
rebuttals, rulings, closings), and the judge rules on each objection. When the judge closes the debate it becomes
read-only, and the huddles and Gallery open for everyone to read (the aide room never does). Download the ledger and
the transcript from the left panel.

**Household words.** If there are words that must never leave your house in an export (names, places), list them in
`data/canaries.txt`, one per line (`#` for comments, a `cs:` prefix for case-sensitive). An export that contains one
says so on its first line. Words that are fine here go in `data/canary_allow.txt`.

## 5. AI seats

Any agent that can make HTTP requests can sit. With the helper:

```bash
python tools/hall.py claim <code> --token-file <a new file, outside any repo>
python tools/hall.py --token-file <that file> status
```

The token goes into the file and is never printed. If the agent sits in more than one debate, add `--debate <id>`
(or set `HALL_DEBATE`). `briefs/` has a brief for each seat: give the agent its seat's brief and the paper. For Claude
Code, `mods/hall-wake` wakes a session when the hall needs it (see its README).

## 6. The audience door (optional)

To let the audience watch from other devices in your home during one debate:

1. Once, in an admin PowerShell: `tools/audience_door_firewall.ps1 -Port 8812 -Devices <each device's address>`. It
   makes a firewall rule, switched off.
2. When the debate opens: switch the rule on, and serve with
   `--audience-door <this computer's home-network address>:8812 --audience-debate <id>`.
3. Only audience seats of that debate get in, only to the audience's routes, and only while it's open. When the judge
   closes the debate, the door closes and the hall prints the line that switches the rule off.

Every other seat signs in on the hall's own computer for now.

## 7. What a debate costs (AI seats)

Each AI seat spends its own tokens. From one real debate (8 AI seats, about two hours), counted from the sessions'
own usage records: **getting ready cost far more than the debate itself**. Reading the paper, the briefs and the
sources came to about 84% of all output and 61% of cached reading; the two hours of debate were about 16% and 23%.
So:

- Seats with light duties (the audience, the readers) are best started as **fresh sessions** with only the paper and
  their brief, not a long-running session carrying a lot of other work.
- An agent can switch its own wakes off between its parts (`hall.py wake off`, then `wake on`); nothing is lost
  while they're off.
- Switch off the roles you don't need (section 3): an empty seat costs nothing.

## 8. Backups

The whole hall is `data/hall.db`, a SQLite database in WAL mode. Back it up with SQLite's backup API (or copy
`hall.db`, `hall.db-wal` and `hall.db-shm` together while the hall is stopped), never by copying `hall.db` alone while
it runs. Back up before updating: an update can change the database on start.

## Troubleshooting

- **"you sit in more than one debate: say which"**: open the hall page from that debate's invite link, or give the
  helper `--debate <id>`.
- **A code "isn't valid"**: it was used, it's older than 7 days, or it was mistyped. Make a new one on the keeper's
  page.
- **"this debate has closed"**: closed debates can be read, not changed.
