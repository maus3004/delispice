"""Scouting shortlist store — append-only, versioned, multi-author, SQLite.

Model
  * An author is a whitelisted account (``authors`` table: initials + display name + PBKDF2
    password hash). Authors are managed from the command line, never from the web UI:
        python -m delispice_app.scouting add MB "Matt Baek" --admin
        python -m delispice_app.scouting passwd MB
        python -m delispice_app.scouting deactivate MB   (and: activate, list)
  * A report THREAD is one author's living report on one player — keyed (player_key, author).
    Every save (edits and deletes included) INSERTs a new version row; nothing is UPDATEd.
    The shortlist shows each thread's newest version grouped by player; history stays browsable.
    "Created" = the thread's first version, "Edited" = its newest.
  * Deleting tombstones ONE thread (the author's own report). The player leaves the shortlist
    only when no live threads remain. Prior versions stay readable in the DB.

The DB lives in ``.data/`` (NOT ``.cache/``) — caches are derived and rebuildable, whereas these
reports are the one thing in this app that cannot be regenerated. Back it up; see deploy/DEPLOY.md.

Writes require a valid author login, verified server-side on every write (a client-side "signed
in" flag can be forged; ``verify_author`` is the actual gate). No authors registered -> no writes.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / ".data" / "scouting.db"

HITTER_GRADES = ["Hit", "Power", "Run", "Field", "Arm"]
PITCHER_BASE_GRADES = ["Control"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
  id           INTEGER PRIMARY KEY,
  initials     TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name TEXT NOT NULL,
  pw_hash      TEXT NOT NULL,
  salt         TEXT NOT NULL,
  is_admin     INTEGER NOT NULL DEFAULT 0,
  active       INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_versions (
  id          INTEGER PRIMARY KEY,
  player_key  TEXT NOT NULL,
  player_name TEXT NOT NULL,
  player_id   TEXT,
  role        TEXT NOT NULL,
  school      TEXT,
  position    TEXT,
  ovr         INTEGER,
  author      TEXT,
  author_id   INTEGER,
  body        TEXT,
  grades      TEXT,
  deleted     INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  report_date TEXT
);
CREATE INDEX IF NOT EXISTS ix_rv_thread ON report_versions(player_key, author_id, created_at DESC, id DESC);
"""


def _conn() -> sqlite3.Connection:
    """A fresh connection per operation — cheap, and safe across gunicorn's worker threads."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")      # readers are never blocked by a writer
    con.execute("PRAGMA busy_timeout=5000")     # a rare write collision waits instead of erroring
    return con


def init_db() -> None:
    with _conn() as con:
        con.executescript(_SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(report_versions)")}
        for col, ddl in (("report_date", "ALTER TABLE report_versions ADD COLUMN report_date TEXT"),
                         ("author_id", "ALTER TABLE report_versions ADD COLUMN author_id INTEGER")):
            if col not in cols:                 # migrate DBs created before these columns existed
                con.execute(ddl)


def player_key(player_id: str | None, player_name: str | None) -> str:
    """Stable grouping key across versions: the TrackMan id when the report is linked to a real
    player, else a slug of the typed name (so free-text reports still version together)."""
    if player_id:
        return f"id:{player_id}"
    slug = re.sub(r"[^a-z0-9]+", "-", (player_name or "").strip().lower()).strip("-")
    return f"name:{slug}"


# ── Authors (the whitelist) ──────────────────────────────────────────────────────────────────────
def _hash_pw(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000).hex()


def add_author(initials: str, display_name: str, password: str, is_admin: bool = False) -> None:
    salt = secrets.token_hex(16)
    with _conn() as con:
        con.execute("INSERT INTO authors (initials, display_name, pw_hash, salt, is_admin, active, "
                    "created_at) VALUES (?,?,?,?,?,1,?)",
                    (initials.strip(), display_name.strip(), _hash_pw(password, salt), salt,
                     int(is_admin), _now()))


def set_password(initials: str, password: str) -> bool:
    salt = secrets.token_hex(16)
    with _conn() as con:
        cur = con.execute("UPDATE authors SET pw_hash=?, salt=? WHERE initials=?",
                          (_hash_pw(password, salt), salt, initials.strip()))
    return cur.rowcount > 0


def set_active(initials: str, active: bool) -> bool:
    with _conn() as con:
        cur = con.execute("UPDATE authors SET active=? WHERE initials=?", (int(active), initials.strip()))
    return cur.rowcount > 0


def list_authors() -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, initials, display_name, is_admin, active, created_at FROM authors "
            "ORDER BY initials").fetchall()]


def verify_author(initials: str | None, password: str | None) -> dict | None:
    """The real write gate — called server-side on EVERY write (and sign-in). Returns the author
    row when the credentials check out against an active whitelist entry, else None."""
    if not initials or not password:
        return None
    with _conn() as con:
        r = con.execute("SELECT * FROM authors WHERE initials=? AND active=1",
                        (str(initials).strip(),)).fetchone()
    if r is None:
        return None
    ok = hmac.compare_digest(_hash_pw(str(password), r["salt"]), r["pw_hash"])
    return {"id": r["id"], "initials": r["initials"], "display_name": r["display_name"],
            "is_admin": bool(r["is_admin"])} if ok else None


def any_authors() -> bool:
    with _conn() as con:
        return con.execute("SELECT 1 FROM authors WHERE active=1 LIMIT 1").fetchone() is not None


# ── Writes (always an INSERT) ────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_version(*, player_name, player_id=None, role, school=None, position=None, ovr=None,
                 author=None, author_id=None, body=None, grades=None, report_date=None) -> str:
    """Append a new version in the (player, author) thread. ``author`` is the initials snapshot
    for display; ``author_id`` is the verified identity that defines the thread. ``report_date``
    is the user-entered date; ``created_at`` stays the server insert time and orders versions."""
    key = player_key(player_id, player_name)
    with _conn() as con:
        con.execute(
            "INSERT INTO report_versions (player_key, player_name, player_id, role, school, "
            "position, ovr, author, author_id, body, grades, report_date, deleted, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (key, player_name, player_id, role, school, position, ovr, author, author_id, body,
             json.dumps(grades or {}), report_date, _now()))
    return key


def delete_thread(key: str, thread: str) -> bool:
    """Tombstone ONE author's report on a player. Permission is the caller's job (own thread, or
    admin). The tombstone row carries the thread's identity so it lands in the same partition."""
    with _conn() as con:
        last = None
        for row in con.execute("SELECT * FROM report_versions WHERE player_key=? "
                               "ORDER BY created_at DESC, id DESC", (key,)):
            if _thread_token(row["author_id"], row["author"]) == thread:
                last = row
                break
        if last is None:
            return False
        con.execute(
            "INSERT INTO report_versions (player_key, player_name, player_id, role, author, "
            "author_id, deleted, created_at) VALUES (?,?,?,?,?,?,1,?)",
            (key, last["player_name"], last["player_id"], last["role"], last["author"],
             last["author_id"], _now()))
    return True


# ── Reads ────────────────────────────────────────────────────────────────────────────────────────
def _thread_token(author_id, author) -> str:
    """A report thread's identity: the verified author id, or a legacy tag for old rows saved
    before per-author threads existed."""
    return f"a{author_id}" if author_id is not None else f"legacy:{author or ''}"


_THREAD_SQL = "COALESCE('a'||author_id, 'legacy:'||COALESCE(author,''))"


def _rows(sql: str, params=()) -> list[dict]:
    with _conn() as con:
        out = []
        for r in con.execute(sql, params).fetchall():
            d = dict(r)
            d["grades"] = json.loads(d["grades"]) if d.get("grades") else {}
            out.append(d)
        return out


_LATEST_THREADS = f"""
    SELECT t.*, a.display_name AS author_name FROM (
      SELECT *, {_THREAD_SQL} AS thread,
             ROW_NUMBER() OVER (PARTITION BY player_key, {_THREAD_SQL}
                                ORDER BY created_at DESC, id DESC) rn
      FROM report_versions) t
    LEFT JOIN authors a ON a.id = t.author_id
    WHERE t.rn = 1 AND t.deleted = 0"""


def latest_threads(role: str | None = None) -> list[dict]:
    """The newest version of every live (player, author) thread."""
    sql, params = _LATEST_THREADS, ()
    if role in ("pitcher", "batter"):
        sql, params = sql + " AND t.role = ?", (role,)
    return _rows(sql + " ORDER BY t.created_at DESC", params)


def players(role: str | None = None, author: str | None = None) -> list[dict]:
    """Shortlist entries: one per player, carrying every author's live report on them.
    ``author`` filters to players that author has written up (matched on initials).
    ``ovr_label`` shows the range when authors disagree; ``ovr_max`` is the sort key."""
    threads = latest_threads(role)
    if author:
        threads = [t for t in threads if (t.get("author") or "").strip().lower() == author.strip().lower()]
    by: dict[str, dict] = {}
    for t in threads:                                # newest-first, so first seen = representative
        e = by.setdefault(t["player_key"], {
            "player_key": t["player_key"], "player_name": (t["player_name"] or "").strip(),
            "role": t["role"], "school": t["school"], "position": t["position"], "reports": []})
        e["reports"].append(t)
    out = []
    for e in by.values():
        ovrs = [r["ovr"] for r in e["reports"] if r["ovr"] is not None]
        e["ovr_max"] = max(ovrs) if ovrs else None
        e["ovr_label"] = ("" if not ovrs else str(ovrs[0]) if len(set(ovrs)) == 1
                          else f"{min(ovrs)}–{max(ovrs)}")
        e["authors_label"] = ", ".join((r.get("author_name") or r.get("author") or "—")
                                       for r in e["reports"])
        out.append(e)
    return out


def report_authors() -> list[dict]:
    """Authors with at least one live report, for the filter dropdown: ``[{initials, name}]``
    (full name shown, initials used as the filter value)."""
    seen = {}
    for t in latest_threads():
        ini = (t.get("author") or "").strip()
        if ini:
            seen[ini] = t.get("author_name") or ini
    return sorted(({"initials": k, "name": v} for k, v in seen.items()), key=lambda d: d["name"].lower())


def versions_for_thread(key: str, thread: str) -> list[dict]:
    """Every readable version of one author's report on one player, newest first."""
    if thread.startswith("a") and thread[1:].isdigit():
        where, params = "author_id = ?", (key, int(thread[1:]))
    else:
        where, params = "author_id IS NULL AND COALESCE(author,'') = ?", (key, thread[len("legacy:"):])
    return _rows(f"SELECT * FROM report_versions WHERE player_key=? AND {where} AND deleted=0 "
                 "ORDER BY created_at DESC, id DESC", params)


# ── Presentation helpers ─────────────────────────────────────────────────────────────────────────
def grades_line(role: str, grades: dict) -> str:
    """'Control 30 | Fastball 45 | Slider 40'  ·  'Hit 45 | Power 50 | Run 40 | ...'"""
    if not grades:
        return ""
    bits = []
    if role == "pitcher":
        if grades.get("Control") is not None:
            bits.append(f"Control {grades['Control']}")
        for p in grades.get("pitches") or []:
            if p.get("name"):
                bits.append(f"{p['name']} {p.get('grade', '')}".strip())
    else:
        bits = [f"{g} {grades[g]}" for g in HITTER_GRADES if grades.get(g) is not None]
    return "  |  ".join(bits)


def stamp(iso: str) -> str:
    """'2026-07-11T18:04:00+00:00' -> 'Jul 11, 2026 · 18:04'"""
    try:
        return datetime.fromisoformat(iso).strftime("%b %-d, %Y · %H:%M")
    except Exception:
        return iso or ""


def fmt_date(d: str | None) -> str:
    """User-entered date '2026-07-18' -> 'Jul 18, 2026'. Blank/None -> ''."""
    if not d:
        return ""
    try:
        return datetime.fromisoformat(d).strftime("%b %-d, %Y")
    except Exception:
        return d


# ── Author-management CLI ────────────────────────────────────────────────────────────────────────
def _prompt_pw() -> str:
    while True:
        pw = getpass.getpass("Password: ")
        if pw and pw == getpass.getpass("Repeat  : "):
            return pw
        print("Passwords empty or mismatched — try again.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m delispice_app.scouting",
                                description="Manage the scouting-report author whitelist.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="add an author (prompts for password)")
    a.add_argument("initials"); a.add_argument("display_name")
    a.add_argument("--admin", action="store_true", help="can remove any author's reports")
    pw = sub.add_parser("passwd", help="reset an author's password"); pw.add_argument("initials")
    d = sub.add_parser("deactivate", help="revoke an author's access"); d.add_argument("initials")
    ac = sub.add_parser("activate", help="restore an author's access"); ac.add_argument("initials")
    sub.add_parser("list", help="list authors")
    args = p.parse_args(argv)
    init_db()
    if args.cmd == "add":
        add_author(args.initials, args.display_name, _prompt_pw(), is_admin=args.admin)
        print(f"added {args.initials} ({args.display_name}){' · admin' if args.admin else ''}")
    elif args.cmd == "passwd":
        print("updated" if set_password(args.initials, _prompt_pw()) else "no such author")
    elif args.cmd in ("deactivate", "activate"):
        ok = set_active(args.initials, args.cmd == "activate")
        print(f"{args.cmd}d" if ok else "no such author")
    else:
        for r in list_authors():
            flags = ("admin " if r["is_admin"] else "") + ("" if r["active"] else "INACTIVE")
            print(f"{r['initials']:6s} {r['display_name']:24s} {flags}")


if __name__ == "__main__":
    main()
