# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
"""The Debate Hall's HTTP API and page. Local only (127.0.0.1); every route but the page itself needs a seat token.

Loopback isn't private from a browser (security review, 10-25): any website can make the browser send requests to
127.0.0.1, and DNS rebinding can let a page read the answers. So: the Host header must be this hall's own
address, no CORS headers are ever sent, a cross-site Origin is refused, the token rides only in an Authorization
header (never a cookie or a URL), and nothing that changes state is a GET.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from markdown_it import MarkdownIt

from .core import ROOMS, Hall, HallError

WEB = Path(__file__).resolve().parent / "web"
STATUS = {"invalid": 400, "forbidden": 403, "not_found": 404, "not_your_floor": 409, "conflict": 409}
WAIT_MAX_S = 25.0
SECTION = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s")
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # Always ask before reusing a copy: the keeper's Chrome kept the old hall.css with the new page after an update
    # (10-25), which squeezed the paper off the right edge. A recheck is a cheap 304 when nothing changed.
    "Cache-Control": "no-cache",
}


FIGURE = re.compile(r"^figures/([A-Za-z0-9_-]{1,80})\.(png|jpe?g)$", re.IGNORECASE)
FIGURE_MAX = 4 * 1024 * 1024


def inline_figures(tokens, figures_dir: Path | None) -> None:
    """The paper's charts (figures/<name>.png beside it) go into the page as data: URIs, so they arrive with the
    paper through the seat's own signed-in request: no open image route, and the same at the audience door (10-30,
    a paper's charts). Anything else is left as written, and the page's CSP won't fetch it."""
    if not figures_dir or not figures_dir.is_dir():
        return
    import base64
    for tok in tokens:
        for child in tok.children or []:
            if child.type != "image":
                continue
            m = FIGURE.match(child.attrGet("src") or "")
            path = figures_dir / f"{m.group(1)}.{m.group(2)}" if m else None
            if path is None or path.resolve().parent != figures_dir.resolve() or not path.is_file() \
                    or path.stat().st_size > FIGURE_MAX:
                continue
            kind = "png" if m.group(2).lower() == "png" else "jpeg"
            child.attrSet("src", f"data:image/{kind};base64," + base64.b64encode(path.read_bytes()).decode("ascii"))


def render_paper(text: str, figures_dir: Path | None = None) -> tuple[str, list[dict]]:
    """Markdown to HTML with an id on every heading: s-4.2 for a numbered one, else a slug. Raw HTML in the
    paper is not passed through (html off); markdown-it already refuses javascript: links."""
    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = md.parse(text)
    inline_figures(tokens, figures_dir)
    headings, used = [], set()
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        title = tokens[i + 1].content if i + 1 < len(tokens) else ""
        m = SECTION.match(title)
        base = "s-" + m.group(1) if m else "h-" + (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "x")
        hid, n = base, 2
        while hid in used:
            hid, n = f"{base}-{n}", n + 1
        used.add(hid)
        tok.attrSet("id", hid)
        headings.append({"id": hid, "level": int(tok.tag[1]), "title": title, "section": m.group(1) if m else None})
    return md.renderer.render(tokens, md.options, {}), headings


def _term_lines(path: Path | None) -> list[str]:
    if not path or not Path(path).is_file():
        return []
    return [t for t in (ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines())
            if t and not t.startswith("#")]


def load_canaries(path: Path | None, allow: Path | None = None) -> list[tuple[str, bool]]:
    """Household words that must never appear in an export: one per line; blank lines and # comments skipped; a
    "cs:" prefix means match case-sensitively (a common list format); the rest ignore case.
    allow: the hall's own list of words that are fine here (data/canary_allow.txt). the keeper, 10-30: "I am the author.
    It is fine. That is my name." The shared list itself stays as its owners keep it."""
    allowed = {t.removeprefix("cs:").strip().lower() for t in _term_lines(allow)}
    out = [(t[3:].strip(), True) if t.startswith("cs:") else (t, False) for t in _term_lines(path)]
    return [(t, cs) for t, cs in out if t and t.lower() not in allowed]


def canary_hits(text: str, canaries: list[tuple[str, bool]]) -> int:
    return sum(1 for term, cs in canaries
               if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, 0 if cs else re.IGNORECASE))


ROLE_LABEL = {"judge": "Judge", "clerk": "Clerk", "moderator": "Moderator", "advocate": "Advocate",
              "adversary": "Adversary", "reader_a": "Team A reader", "reader_b": "Team B reader",
              "witness": "Witness", "aide": "Judge's aide", "audience": "Audience"}


def by_role(hall: Hall, did: int, text: str) -> str:
    """Exports name seats by their role, never by name (an advocate, 10-25: the judge's own name may be a household word, and
    exports travel). Every seat name, wherever it appears as a whole word, becomes its role label."""
    seats = sorted(hall.seats(did), key=lambda s: -len(s["name"]))
    audience = [s["name"] for s in seats if s["role"] == "audience"]
    for s in seats:
        label = ROLE_LABEL[s["role"]]
        if s["role"] == "audience" and len(audience) > 1:
            label = f"Audience {sorted(audience).index(s['name']) + 1}"
        text = re.sub(r"(?<!\w)@?" + re.escape(s["name"]) + r"(?!\w)", label, text, flags=re.IGNORECASE)
    return text


# The audience door (the keeper + a security review, 10-28): a second listener on the home network, for ONE debate's audience seats
# only, open only while that debate is open. Only these routes exist there; everything else is 404.
DOOR_ROUTES = {("GET", "/"), ("GET", "/web/"), ("GET", "/me"), ("GET", "/status"), ("GET", "/feed"),
               ("GET", "/wait"), ("GET", "/attention"), ("GET", "/ledger"), ("GET", "/paper"), ("GET", "/paper/text"),
               ("GET", "/citations"), ("GET", "/citations/"), ("POST", "/say"), ("POST", "/claim")}
DOOR_POSTS_PER_MINUTE = 6


def door_route_ok(method: str, path: str) -> bool:
    if (method, path) in DOOR_ROUTES:
        return True
    for m, p in DOOR_ROUTES:
        if p.endswith("/") and p != "/" and method == m and path.startswith(p) and "/" not in path[len(p):]:
            return True
    return False


def create_app(hall: Hall, port: int, canary_file: Path | None = None, door: dict | None = None,
               canary_allow: Path | None = None) -> FastAPI:
    """door: {"host": "<lan ip>:<port>", "debate": <id>} makes this app the AUDIENCE DOOR for that debate."""
    app = FastAPI(title="Debate Hall", docs_url=None, redoc_url=None, openapi_url=None)
    own = {door["host"]} if door else {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in own}
    posted: dict[int, list[float]] = {}

    @app.middleware("http")
    async def _local_only(request: Request, call_next):
        if request.headers.get("host", "") not in own:
            return PlainTextResponse("wrong host", status_code=421)
        origin = request.headers.get("origin")
        if origin is not None and origin not in origins:
            return PlainTextResponse("cross-site requests are refused", status_code=403)
        if door and not door_route_ok(request.method, request.url.path):
            return PlainTextResponse("not here: this door is for the audience", status_code=404)
        return await call_next(request)

    @app.exception_handler(HallError)
    async def _hall_error(_: Request, exc: HallError):
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=STATUS.get(exc.code, 400))

    def seat(request: Request, authorization: str | None = Header(default=None),
             x_debate: str | None = Header(default=None)):
        token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
        # Which debate (11-07: one sign-in may sit in several): the door's own; else the X-Debate header or
        # ?debate=; else the holder's only open seat.
        want = door["debate"] if door else (x_debate or request.query_params.get("debate"))
        try:
            debate = int(want) if want not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "debate is a number") from None
        s = hall.authenticate(token, debate)
        if s is None:
            raise HTTPException(401, "a sign-in is needed (Authorization: Bearer <token>), with a seat in this debate")
        # A closed debate takes no more writes, whatever the sign-in (security review, 11-07: the old seat token's expiry did
        # this for free).
        if request.method not in ("GET", "HEAD") and hall.debate(s["debate_id"])["status"] == "closed":
            raise HTTPException(403, "this debate has closed: it can be read, not changed")
        if door:
            # Checked on every request, so a seat the judge moves out of the audience is refused here at once.
            if s["role"] != "audience" or s["debate_id"] != door["debate"]:
                raise HTTPException(403, "this door is for the audience; use the hall on its own computer")
            if hall.debate(door["debate"])["status"] == "closed":
                raise HTTPException(403, "the audience door is closed: the debate has ended")
        return s

    # ----------------------------------------------------------------- the keeper (as AI-Beach's, 11-07)
    def keeper(authorization: str | None = Header(default=None)):
        token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
        k = hall.keeper_for(token)
        if k is None:
            raise HTTPException(403, "this is the keeper's; sign in with the keeper's code (`hall keeper`)")
        return k

    def invite_out(code: str, did: int | None) -> dict:
        link = f"/#c={code}" + (f"&d={did}" if did else "")
        return {"code": code, "link": link}

    @app.get("/keeper", include_in_schema=False)
    def keeper_page():
        return FileResponse(WEB / "keeper.html", headers=PAGE_HEADERS)

    @app.get("/admin/overview")
    def admin_overview(k=Depends(keeper)):
        return {"me": {"id": k["id"], "name": k["name"]}, **hall.overview()}

    @app.post("/admin/debates")
    def admin_debate(body: dict = Body(...), k=Depends(keeper)):
        did = hall.create_debate(str(body.get("title", "")))
        if body.get("paper"):
            hall.point_at_paper(did, str(body["paper"]))
        if body.get("roles_off"):
            hall.set_roles_off(did, body["roles_off"])
        return {"id": did}

    @app.post("/admin/debates/{did}/paper")
    def admin_paper(did: int, body: dict = Body(...), k=Depends(keeper)):
        return hall.point_at_paper(did, str(body.get("paper", "")))

    @app.post("/admin/debates/{did}/roles-off")
    def admin_roles_off(did: int, body: dict = Body(...), k=Depends(keeper)):
        hall.debate(did)
        return {"roles_off": hall.set_roles_off(did, body.get("roles") or [])}

    @app.post("/admin/debates/{did}/seats")
    def admin_seat(did: int, body: dict = Body(...), k=Depends(keeper)):
        """Seat someone: an existing participant picked by id, or someone new (security review: never matched by name).
        Returns a one-time claim code for that seat, never a token."""
        pid = body.get("participant")
        if pid is not None:
            sid, _ = hall.add_seat(did, "", str(body.get("role", "")), participant=int(pid), sign_in=False)
        else:
            if body.get("access", "resident") == "visitor":
                p = hall.add_participant(str(body.get("name", "")), str(body.get("kind", "")), access="visitor")
                sid, _ = hall.add_seat(did, "", str(body.get("role", "")), participant=p, sign_in=False)
            else:
                sid, _ = hall.add_seat(did, str(body.get("name", "")), str(body.get("role", "")),
                                       str(body.get("kind", "agent")), sign_in=False)
        s = hall.db.one("SELECT * FROM seats WHERE id = ?", (sid,))
        return {"seat": sid, **invite_out(hall.new_invite(s["participant_id"], did), did)}

    @app.post("/admin/seats/{sid}/revoke")
    def admin_revoke_seat(sid: int, k=Depends(keeper)):
        s = hall.db.one("SELECT * FROM seats WHERE id = ?", (sid,))
        if s is None:
            raise HTTPException(404, "no such seat")
        hall.db.run("UPDATE seats SET revoked_at = ? WHERE id = ?", (hall.now(), sid))
        return {"revoked": sid}

    @app.post("/admin/people/{pid}/invite")
    def admin_invite(pid: int, body: dict = Body(default={}), k=Depends(keeper)):
        """Another device for someone. A visitor's invite is always for one debate."""
        p = hall.db.one("SELECT * FROM participants WHERE id = ? AND removed_at IS NULL", (pid,))
        if p is None:
            raise HTTPException(404, "no such participant")
        did = body.get("debate")
        if p["access"] == "visitor" and not did:
            raise HTTPException(400, "a visitor's invite is for one debate: say which")
        if did is not None:
            hall.debate(int(did))
        return invite_out(hall.new_invite(pid, int(did) if did else None), int(did) if did else None)

    @app.post("/admin/sign-ins/{sid}/revoke")
    def admin_revoke_sign_in(sid: int, k=Depends(keeper)):
        return hall.revoke_sign_in(sid)

    @app.post("/claim")
    def claim(body: dict = Body(...)):
        """Swap a one-time claim code for a seat's token (a person never receives the token itself)."""
        return hall.claim(str(body.get("code", "")), audience_of=door["debate"] if door else None)

    # ----------------------------------------------------------------- the page
    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(WEB / "index.html", media_type="text/html", headers=PAGE_HEADERS)

    @app.get("/web/{name}", include_in_schema=False)
    def web_file(name: str):
        path = (WEB / name).resolve()
        if path.parent != WEB or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers=PAGE_HEADERS)

    # ----------------------------------------------------------------- reading
    @app.get("/me")
    def me(s=Depends(seat)):
        d = hall.debate(s["debate_id"])
        return {"seat": {"id": s["id"], "name": s["name"], "role": s["role"], "kind": s["kind"]},
                "debate": {"id": d["id"], "title": d["title"]}, "floor": hall.floor_state(d["id"]),
                "who_may_speak": hall.who_may_speak(d["id"]),
                "seats": hall.seats(d["id"]), "rooms": [r for r in ROOMS if hall.can_read(s, r)]}

    @app.get("/status")
    def status(s=Depends(seat)):
        return hall.status(s)

    @app.post("/lookup")
    def lookup(body: dict = Body(...), s=Depends(seat)):
        return hall.lookup(s, str(body.get("question", "")))

    def page(s, after: int, limit: int) -> dict:
        # "more": lines are waiting past this page, so a reader can't mistake one page for everything (the keeper,
        # 10-25: truncation is a real worry when agents evaluate, and they tend to report as if they read it all).
        limit = max(1, min(limit, 500))
        items = hall.feed(s, after, limit + 1)
        return {"items": items[:limit], "more": len(items) > limit, "floor": hall.floor_state(s["debate_id"])}

    @app.get("/feed")
    def feed(after: int = 0, limit: int = 200, s=Depends(seat)):
        return page(s, after, limit)

    @app.get("/wait")
    async def wait(after: int = 0, timeout: float = WAIT_MAX_S, s=Depends(seat)):
        """Hold until there is something new to read (or the timeout), then answer like /feed."""
        end = time.monotonic() + max(0.0, min(timeout, WAIT_MAX_S))
        while True:
            r = page(s, after, 200)
            if r["items"] or time.monotonic() >= end:
                return r
            await asyncio.sleep(0.5)

    @app.get("/attention")
    def attention(after: int = 0, s=Depends(seat)):
        """What is asking for this seat: the floor given to it, an objection for it to answer, the chair naming
        it, a ruling on its objection. The wake mod polls this; it never quotes anyone's words."""
        now = hall.now()
        if s["last_check"] is None or now - s["last_check"] > 30:      # who's listening, for the chair (10-30)
            hall.db.run("UPDATE seats SET last_check = ? WHERE id = ?", (now, s["id"]))
        return {"items": hall.attention(s, after), "floor": hall.floor_state(s["debate_id"])}

    @app.post("/wake")
    def wake(body: dict = Body(...), s=Depends(seat)):
        return hall.set_wake(s, bool(body.get("on")))

    @app.post("/draft-now")
    def draft_now(body: dict = Body(default={}), s=Depends(seat)):
        return hall.draft_now(s, body.get("objection"))

    @app.post("/huddle/call")
    def huddle_call(body: dict = Body(default={}), s=Depends(seat)):
        return hall.call_huddle(s, body.get("minutes", 10))

    @app.post("/huddle/end")
    def huddle_end(s=Depends(seat)):
        return hall.end_huddle(s)

    @app.post("/huddle/done")
    def huddle_done(s=Depends(seat)):
        return hall.huddle_done(s)

    @app.get("/ledger")
    def ledger(s=Depends(seat)):
        return {"objections": hall.ledger(s["debate_id"])}

    @app.get("/paper")
    def paper(s=Depends(seat)):
        got = hall.paper_text(s["debate_id"])
        if got is None:
            return {"paper": None}
        html, headings = render_paper(got[0], hall.docs_dir / "figures")
        return {"paper": hall.debate(s["debate_id"])["paper_path"], "sha": got[1], "html": html,
                "headings": headings}

    # ----------------------------------------------------------------- speaking and the floor
    @app.post("/say")
    def say(body: dict = Body(...), s=Depends(seat)):
        room = str(body.get("room", "hall"))
        if door:
            if room != "gallery":
                raise HTTPException(403, "from this door you post in the Gallery")
            now = time.monotonic()
            recent = [t for t in posted.get(s["id"], []) if now - t < 60]
            if len(recent) >= DOOR_POSTS_PER_MINUTE:
                raise HTTPException(429, f"at most {DOOR_POSTS_PER_MINUTE} Gallery posts a minute from this door")
            posted[s["id"]] = recent + [now]
        return hall.post(s, room, str(body.get("text", "")))

    @app.get("/paper/text")
    def paper_text(section: str | None = None, s=Depends(seat)):
        """The paper as text (whole, or one section), for a reader that can't open the hall's own file."""
        return hall.paper_section(s["debate_id"], section)

    @app.get("/citations")
    def citations(s=Depends(seat)):
        return {"citations": hall.citations()}

    @app.get("/citations/{name}")
    def citation(name: str, s=Depends(seat)):
        return hall.citation(name)

    @app.post("/seats/revoke")
    def revoke(body: dict = Body(...), s=Depends(seat)):
        return hall.revoke(s, str(body.get("name", "")))

    @app.post("/floor/give")
    def give(body: dict = Body(...), s=Depends(seat)):
        minutes = body.get("minutes")
        return hall.give_floor(s, str(body.get("name", "")), float(minutes) if minutes not in (None, "") else None)

    @app.post("/floor/take")
    def take(s=Depends(seat)):
        return hall.take_floor(s)

    @app.post("/floor/yield")
    def yield_(s=Depends(seat)):
        return hall.yield_floor(s)

    @app.post("/floor/request")
    def request_floor(s=Depends(seat)):
        return hall.request_floor(s)

    @app.post("/floor/withdraw")
    def withdraw_request(s=Depends(seat)):
        return hall.withdraw_request(s)

    @app.post("/phase")
    def phase(body: dict = Body(...), s=Depends(seat)):
        return hall.set_phase(s, str(body.get("phase", "")))

    @app.post("/close")
    def close(s=Depends(seat)):
        return hall.close_debate(s)

    @app.post("/nudge")
    def nudge(s=Depends(seat)):
        return hall.nudge_moderator(s)

    @app.post("/ping-judge")
    def ping_judge(body: dict = Body(...), s=Depends(seat)):
        return hall.ping_judge(s, str(body.get("note", "")))

    @app.post("/pacing")
    def pacing(body: dict = Body(...), s=Depends(seat)):
        return hall.set_paced(s, bool(body.get("on")))

    @app.post("/relay")
    def relay(body: dict = Body(...), s=Depends(seat)):
        return hall.relay(s, int(body.get("id", 0)))

    @app.post("/seats/role")
    def set_role(body: dict = Body(...), s=Depends(seat)):
        return hall.set_role(s, str(body.get("name", "")), str(body.get("role", "")))

    @app.post("/paper")
    def set_paper(body: dict = Body(...), s=Depends(seat)):
        return hall.set_paper(s, str(body.get("path", "")))

    @app.post("/show")
    def show(body: dict = Body(...), s=Depends(seat)):
        return hall.show(s, str(body.get("target", "")))

    # ----------------------------------------------------------------- the ledger
    @app.post("/objections")
    def obj(body: dict = Body(...), s=Depends(seat)):
        return hall.object(s, str(body.get("anchor", "")), str(body.get("claim", "")), str(body.get("why", "")),
                           body.get("quote"), body.get("depends_on"))

    @app.post("/objections/{oid}/reply")
    def reply(oid: int, body: dict = Body(...), s=Depends(seat)):
        return hall.reply(s, oid, str(body.get("text", "")))

    @app.post("/objections/{oid}/link")
    def link(oid: int, body: dict = Body(...), s=Depends(seat)):
        try:
            to = int(body.get("depends_on"))
        except (TypeError, ValueError):
            raise HTTPException(400, "depends_on is an objection number") from None
        return hall.link(s, oid, to, remove=body.get("remove") is True)

    @app.post("/objections/{oid}/answer")
    def answer(oid: int, body: dict = Body(...), s=Depends(seat)):
        mode = "concede" if body.get("concede") is True else str(body.get("mode", "answer"))
        return hall.answer(s, oid, str(body.get("text", "")), mode, body.get("proposed"), body.get("evidence"))

    @app.post("/objections/{oid}/draft")
    def draft(oid: int, body: dict = Body(...), s=Depends(seat)):
        return hall.draft(s, oid, str(body.get("verdict", "")), str(body.get("text", "")))

    @app.post("/objections/{oid}/rule")
    def rule(oid: int, body: dict = Body(...), s=Depends(seat)):
        return hall.rule(s, oid, str(body.get("verdict", "")), str(body.get("text", "")))

    # ----------------------------------------------------------------- exports (read-only)
    # Every export is checked against the household-words list first (an advocate, 10-25: the revision list travels, and
    # the huddles open later). A hit refuses the export and says how many, never which words.
    def checked(text: str, did: int) -> Response:
        text = by_role(hall, did, text)
        canaries = load_canaries(canary_file, canary_allow)
        # the keeper, 10-30: export anyway, and say on the FIRST line which household terms are in it, so the reader can
        # search for them right there and deal with each as they like (the export used to refuse and name nothing).
        found = [term for term, cs in canaries
                 if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, 0 if cs else re.IGNORECASE)]
        head = (f"<!-- CANARY CHECK: {len(found)} household term(s) found: {', '.join(found)}. Search for each "
                "below and deal with it before sharing this file. -->\n\n") if found else ""
        note = "" if found else (f"\n\n<!-- canary check: passed ({len(canaries)} terms) -->\n" if canaries else
                                 "\n\n<!-- canary check: no list configured (data/canaries.txt) -->\n")
        return Response(head + text + note, media_type="text/markdown; charset=utf-8")

    @app.get("/export/ledger.md")
    def export_ledger(s=Depends(seat)):
        return checked(ledger_md(hall, s["debate_id"]), s["debate_id"])

    @app.get("/export/transcript.md")
    def export_transcript(s=Depends(seat)):
        return checked(transcript_md(hall, s), s["debate_id"])

    return app


def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def ledger_md(hall: Hall, did: int) -> str:
    """The revision list: objections that need a revision first, then the rest, each with its passage, its
    answers and the rulings."""
    d = hall.debate(did)
    order = {"revise": 0, "sustained": 1, "conceded": 2, "partial": 3, "open": 4, "answered": 5, "overruled": 6}
    rows = sorted(hall.ledger(did), key=lambda o: (order.get(o["status"], 9), o["id"]))
    out = [f"# Objection ledger: {d['title']}", "", f"Paper version: {(d['paper_sha'] or 'none')[:12]}", ""]
    for o in rows:
        out += [f"## #{o['id']} [{o['status']}] at {o['anchor']} ({o['who']})", "",
                f"**Claim attacked:** {o['claim']}", "", f"**Why:** {o['why']}", ""]
        if o["quote"]:
            out += [f"> {o['quote']}", ""]
        if o["depends_on"] or o["needed_by"]:
            parts = ([f"depends on {', '.join('#' + str(n) for n in o['depends_on'])}"] if o["depends_on"] else []) + \
                    ([f"needed by {', '.join('#' + str(n) for n in o['needed_by'])}"] if o["needed_by"] else [])
            out += [f"**Linked:** {'; '.join(parts)}", ""]
        # The latest proposed wording is the current one; earlier ones are kept but marked (an advocate, a test debate:
        # three improving answers on one objection left the reader to work out which text stood).
        proposals = [i for i, e in enumerate(o["events"]) if e.get("proposed") and e["kind"] != "ruling"]
        current = proposals[-1] if proposals else None
        agreed = [e["proposed"] for e in o["events"] if e["kind"] == "ruling" and e["verdict"] == "revise" and e.get("proposed")]
        if agreed and o["status"] == "revise":
            out += [f"**Agreed revision text:** {agreed[-1]}", ""]
        elif current is not None:
            out += [f"**Current proposed text:** {o['events'][current]['proposed']}", ""]
        for i, e in enumerate(o["events"]):
            verdict = f" ({e['verdict']})" if e["verdict"] else ""
            if e["kind"] == "ruling" and e["verdict"] in ("revise", "sustained"):
                out += [f"- {e['who']}, ruling{verdict}. **What the revision must do:** {e['text']}"]
                if e["verdict"] == "revise" and e.get("proposed"):
                    out += [f"  - **Agreed revision text:** {e['proposed']}"]
                continue
            else:
                out += [f"- {e['who']}, {e['kind']}{verdict}: {e['text']}"]
            if e.get("proposed"):
                mark = "Proposed text" if i == current else "Proposed text (superseded)"
                out += [f"  - **{mark}:** {e['proposed']}" if i == current else f"  - {mark}: ~~{e['proposed']}~~"]
            for ev in e.get("evidence") or []:
                out += [f"  - Evidence: `{ev['path']}` @ `{ev['sha']}`" + (f": {ev['note']}" if ev["note"] else "")]
        out.append("")
    return "\n".join(out)


def transcript_md(hall: Hall, seat) -> str:
    d = hall.debate(seat["debate_id"])
    out = [f"# Transcript: {d['title']}", ""]
    after = 0
    while True:
        page = hall.feed(seat, after, 500)
        if not page:
            break
        # The judge's talk with their aide is theirs alone: never in an export, which travels.
        out += [f"- {_when(m['at'])} [{m['room']}] {m['who']}: {m['text']}" for m in page if m["room"] != "aide"]
        after = page[-1]["id"]
    return "\n".join(out) + "\n"
