"""TrackMan file names -> (GameID, kind, verified). The one place that knows the naming rule (plan.md §5).

    YYYYMMDD-<stadium>-N[_unverified][_playerpositioning|_battracking][_<variant>].csv|json

    20260502-SwellThomasStadium-1.csv                           pitches, verified
    20260502-SwellThomasStadium-1_unverified_battracking.json   battracking, unverified
    20260613-CharlesSchwabField-2_unverified_playerpositioning_FHC.csv
    20220218-David F. Couch-1.csv                               stadium names can hold spaces and dots

The GameID (everything up to the game number) equals the CSV's GameID column and the JSON's
GameReference, and is identical between an unverified file and its verified version (plan.md §2).
Checked against all 91,469 names on the FTP (2026-10-06): every one parses.

    python -m pipeline_v2.names < names.txt      # counts by kind + any name that doesn't parse
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

_NAME = re.compile(
    r"^(?P<game_id>(?P<date>\d{8})-.+-\d+)"
    r"(?P<unverified>_unverified)?"
    r"(?:_(?P<kind>playerpositioning|battracking)(?:_(?P<variant>[A-Za-z0-9]+))?)?"
    r"\.(?P<ext>csv|json)$"
)
_KINDS = {None: "pitches", "playerpositioning": "positioning", "battracking": "battracking"}


@dataclass(frozen=True)
class TrackmanName:
    name: str
    game_id: str              # e.g. "20260502-SwellThomasStadium-1"
    kind: str                 # pitches | positioning | battracking
    verified: bool
    variant: str | None       # e.g. "FHC" in _playerpositioning_FHC
    ext: str                  # csv | json
    game_date: date | None    # from the name; None if the 8 digits aren't a real date


def parse(name: str) -> TrackmanName | None:
    """Parse a bare file name. None when it doesn't follow TrackMan's pattern (ledger kind 'unknown')."""
    m = _NAME.match(name)
    if m is None:
        return None
    try:
        game_date = datetime.strptime(m["date"], "%Y%m%d").date()
    except ValueError:
        game_date = None
    return TrackmanName(name=name, game_id=m["game_id"], kind=_KINDS[m["kind"]],
                        verified=m["unverified"] is None, variant=m["variant"], ext=m["ext"],
                        game_date=game_date)


if __name__ == "__main__":
    counts: Counter = Counter()
    bad = []
    for line in sys.stdin:
        name = line.strip()
        if not name:
            continue
        p = parse(name)
        if p is None:
            bad.append(name)
        else:
            counts[(p.kind, "verified" if p.verified else "unverified")] += 1
    for (kind, ver), n in sorted(counts.items()):
        print(f"{kind:<12} {ver:<10} {n:>8,}")
    print(f"unparsed: {len(bad)}")
    for name in bad[:20]:
        print("   ", name)
