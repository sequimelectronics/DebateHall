# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
"""The hall's sqlite store: one file, one lock (everything is small and local)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS debates (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'setup',
    paper_path TEXT,
    paper_sha TEXT,
    status TEXT NOT NULL DEFAULT 'open',          -- open | closed
    floor_seat_id INTEGER,
    floor_until REAL,
    created_at REAL NOT NULL,
    closed_at REAL
);
CREATE TABLE IF NOT EXISTS seats (
    id INTEGER PRIMARY KEY,
    debate_id INTEGER NOT NULL REFERENCES debates(id),
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,                           -- person | agent
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    UNIQUE (debate_id, name)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    debate_id INTEGER NOT NULL REFERENCES debates(id),
    room TEXT NOT NULL,                           -- hall | gallery | team_a | team_b
    seat_id INTEGER,                              -- NULL for the hall's own notices
    kind TEXT NOT NULL,                           -- say | notice | show | objection | answer | draft | ruling
    text TEXT NOT NULL,
    data TEXT,                                    -- JSON
    paper_sha TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_debate ON messages (debate_id, id);
CREATE TABLE IF NOT EXISTS floor_requests (
    debate_id INTEGER NOT NULL,
    seat_id INTEGER NOT NULL,
    requested_at REAL NOT NULL,
    PRIMARY KEY (debate_id, seat_id)
);
CREATE TABLE IF NOT EXISTS attention (
    id INTEGER PRIMARY KEY,
    debate_id INTEGER NOT NULL,
    seat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                           -- floor | objection | named | ruling
    ref INTEGER,                                  -- message or objection id
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS objections (
    id INTEGER PRIMARY KEY,
    debate_id INTEGER NOT NULL,
    seat_id INTEGER NOT NULL,
    anchor TEXT NOT NULL,
    quote TEXT,
    claim TEXT NOT NULL,
    why TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',          -- open | answered | conceded | sustained | overruled | revise
    paper_sha TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS objection_events (
    id INTEGER PRIMARY KEY,
    objection_id INTEGER NOT NULL REFERENCES objections(id),
    seat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                           -- answer | partial | testimony | concede | draft | ruling
    verdict TEXT,
    text TEXT NOT NULL,
    proposed TEXT,                                -- a concession's or partial answer's proposed rewording
    evidence TEXT,                                -- testimony: JSON [{path, sha, note}]
    created_at REAL NOT NULL
);
-- Access like AI-Beach's (the keeper + a security review, 11-07): a participant is one person or agent across debates; each device
-- signs in on its own; a seat links a participant to a role in one debate.
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                           -- person | agent
    access TEXT NOT NULL DEFAULT 'resident',      -- resident | visitor
    is_admin INTEGER NOT NULL DEFAULT 0,          -- the keeper
    created_at REAL NOT NULL,
    removed_at REAL
);
CREATE TABLE IF NOT EXISTS sign_ins (
    id INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    debate_id INTEGER,                            -- set: this sign-in reaches that debate only
    created_at REAL NOT NULL,
    last_used REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    code_hash TEXT NOT NULL UNIQUE,
    debate_id INTEGER,                            -- the debate the claim is for (the door checks it before use)
    expires_at REAL NOT NULL,
    used_at REAL,
    created_at REAL NOT NULL
);
-- The huddle step (the keeper's debrief, 10-30): the chair calls a huddle; each team says when it's done.
CREATE TABLE IF NOT EXISTS huddles (
    id INTEGER PRIMARY KEY,
    debate_id INTEGER NOT NULL,
    called_by INTEGER NOT NULL,
    called_at REAL NOT NULL,
    ends_at REAL,                                 -- the block of time granted (the keeper, 11-07)
    ended_at REAL,                                -- both done, time up, or the chair ended it
    done_a REAL,
    done_b REAL
);
CREATE TABLE IF NOT EXISTS objection_links (     -- "#6 depends on #5" (the keeper, 10-25: complex topics, complex objections)
    debate_id INTEGER NOT NULL,
    from_num INTEGER NOT NULL,                    -- the objection that depends ...
    to_num INTEGER NOT NULL,                      -- ... on this one
    seat_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (debate_id, from_num, to_num)
);
"""
# Columns added after the first build: (table, column, declaration).
LATER_COLUMNS = (("objection_events", "proposed", "TEXT"), ("objection_events", "evidence", "TEXT"),
                 ("debates", "paced", "INTEGER"),                             # NULL: paced when the judge is a person
                 ("debates", "waiting", "INTEGER NOT NULL DEFAULT 0"),        # waiting on the judge's Continue
                 ("objections", "num", "INTEGER"),                            # numbered from 1 within each debate
                 # The audience door (the keeper + a security review, 10-28): a seat can be revoked (by the judge, or every audience
                 # seat when its debate closes), and a person's seat can start as a one-time claim code.
                 ("seats", "revoked_at", "REAL"),
                 ("seats", "claim_hash", "TEXT"),
                 ("seats", "claim_expires", "REAL"),
                 # Access like AI-Beach's (11-07): the seat's holder; and the roles a debate leaves out.
                 ("seats", "participant_id", "INTEGER"),
                 ("debates", "roles_off", "TEXT"),
                 # When each seat last checked for wakes (GET /attention): the chair's "who's listening" (10-30).
                 ("seats", "last_check", "REAL"),
                 # An agent switches its own wakes off (the keeper, 10-30: "good idea"); NULL = on.
                 ("seats", "wake_off", "REAL"))


class Database:
    def __init__(self, path: Path | str):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            for table, col, decl in LATER_COLUMNS:
                have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def run(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def close(self) -> None:
        self.conn.close()
