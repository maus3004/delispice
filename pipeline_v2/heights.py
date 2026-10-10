"""Baseball Reference heights + birthdays -> heights.csv, as an always-on service (plan.md §8).

    python -m pipeline_v2.heights              # run forever (the systemd service: deploy/delispice-heights.service)
    python -m pipeline_v2.heights --once       # one pass over the queue, then exit
    python -m pipeline_v2.heights --dry-run    # queue size + ETA, no requests
    python -m pipeline_v2.heights --once --limit 3   # a quick live test

Ported from data_pipeline/height_scraper.py: matching, parsing and the file format are unchanged.
What's new: players come from serving/ (verified games only, so names that verification later
corrects aren't scraped), and instead of 250 players a night it works through the whole queue at one
request every HEIGHTS_RATE_S, then sleeps HEIGHTS_IDLE_S and looks again.

Queue: players not in heights.csv yet (latest season first, D1 first within a season, then most
recently seen), then players BR didn't match (not_found / ambiguous / no_height) whose last try is
older than HEIGHTS_RETRY_DAYS. Rows typed into the app (Status 'manual') are never re-scraped.
heights.csv is append-only, one flushed row per player; the last row for a (Name, TrackManId)
wins, here and in the app.

Matching: search.fcgi for "First Last". A unique hit 302s straight to the player page. A results page
is filtered to exact name matches whose BR active years overlap the seasons we saw the player (+/- 1
year); if several survive, up to 3 candidate pages are checked for the player's school (TrackMan
team acronym -> school via team_acronyms.csv). Every player page also yields the birthday.

It never crashes: a server error or timeout skips the player for this pass and backs off (1 min,
doubling up to 30); three straight 403/429s mean BR is refusing us (it bans IPs above ~20
requests/min), so it posts to Discord and waits HEIGHTS_BLOCK_S; anything unexpected is logged and
retried after 10 minutes. systemd restarts the process if it dies anyway. What it's doing is kept
in HEIGHTS_STATE for `python -m pipeline_v2 status`.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin

import polars as pl
import requests

from pipeline_v2 import config, notify

log = logging.getLogger("heights")

SEARCH_URL = "https://www.baseball-reference.com/search/search.fcgi?search="
BR_ROOT = "https://www.baseball-reference.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# heights.csv columns. delispice_app/data.py writes manual rows with the same order (aligned to the
# file's own header), so keep the two in sync.
FIELDS = ["Name", "TrackManId", "HeightIn", "Height", "WeightLb", "BirthDate",
          "Status", "BRUrl", "ScrapedAt"]
RETRY_STATUSES = {"not_found", "ambiguous", "no_height"}
YEAR_TOLERANCE = 1          # BR active years may lag/lead our sightings by a season
MAX_CANDIDATE_FETCHES = 3   # page fetches spent disambiguating one shared name
ERROR_ALERT_AFTER = 10      # straight server errors / timeouts before a Discord message

# names worth scraping: "Last, First" with letters on both sides (drops "1, Batter" test rows etc.)
_NAME_OK = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .\-]*,\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .\-]*")
# "<span>6-2</span>,&nbsp;<span>202lb</span>" -- identical on /players/ and /register/ pages
_HT_WT = re.compile(r"<span>(\d)-(\d{1,2})</span>,&nbsp;<span>(\d+)lb</span>")
_HT_CM = re.compile(r"\((\d{2,3})cm")
# "Born: April 7, 2005", matched against the tag-stripped meta block
_BORN = re.compile(r"Born:\s*([A-Z][a-z]+\.?\s+\d{1,2}\s*,\s*\d{4})")
_SEARCH_ITEM = re.compile(r'<div class="search-item-name">\s*<a href="([^"]+)">([^<]+)', re.S)
_YEARS = re.compile(r"\((\d{4})(?:-(\d{4}))?\)")
_SCHOOL = re.compile(r"<strong>School:</strong>\s*<a[^>]*>([^<(]+)")


class Blocked(Exception):
    """BR is refusing us (403/429): stop before the ban gets longer."""


class Transient(Exception):
    """A server error or timeout: skip this player for now, he stays in the queue."""


class Missing(Exception):
    """HTTP 404 for a page we were sent to."""


# ── HTTP: one throttled session, small caches for one pass ───────────────────────────────────────
class BRClient:
    def __init__(self, rate: float):
        self.rate = rate
        self.sess = requests.Session()
        self.sess.headers["User-Agent"] = UA
        self._last = 0.0
        self._blocks = 0
        self.requests_made = 0
        self._search_cache: dict[str, requests.Response] = {}
        self._page_cache: dict[str, str] = {}

    def _get(self, url: str, _depth: int = 0) -> requests.Response:
        wait = self._last + self.rate * random.uniform(0.85, 1.2) - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        self.requests_made += 1
        try:   # redirects followed by hand so every server hit is throttled
            r = self.sess.get(url, timeout=30, allow_redirects=False)
        except requests.RequestException as e:
            raise Transient(f"{type(e).__name__} at {url}") from None
        if r.is_redirect and _depth < 5:
            return self._get(urljoin(url, r.headers["Location"]), _depth + 1)
        if r.status_code in (403, 429):
            self._blocks += 1
            if self._blocks >= 3:
                raise Blocked(f"HTTP {r.status_code} x{self._blocks} at {url}")
            log.warning("HTTP %d from BR; backing off 90 s", r.status_code)
            time.sleep(90)
            return self._get(url)
        if r.status_code == 404:
            raise Missing(url)
        if r.status_code >= 400:
            raise Transient(f"HTTP {r.status_code} at {url}")
        self._blocks = 0
        return r

    def search(self, name: str) -> requests.Response:
        if name not in self._search_cache:
            self._search_cache[name] = self._get(SEARCH_URL + quote_plus(name))
        return self._search_cache[name]

    def page(self, url: str) -> str:
        if url not in self._page_cache:
            self._page_cache[url] = self._get(url).text
        return self._page_cache[url]


# ── Parsing ──────────────────────────────────────────────────────────────────────────────────────
def parse_height(html: str) -> tuple[str, int, int | None] | None:
    """-> ("6-2", 74, 202) from a player page, or None if no height is listed."""
    m = _HT_WT.search(html)
    if m:
        ft, inch, wt = int(m[1]), int(m[2]), int(m[3])
        return f"{ft}-{inch}", ft * 12 + inch, wt
    i = html.find("<h1>")                      # metric-only fallback, meta section only --
    m = _HT_CM.search(html[i:i + 5000]) if i >= 0 else None   # "(188cm" elsewhere means box scores
    if m:
        total = round(int(m[1]) / 2.54)
        return f"{total // 12}-{total % 12}", total, None
    return None


def parse_birthdate(html: str) -> str:
    """'Born: April 7, 2005' -> '2005-04-07', or '' (meta block only)."""
    i = html.find("<h1>")
    seg = re.sub(r"<[^>]+>", " ", html[i:i + 3000] if i >= 0 else html[:3000])
    m = _BORN.search(seg)
    if not m:
        return ""
    txt = re.sub(r"\s+", " ", m[1]).replace(" ,", ",")
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(txt, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def parse_candidates(html: str) -> list[dict]:
    """Search-results page -> [{url, name, y0, y1}] for player links only."""
    out = []
    for href, text in _SEARCH_ITEM.findall(html):
        if "/players/" not in href and "/register/player.fcgi" not in href:
            continue
        ym = _YEARS.search(text)
        out.append({"url": href if href.startswith("http") else BR_ROOT + href,
                    "name": _YEARS.sub("", text).strip(),
                    "y0": int(ym[1]) if ym else None,
                    "y1": int(ym[2] or ym[1]) if ym else None})
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace(".", "").strip()).casefold()


def search_name(tm_name: str) -> str:
    """'Blessinger, Max' -> 'Max Blessinger'."""
    last, _, first = tm_name.partition(",")
    return re.sub(r"\s+", " ", f"{first.strip()} {last.strip()}").strip()


def load_school_map() -> dict[str, str]:
    if not config.TEAM_ACRONYMS.exists():
        return {}
    with open(config.TEAM_ACRONYMS, newline="", encoding="utf-8") as f:
        return {r["acronym"]: r["school_name"] for r in csv.DictReader(f) if r.get("school_name")}


# ── One player -> one row ────────────────────────────────────────────────────────────────────────
def _row(p: dict, status: str, url: str = "", ht: tuple[str, int, int | None] | None = None,
         bday: str = "") -> dict:
    return {"Name": p["Name"], "TrackManId": p["TrackManId"],
            "HeightIn": ht[1] if ht else "", "Height": ht[0] if ht else "",
            "WeightLb": (ht[2] if ht and ht[2] is not None else ""),
            "BirthDate": bday, "Status": status, "BRUrl": url,
            "ScrapedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d")}


def _page_row(client: BRClient, p: dict, url: str, html: str | None = None) -> dict:
    html = html if html is not None else client.page(url)
    ht = parse_height(html)                            # height drives Status; the birthday is kept either way
    return _row(p, "found" if ht else "no_height", url, ht, parse_birthdate(html))


def resolve(client: BRClient, p: dict, schools: dict[str, str]) -> dict:
    try:
        resp = client.search(search_name(p["Name"]))
        if "/search/" not in resp.url:                  # unique match -> straight to the player page
            return _page_row(client, p, resp.url, resp.text)

        cands = parse_candidates(resp.text)
        want = _norm(search_name(p["Name"]))
        exact = [c for c in cands if _norm(c["name"]) == want]
        cands = exact or [c for c in cands if want in _norm(c["name"])]
        cands = [c for c in cands if c["y0"] is None or p["last_year"] is None or
                 (c["y0"] - YEAR_TOLERANCE <= p["last_year"] and c["y1"] + YEAR_TOLERANCE >= p["first_year"])]
        if not cands:
            return _row(p, "not_found")
        if len(cands) == 1:
            return _page_row(client, p, cands[0]["url"])

        my_schools = {_norm(schools[t]) for t in p["teams"] if t in schools}
        if my_schools and len(cands) <= MAX_CANDIDATE_FETCHES:
            for c in cands:
                html = client.page(c["url"])
                m = _SCHOOL.search(html)
                if m and _norm(m[1]) in my_schools:
                    return _page_row(client, p, c["url"], html)
        return _row(p, "ambiguous")
    except Missing as e:
        return _row(p, "not_found", str(e))


# ── The queue ────────────────────────────────────────────────────────────────────────────────────
def gather_players() -> pl.DataFrame:
    """Every (Name, TrackManId) in verified served games, with what resolve() needs."""
    lf = pl.scan_parquet(config.PITCHES_DIR / "**" / "*.parquet")
    if "is_verified" in lf.collect_schema().names():
        lf = lf.filter(pl.col("is_verified"))

    def side(name_c: str, id_c: str, team_c: str) -> pl.LazyFrame:
        return lf.select(pl.col(name_c).alias("Name"),
                         pl.col(id_c).cast(pl.Utf8).fill_null("").alias("TrackManId"),
                         pl.col(team_c).alias("Team"), pl.col("Level"),
                         pl.col("Date").str.slice(0, 4).cast(pl.Int32, strict=False).alias("Year"),
                         pl.col("Date").alias("LastSeen"))

    return (pl.concat([side("Batter", "BatterId", "BatterTeam"), side("Pitcher", "PitcherId", "PitcherTeam")])
              .filter(pl.col("Name").is_not_null())
              .group_by(["Name", "TrackManId"])
              .agg(pl.col("Year").min().alias("first_year"), pl.col("Year").max().alias("last_year"),
                   pl.col("Team").drop_nulls().unique().alias("teams"),
                   (pl.col("Level") == "D1").any().alias("is_d1"), pl.col("LastSeen").max().alias("last_seen"))
              .filter(pl.col("Name").str.contains(_NAME_OK.pattern))
              .sort(["last_year", "is_d1", "last_seen"], descending=True)
              .collect())


def load_table() -> dict[tuple[str, str], tuple[str, str]]:
    """(Name, TrackManId) -> (Status, ScrapedAt) of the last row for each player."""
    if not config.HEIGHTS_CSV.exists():
        return {}
    with open(config.HEIGHTS_CSV, newline="", encoding="utf-8") as f:
        return {(r["Name"], r.get("TrackManId") or ""): (r.get("Status") or "", r.get("ScrapedAt") or "")
                for r in csv.DictReader(f)}


def queue() -> tuple[list[dict], int, int]:
    """(players to scrape, how many are new, how many are retries), new players first."""
    table = load_table()
    retry_before = (date.today() - timedelta(days=config.HEIGHTS_RETRY_DAYS)).isoformat()
    new, retry = [], []
    for p in gather_players().iter_rows(named=True):
        done = table.get((p["Name"], p["TrackManId"]))
        if done is None:
            new.append(p)
        elif done[0] in RETRY_STATUSES and done[1] < retry_before:
            retry.append((done[1], p))
    retry = [p for _, p in sorted(retry, key=lambda t: t[0])]       # longest-waiting first
    return new + retry, len(new), len(retry)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── State for `status` ───────────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    try:
        return json.loads(config.HEIGHTS_STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    config.HEIGHTS_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.HEIGHTS_STATE.with_name(config.HEIGHTS_STATE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, config.HEIGHTS_STATE)


# ── The service ──────────────────────────────────────────────────────────────────────────────────
def scrape(pending: list[dict], state: dict) -> Counter:
    """One pass: resolve each player and append his row. Raises Blocked."""
    schools = load_school_map()
    client = BRClient(config.HEIGHTS_RATE_S)
    counts: Counter = Counter()
    errors = 0
    new_file = not config.HEIGHTS_CSV.exists()
    t0 = time.time()
    with open(config.HEIGHTS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for i, p in enumerate(pending, 1):
            try:
                row = resolve(client, p, schools)
            except Transient as e:
                errors += 1
                counts["error (stays queued)"] += 1
                wait = min(60 * 2 ** (errors - 1), 1800)
                log.warning("%s: %s; backing off %d s", p["Name"], e, wait)
                state["last_error"] = f"{_now()} {e}"
                if errors == ERROR_ALERT_AFTER:
                    notify.send("Heights: Baseball Reference keeps failing",
                                f"{errors} errors in a row, the last: {e}. Still retrying, with longer waits.", "warn")
                time.sleep(wait)
                continue
            errors = 0
            w.writerow(row)
            f.flush()                                    # every row is saved the moment it's scraped
            counts[row["Status"]] += 1
            if i % 25 == 0 or i == len(pending):
                per = (time.time() - t0) / i
                log.info("heights: %d/%d %s, ~%.1f h left", i, len(pending), dict(counts), per * (len(pending) - i) / 3600)
                state.update(state="scraping", done_this_pass=dict(counts), requests=client.requests_made)
                save_state(state)
    return counts


def serve(once: bool = False, dry_run: bool = False, limit: int | None = None) -> int:
    state = load_state()
    while True:
        try:
            pending, n_new, n_retry = queue()
            pending = pending[:limit] if limit else pending
            log.info("heights: %d to scrape (%d new, %d retries of misses older than %d days)",
                     len(pending), n_new, n_retry, config.HEIGHTS_RETRY_DAYS)
            if dry_run:
                eta_h = len(pending) * config.HEIGHTS_RATE_S * 1.4 / 3600   # ~1.4 requests per player
                log.info("dry run: ~%.1f h at %.1f s/request; next up: %s", eta_h, config.HEIGHTS_RATE_S,
                         [p["Name"] for p in pending[:8]])
                return 0
            state.update(queue=len(pending), queue_new=n_new, queue_retry=n_retry, pass_started=_now())
            if pending:
                state["state"] = "scraping"
                save_state(state)
                counts = scrape(pending, state)
                state.update(last_pass=dict(counts), last_pass_done=_now())
                log.info("heights: pass done %s", dict(counts))
            if once:
                state["state"] = "stopped"
                save_state(state)
                return 0
            state.update(state="idle", queue=0)
            save_state(state)
            time.sleep(config.HEIGHTS_IDLE_S)
        except Blocked as e:
            log.error("heights: BR is refusing us (%s); waiting %.0f h", e, config.HEIGHTS_BLOCK_S / 3600)
            state.update(state="blocked", last_block=_now(), last_error=str(e))
            save_state(state)
            notify.send("Heights: Baseball Reference is blocking us",
                        f"{e}. Rows so far are saved; trying again in {config.HEIGHTS_BLOCK_S / 3600:.0f} h.", "warn")
            if once:
                return 1
            time.sleep(config.HEIGHTS_BLOCK_S)
        except Exception as e:                         # never crash: log, wait, go again
            log.exception("heights: unexpected error; retrying in 10 min")
            state.update(state="error", last_error=f"{_now()} {type(e).__name__}: {e}")
            save_state(state)
            if once:
                return 1
            time.sleep(600)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Baseball Reference heights -> heights.csv (always-on service)")
    ap.add_argument("--once", action="store_true", help="one pass over the queue, then exit")
    ap.add_argument("--dry-run", action="store_true", help="queue size + ETA, no requests")
    ap.add_argument("--limit", type=int, help="scrape at most N players per pass (testing)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    return serve(once=args.once, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
