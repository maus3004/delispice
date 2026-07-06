"""Scrape Baseball Reference for player heights -> heights.csv.

Maintains a lookup table keyed by (Name, TrackManId) covering every Batter and Pitcher that
appears in wbaserunners/. Each run diffs the table against the data and scrapes only players
not yet in it; rows are appended + flushed one at a time, so a crash / ctrl-C / IP block never
loses progress and the next run resumes where this one stopped. Join it onto pitch data with
    df.join(load_heights(), left_on=["Batter", "BatterId"], right_on=["Name", "TrackManId"], how="left")

  Nightly:   python height_scraper.py                  new players, capped at --max-players (250)
  Backfill:  python height_scraper.py --backfill       uncapped; ~5 days for the full history, resumable
  Rescrape:  python height_scraper.py --retry-misses   re-queue not_found / ambiguous / no_height rows
  Preview:   python height_scraper.py --dry-run        queue size + ETA, no requests

Queue order: D1 players first, then by most recently seen -- the backfill reaches current D1
rosters long before the long tail of old summer-league players.

Matching: search.fcgi for "First Last". A unique hit 302s straight to the player page. A results
page is filtered to exact name matches whose BR active years overlap the seasons we observed the
player (+/- 1 yr); if several survive, up to 3 candidate pages are fetched and their School line is
checked against the player's school (TrackMan team acronym -> school via team_acronyms.csv).

Statuses (all terminal -- never re-scraped without --retry-misses):
  found      height parsed              no_height   player page found, no height listed
  not_found  no BR entry matched        ambiguous   several BR entries matched, couldn't disambiguate
Network errors are NOT written to the table, so they retry automatically on the next run.

Sports Reference bans IPs that exceed ~20 requests/min (for a day or more). We throttle to
~9 req/min (--rate to change at your own risk) and abort the run after 3 straight 403/429s.
"""
from __future__ import annotations

import argparse
import csv
import logging
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import polars as pl
import requests

log = logging.getLogger("height_scraper")

BASE = Path(__file__).resolve().parent
WBR = BASE / "wbaserunners"
OUT = BASE / "heights.csv"
ACRONYMS = BASE.parent / "backend" / "models" / "team_acronyms.csv"

SEARCH_URL = "https://www.baseball-reference.com/search/search.fcgi?search="
BR_ROOT = "https://www.baseball-reference.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

FIELDS = ["Name", "TrackManId", "HeightIn", "Height", "WeightLb", "Status", "BRUrl", "ScrapedAt"]
RETRY_STATUSES = {"not_found", "ambiguous", "no_height"}
YEAR_TOLERANCE = 1          # BR active years may lag/lead our sightings by a season
MAX_CANDIDATE_FETCHES = 3   # page fetches spent disambiguating one shared name

# names worth scraping: "Last, First" with letters on both sides (drops "1, Batter" test rows etc.)
_NAME_OK = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .\-]*,\s*[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' .\-]*")

# "<span>6-2</span>,&nbsp;<span>202lb</span>" -- identical on /players/ and /register/ pages
_HT_WT = re.compile(r"<span>(\d)-(\d{1,2})</span>,&nbsp;<span>(\d+)lb</span>")
_HT_CM = re.compile(r"\((\d{2,3})cm")
_SEARCH_ITEM = re.compile(r'<div class="search-item-name">\s*<a href="([^"]+)">([^<]+)', re.S)
_YEARS = re.compile(r"\((\d{4})(?:-(\d{4}))?\)")
_SCHOOL = re.compile(r"<strong>School:</strong>\s*<a[^>]*>([^<(]+)")


class Blocked(Exception):
    """Sports Reference is refusing us (403/429) -- stop before the ban gets longer."""


# ---------------------------------------------------------------------------
# HTTP: one throttled session, tiny in-run caches
# ---------------------------------------------------------------------------
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
        # redirects followed manually so every server hit is throttled (search.fcgi 302s
        # straight to the player page on a unique match)
        r = self.sess.get(url, timeout=30, allow_redirects=False)
        if r.is_redirect and _depth < 5:
            return self._get(urljoin(url, r.headers["Location"]), _depth + 1)
        if r.status_code in (403, 429):
            self._blocks += 1
            if self._blocks >= 3:
                raise Blocked(f"HTTP {r.status_code} x{self._blocks} at {url}")
            log.warning("HTTP %d from BR; backing off 90s", r.status_code)
            time.sleep(90)
            return self._get(url)
        r.raise_for_status()
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


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_height(html: str) -> tuple[str, int, int | None] | None:
    """-> ("6-2", 74, 202lb) from a player page, or None if no height listed."""
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


def parse_candidates(html: str) -> list[dict]:
    """Search-results page -> [{url, name, y0, y1}] for player links only."""
    out = []
    for href, text in _SEARCH_ITEM.findall(html):
        if "/players/" not in href and "/register/player.fcgi" not in href:
            continue
        ym = _YEARS.search(text)
        out.append({
            "url": href if href.startswith("http") else BR_ROOT + href,
            "name": _YEARS.sub("", text).strip(),
            "y0": int(ym[1]) if ym else None,
            "y1": int(ym[2] or ym[1]) if ym else None,
        })
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace(".", "").strip()).casefold()


def search_name(tm_name: str) -> str:
    """'Blessinger, Max' -> 'Max Blessinger'."""
    last, _, first = tm_name.partition(",")
    return re.sub(r"\s+", " ", f"{first.strip()} {last.strip()}").strip()


def load_school_map() -> dict[str, str]:
    if not ACRONYMS.exists():
        return {}
    with open(ACRONYMS, newline="", encoding="utf-8") as f:
        return {r["acronym"]: r["school_name"] for r in csv.DictReader(f) if r.get("school_name")}


# ---------------------------------------------------------------------------
# Resolution: one player -> one row
# ---------------------------------------------------------------------------
def _row(p: dict, status: str, url: str = "", ht: tuple[str, int, int | None] | None = None) -> dict:
    return {"Name": p["Name"], "TrackManId": p["TrackManId"],
            "HeightIn": ht[1] if ht else "", "Height": ht[0] if ht else "",
            "WeightLb": (ht[2] if ht and ht[2] is not None else ""),
            "Status": status, "BRUrl": url,
            "ScrapedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d")}


def _page_row(client: BRClient, p: dict, url: str, html: str | None = None) -> dict:
    ht = parse_height(html if html is not None else client.page(url))
    return _row(p, "found" if ht else "no_height", url, ht)


def resolve(client: BRClient, p: dict, schools: dict[str, str]) -> dict:
    resp = client.search(search_name(p["Name"]))
    if "/search/" not in resp.url:                      # unique match -> straight to player page
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


# ---------------------------------------------------------------------------
# Player universe + table IO
# ---------------------------------------------------------------------------
def gather_players() -> pl.DataFrame:
    """Every (Name, TrackManId) in wbaserunners/, with the context resolve() needs."""
    lf = pl.scan_parquet(WBR / "**" / "*.parquet")

    def side(name_c: str, id_c: str, team_c: str) -> pl.LazyFrame:
        return lf.select(
            pl.col(name_c).alias("Name"),
            pl.col(id_c).cast(pl.Utf8).fill_null("").alias("TrackManId"),
            pl.col(team_c).alias("Team"),
            pl.col("Level"),
            pl.col("Date").str.slice(0, 4).cast(pl.Int32, strict=False).alias("Year"),
            pl.col("Date").alias("LastSeen"),
        )

    both = pl.concat([side("Batter", "BatterId", "BatterTeam"),
                      side("Pitcher", "PitcherId", "PitcherTeam")])
    return (
        both.filter(pl.col("Name").is_not_null())
        .group_by(["Name", "TrackManId"])
        .agg(
            pl.col("Year").min().alias("first_year"),
            pl.col("Year").max().alias("last_year"),
            pl.col("Team").drop_nulls().unique().alias("teams"),
            (pl.col("Level") == "D1").any().alias("is_d1"),
            pl.col("LastSeen").max().alias("last_seen"),
        )
        .filter(pl.col("Name").str.contains(_NAME_OK.pattern))
        .sort(["is_d1", "last_seen"], descending=True)
        .collect()
    )


def load_table() -> dict[tuple[str, str], str]:
    """(Name, TrackManId) -> Status for every row already scraped."""
    if not OUT.exists():
        return {}
    with open(OUT, newline="", encoding="utf-8") as f:
        return {(r["Name"], r["TrackManId"]): r["Status"] for r in csv.DictReader(f)}


def load_heights() -> pl.DataFrame:
    """heights.csv as a join-ready frame -- the file is append-only, so with --retry-misses a key
    can appear twice; the newest row wins."""
    df = pl.read_csv(OUT, schema_overrides={"TrackManId": pl.Utf8})
    return df.unique(subset=["Name", "TrackManId"], keep="last")


def run(backfill: bool, max_players: int, rate: float, retry_misses: bool, dry_run: bool) -> int:
    players = gather_players()
    done = load_table()
    pending = [p for p in players.iter_rows(named=True)
               if (k := (p["Name"], p["TrackManId"])) not in done
               or (retry_misses and done[k] in RETRY_STATUSES)]
    cap = len(pending) if backfill else min(max_players, len(pending))
    log.info("heights: %d players in data, %d in table, %d pending; scraping %d this run",
             players.height, len(done), len(pending), cap)

    if dry_run:
        eta_h = cap * rate * 1.4 / 3600          # ~1.4 requests per player on average
        log.info("dry-run: ~%.1f h at %.1fs/request; next up: %s",
                 eta_h, rate, [p["Name"] for p in pending[:10]])
        return 0
    if not cap:
        return 0

    schools = load_school_map()
    client = BRClient(rate)
    counts: dict[str, int] = {}
    is_new_file = not OUT.exists()
    t0 = time.time()
    with open(OUT, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new_file:
            w.writeheader()
        try:
            for i, p in enumerate(pending[:cap], 1):
                row = resolve(client, p, schools)
                w.writerow(row)
                f.flush()
                counts[row["Status"]] = counts.get(row["Status"], 0) + 1
                if i % 25 == 0 or i == cap:
                    per = (time.time() - t0) / i
                    log.info("heights: %d/%d (%s) eta %.1f h", i, cap, counts, per * (cap - i) / 3600)
        except KeyboardInterrupt:
            log.info("heights: interrupted; %s saved, rerun to resume", counts)
            return 0
        except Blocked as exc:
            log.error("heights: BR is blocking us (%s); %s saved. Wait a day before rerunning "
                      "or lower --rate", exc, counts)
            return 1
    log.info("heights: done %s (%d http requests, %.1f min)",
             counts, client.requests_made, (time.time() - t0) / 60)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Baseball Reference height scraper -> heights.csv")
    ap.add_argument("--backfill", action="store_true", help="no per-run cap (initial multi-day backfill)")
    ap.add_argument("--max-players", type=int, default=250, help="per-run cap outside --backfill")
    ap.add_argument("--rate", type=float, default=6.5, help="seconds between HTTP requests")
    ap.add_argument("--retry-misses", action="store_true",
                    help="also re-scrape not_found/ambiguous/no_height rows")
    ap.add_argument("--dry-run", action="store_true", help="report queue size + ETA, no requests")
    a = ap.parse_args()
    sys.exit(run(a.backfill, a.max_players, a.rate, a.retry_misses, a.dry_run))
