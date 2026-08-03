# grilled-cheese.md

> **What this file is.** The design record for `delispice` — every structural choice, why it was
> made, and what we decided to change. Written so an AI agent (or a new developer) can read this
> file top-to-bottom and then work in the codebase without re-deriving the architecture.
>
> **Status:** in progress. Built during a "grill-me" session — we walk the codebase top to bottom,
> justify or kill each design choice, and record the verdict here.
>
> Companion goal: **reduce complexity so other developers can contribute.**

---

## 0. Session log

**What we are optimizing for** (stated by the maintainer, Round 1 — this is the objective function
for every verdict in this file):

1. **Traceability** — be able to say exactly what the code is doing, and when a number looks wrong,
   point at the one place it came from.
2. **Modularity** — make it easy for another developer to contribute.

Speed is *not* the primary goal. A change that makes the code slower but locates a metric in one
provable place is a good trade here.

| Round | Scope | Status |
|---|---|---|
| 1 | System map + repo layout + shared domain constants | ✅ done |
| 2 | `data.py` serving layer | ✅ done |
| 3 | `app.py` (2,106 lines, 47 callbacks) | ⬜ not started |
| 4 | `report.py` / `leaderboard.py` | ⬜ not started |
| 5 | `backend/models/` (xRV, RV, Eye, autotagger) | ⬜ not started |
| 6 | `data_pipeline/` | ⬜ not started |
| 7 | Deploy, ops, contributor onboarding | ⬜ not started |

Legend: ✅ decided · 🔵 in progress · ⬜ not started · ⚠️ open question · 🔧 change agreed

---

## 1. What the system is

delispice turns **raw TrackMan pitch-tracking CSVs** into **college-baseball scouting reports**.

One sentence per layer:

1. **Ingest** — nightly job cleans TrackMan CSVs into a partitioned parquet tree.
2. **Model** — offline jobs train per-(level, year) models and precompute per-pitch scores into artifacts.
3. **Serve** — a Dash app queries the parquet tree with DuckDB, joins the model artifacts, and renders reports.

There is **no API server, no database service, and no ORM**. The parquet tree *is* the database;
DuckDB is the query engine loaded in-process. The one exception is a small SQLite file for
human-written scouting notes.

### Scale facts that drive every design choice
- ~7M pitch rows, 206 columns, ~6 GB of parquet.
- Data is **immutable and append-only** — a pitch, once thrown, never changes.
- Single-digit concurrent users (a scouting staff), not a public web product.
- Runs on a Mac laptop as a native window *and* on one Ubuntu box as a web app.

> **The governing principle, stated once:** *never load the 7M-row frame whole.* Almost every
> non-obvious thing in this codebase is downstream of that constraint.

---

## 2. Repo map

```
data_pipeline/          INGEST. Scripts tracked in git; the ~21 GB of data is NOT (rsync'd box-to-box).
  run_pipeline.py         orchestrator — runs each stage as its own subprocess
  pipeline_scaffold.py    stage 1: data/*.csv -> clean/*.parquet (type cast, validate, quarantine)
  fix_dictionary.py       hand-built typo map for TrackMan's dirty enum values
  trackman_schema.py      the 206-column canonical schema
  trackman_pandera_schema.py  validation schema (allowed enum values per column)
  baserunner_state.py     stage 2: clean/ -> wbaserunners/ (adds base-out state per pitch)
  height_scraper.py       stage 3: scrapes Baseball Reference -> heights.csv (bio data)
  compact.py              monthly: rewrites daily parquet into monthly/yearly files
  re_matrix.py            monthly: builds the RE288 run-expectancy matrix
  serving.py              path <-> (level, year) helpers over the partition tree
  wbaserunners/           THE SERVING TREE — {Part}/{Year}/**.parquet  (Part = D1 | Others)

backend/models/         MODELS. Offline training + artifact serving. Imported by the app.
  contact_quality.py      xRV model: k-NN over (ExitSpeed, Angle, Direction) -> expected run value
  cq_store.py             train/serve/cache layer for xRV artifacts
  rv_store.py             realized RV: precomputed state transitions + RE matrix applied on read
  eye_v1.py               Eye model: LightGBM swing/take decision value
  eye_store.py            train/serve/cache layer for Eye artifacts
  autotagger.py           GMM pitch-type clustering (the real implementation)
  cluster.py              thin adapter the app calls into autotagger through
  artifacts/              trained models + per-PitchUID score caches (git-ignored, rebuildable)

backend/notebooks/      Model-dev workspace. Prototype in .ipynb, graduate to backend/models/*.py.

delispice_app/          SERVE. The Dash app.
  data.py    (965)        DuckDB serving layer + caches + retag/cluster stores
  app.py    (2,106)       ALL Dash layout and ALL 47 callbacks
  report.py   (749)       pure report builders + Plotly figures (no Dash, no DuckDB)
  leaderboard.py (700)    population-wide leaderboard/lookup engine
  scouting.py (358)       SQLite store for human-written scouting reports
  launch.py    (64)       desktop mode — Dash in a thread + pywebview native window
  .cache/                 derived: picker indexes, percentile pools, retags.json, autocluster.json
  .data/                  SQLite scouting DB — the ONE store that cannot be regenerated

deploy/                 Ubuntu box: gunicorn + systemd behind Caddy. See deploy/DEPLOY.md.
```

### Design decision 2.1 — three top-level packages, no shared "core" ✅
`data_pipeline` (writes the tree) → `backend/models` (reads the tree, writes artifacts) →
`delispice_app` (reads both). Dependencies point one direction only. Nothing in the pipeline imports
the app; nothing in the app imports the pipeline *except* for paths.

**Why:** each layer runs on a different schedule (nightly / occasional / per-request) and can fail
independently. Keeping them as plain scripts rather than one framework means a stage crash is a
subprocess exit code, not a broken app.

**Verdict:** keep. This is the right shape. See ⚠️ 3.1 for the leak in it.

### Design decision 2.2 — data is git-ignored, code is tracked ✅
21 GB of parquet moves box-to-box by rsync. Model artifacts are git-ignored because they are
*rebuildable from the data*. The SQLite scouting DB is git-ignored because it is *user-written and
NOT rebuildable* — it is the one thing that needs a real backup.

**Verdict:** keep. But see 🔧 7.x (backup story is currently a comment in `.gitignore`, not a job).

---

## 3. Data flow, end to end

```
TrackMan CSV
  │  pipeline_scaffold.py — read as strings, apply fix_dictionary, cast, validate, atomic write
  ▼
clean/*.parquet
  │  baserunner_state.py — replay each half-inning to attach base-out state per pitch
  ▼
wbaserunners/{D1|Others}/{Year}/**.parquet          ← THE SERVING TREE (immutable, append-only)
  │
  ├─► re_matrix.py ──────────► re_matrices/re288_matrix.parquet   (run expectancy by base-out-count)
  │
  ├─► cq_store.train ────────► artifacts/cq_{level}_{year}.*      (xRV model)
  │   cq_store.build_xrv_cache ─► artifacts/xrv_{level}_{year}.parquet   (PitchUID -> xRV)
  │
  ├─► eye_store.train ───────► artifacts/eye_{level}_{year}_{ump,swing,ev}.txt  (LightGBM boosters)
  │   eye_store.build_eye_cache ─► artifacts/eye_{level}_{year}.parquet  (PitchUID -> eye)
  │
  ├─► rv_store.build_state_cache ─► artifacts/state_{year}.parquet  (pitch -> next-state transition)
  │
  └─► height_scraper.py ─────► heights.csv                        (name/id -> height, birthdate)

        ▼  (app start)
delispice_app/data.py
  get_index(role)     one DuckDB pass -> .cache/{role}_index.parquet
                      = DISTINCT (Part, Level, League, Team, Player, Year)   ~small
  get_rows(...)       index says which (Part, Year) partitions the player lives in
                      -> DuckDB reads ONLY those, projecting ONLY that role's ~25 columns
                      -> left-join xRV / RV / Eye artifacts by PitchUID
                      -> lru_cache in process
        ▼
report.py / leaderboard.py   pure Polars -> tables + Plotly figures
        ▼
app.py                       Dash callbacks wire UI state -> those builders
```

### Design decision 3.1 — the picker index ✅
The UI needs "which players exist, at which level, on which team, in which year" *instantly*, and it
needs to know **which files to open** for a chosen player. One DuckDB `SELECT DISTINCT` over the whole
tree produces both, cached to a small parquet.

**Why this and not a database:** it is one derived file, rebuilt by one button, and it makes the
per-player query a *partition-pruned* read instead of a full scan. A Postgres/SQLite mirror of 7M rows
would need a sync story and would be slower than DuckDB-on-parquet for these analytical queries.

**Verdict:** keep — this is the load-bearing idea of the whole app.

### Design decision 3.2 — `Part` vs `Level` ✅
`Part` = physical top-level directory (`D1` or `Others`), used **only** to prune file scans.
`Level` = the fine game level (`D1`, `JUCO`, `D2`, …) that the UI filters on.
They deliberately do not match: some `Level=D1` rows physically live under `Others/`, so a player's
scan must cover **every `Part` they appear in** or the report silently under-counts.

**Verdict:** keep, but this is the single most confusing thing in the codebase for a newcomer.
🔧 It needs to be stated in a docstring in exactly one place and referenced everywhere else.

### Design decision 3.3 — `P4` is a synthetic Level ⚠️
"Power 4" is not a value of the `Level` column — it is `Level='D1' AND League IN (SEC, ACC, BIG10,
BIG12)`. It is presented to the user as if it were a level, so **every level-aware query branches on
it**: `_filter_level()` (Polars), `_level_where()` (SQL), and again in `contact_quality.py`,
`eye_v1.py`, `cq_store.py`, `re_matrix.py`.

**Cost:** five modules must independently remember that P4 is special. Miss the branch in one place
and you get a silently empty result, not an error.

**Open question →** see Round 1 grill Q2.

---

## 4. Cross-cutting conventions an agent must know

| Convention | Rule |
|---|---|
| **DataFrames** | Polars everywhere, never pandas. DuckDB `.pl()` returns Polars directly. |
| **Join key** | `PitchUID` is the universal pitch identity. Every artifact join is on it. |
| **Duplicate PitchUIDs** | A pitch can be written into **two** physical partitions. Every join therefore `.unique(subset=["PitchUID"])` on the right side — otherwise the join fans out and double-counts. This is defended in ~5 places. |
| **Model scoring** | Models are **never trained at request time.** Trained offline → artifact → per-PitchUID score cache → left join. Cache misses are scored live so last night's games are correct before the next batch run. |
| **Null over crash** | A missing artifact yields a null column, not an exception. Reports must tolerate null everywhere. |
| **Caching layers** | (1) `.cache/*.parquet` on disk, (2) `functools.lru_cache` in process, (3) `dcc.Store` in the browser. Invalidation is manual: the **⟳ Rebuild index** button. |
| **User edits** | Retags (`retags.json`) and clusters (`autocluster.json`) are **non-destructive overlays** applied at read time, in separate files so one can be reverted without touching the other. |
| **Empty frames** | Must carry a real dtype schema (`_schema_for`), or report builders crash doing numeric ops on an all-string empty frame. |

---

## 5. Round 1 findings

### 🐛 Finding 1.0 — CONFIRMED BUG: pitcher `Avg EV` and `Hard Hit %` include foul balls

This is the concrete instance of the duplication problem, found by grilling it. **TrackMan records
`ExitSpeed` on foul balls**, not just balls in play. Verified on 40 files of `D1/2025`:

```
InPlay                rows with EV = 1,789
FoulBallNotFieldable  rows with EV = 1,289     <-- 42% of all EV-bearing rows
BallCalled / HitByPitch / StrikeSwinging / ...    ~45 more
```

Six places in the codebase compute an exit-velocity statistic. **Four filter to `PitchCall='InPlay'`.
Two do not.**

| Site | Population | Correct? |
|---|---|---|
| `report.py:213` batter summary | `InPlay & ExitSpeed.is_not_null()` | ✅ |
| `report.py:287` batter pitch table | `inplay & ExitSpeed.is_not_null()` | ✅ |
| `data.py:924` percentile pool | `PitchCall='InPlay' AND ExitSpeed IS NOT NULL` | ✅ |
| `leaderboard.py:170` | `PitchCall='InPlay' AND ExitSpeed >= 95` | ✅ |
| **`report.py:152-153` pitcher arsenal table** | **`ExitSpeed.is_not_null()` — any pitch call** | ❌ |
| **`report.py:444` pitcher location heatmap** | **`ExitSpeed.is_not_null()` — any pitch call** | ❌ |

Measured impact on the same 40-file sample:

```
Hard Hit %   correct (InPlay only) :  36.78 %
Hard Hit %   arsenal table as-is   :  24.21 %     off by 12.6 points
Avg EV       correct (InPlay only) :  86.94 mph
Avg EV       arsenal table as-is   :  82.02 mph   off by 4.9 mph
```

**The user-visible symptom:** on the pitcher report, the arsenal table and the Percentile Rankings
panel are *on the same screen* and disagree about Avg EV and Hard Hit% — because the arsenal comes
from `report.build_arsenal` and the sliders come from `data.percentile_pool`, which independently
reimplement the same stat. The batter report is right; the pitcher report is wrong.

### ✅ Finding 1.0 — FIXED

**Decision (maintainer):** foul balls are not counted in any exit-velocity metric.

**The fix** — one shared predicate in `report.py`, applied at *every* EV site in the file:

```python
IN_PLAY     = pl.col("PitchCall") == "InPlay"
BATTED_BALL = IN_PLAY & pl.col("ExitSpeed").is_not_null()
```

| Site | Before | After |
|---|---|---|
| `report.py:160` arsenal `Avg EV` | `ExitSpeed.mean()` | `.filter(BATTED_BALL)` |
| `report.py:161` arsenal `Hard Hit %` | `.filter(ExitSpeed.is_not_null())` | `.filter(BATTED_BALL)` |
| `report.py:452` heatmap `Hard hit%` | population `ExitSpeed.is_not_null()` | population `BATTED_BALL` |
| `report.py:221` batter summary | *(already correct)* | rewritten to use `BATTED_BALL` |
| `report.py:295-297` batter pitch table | *(already correct)* | rewritten to use `BATTED_BALL` |

The last two were not broken — they were rewritten anyway so all five sites now reference **one**
definition. That is the point: the next person to touch an EV metric has one place to look.

**Verified** on the same 40-file `D1/2025` sample that exposed it:

```
ground truth  (in-play only)      Avg EV 86.94   Hard Hit% 36.78%
arsenal table (post-fix)          Avg EV 86.94   Hard Hit% 36.78%   ✅
batter summary                    Avg EV 86.9    Hard Hit% 37%      ✅
batter pitch table ("All" row)    Avg EV 86.9    Hard Hit% 37%      ✅
heatmap population                1,789 rows, all InPlay            ✅  (was 3,123)
```

And the user-visible symptom — two panels on the same pitcher screen disagreeing — is gone:

```
pitcher: Kimball, Blake  (20 batted balls)
  arsenal table    (report.build_arsenal)  Avg EV 86.90   Hard Hit% 40.00%
  percentile panel (data.percentile_pool)  Avg EV 86.90   Hard Hit% 40.00%   AGREE
```

App imports clean, 47 callbacks registered, all batter figures build.

**Sites confirmed already correct, left alone:** `data.percentile_pool` (`bbe_ev`),
`leaderboard._flags_sql` (`ev`, `is_hardhit`, `is_barrel`, `max_ev`), `leaderboard._xrv_sums`,
`data._with_xrv`, `report.spray_fig`, `report._ev_surface`.

**Not applied to `backend/models/`** — those train and score on their own explicit `InPlay` filters
and were already correct; the models were not retrained and their artifacts are unaffected.

---

### 🔧 Finding 1.1 — the duplication is of *computations*, not just constants

The constants are duplicated because the **metric definitions** are duplicated. Whiff%, Chase%,
Hard Hit%, and Barrel% are each implemented independently **four times** — once in Polars for the
pitcher report, once in Polars for the batter report, once in DuckDB SQL for the percentile pool,
once in DuckDB SQL for the leaderboard. Finding 1.0 is what that costs.

| Fact | Written down in |
|---|---|
| what counts as a **swing** | `report.py:30` (list) · `leaderboard.py:42` (tuple) · `data.py:876` (SQL literal) · `eye_v1.py:58` (list) |
| the **strike zone** | `report.py:433` (dict, feet) · `leaderboard.py:46` (SQL string) · `data.py:922` (inline SQL, uncommented) |
| **hard hit** = 95 mph | `report.py:34` · `leaderboard.py:45` (comment: *"matches report.HARD_HIT_MPH"*) |
| **P4 leagues** | `data.py:81` · `contact_quality.py:27` · `re_matrix.py:48` |
| **barrel** definition | `data.py:925` (inline SQL) · `leaderboard.py:171` (SQL) |
| **pitch family** | `report.py:231` (`PITCH_FAMILY`, raw tags) · `eye_v1.py:65` (`FAMILY`, AutoPitchType names) |

Today the *constants* agree — someone kept them in step by hand, and left comments saying so
(`report.py:28`, `leaderboard.py:41`, `leaderboard.py:45`). Those comments are the codebase asking
for this fix. What did **not** stay in step is the surrounding *logic*, which is Finding 1.0.

**The counter-argument already in the code**, at `report.py:28`:

> *"`data._SWINGS_SQL` and `leaderboard._FOULS` must carry the same three values; report.py is
> DuckDB-free and data.py is plotly-free, so the constant genuinely cannot be shared."*

This is half right and worth stating precisely, because it is the reason the duplication exists:
`report.py` must not import `data.py` (would drag in DuckDB) and `data.py` must not import
`report.py` (would drag in Plotly). Both true — keeping those two modules independent is a **good**
design choice and should survive this session.

But the conclusion doesn't follow. A **third** module that imports neither DuckDB nor Plotly can be
imported by both. The dependency graph stays acyclic and both purity rules hold:

```
             baseball.py          (stdlib only — no duckdb, no plotly, no polars needed)
            /     |      \
     report.py  data.py  leaderboard.py
     (plotly)   (duckdb)   (duckdb)
```

**Proposed fix** — `delispice_app/baseball.py`, owning each fact once and emitting it in whichever
encoding the caller needs:

```python
SWING_CALLS = ("StrikeSwinging", "FoulBall", "FoulBallNotFieldable", "FoulBallFieldable", "InPlay")
HARD_HIT_MPH = 95
ZONE = Zone(bottom=1.5, top=3.3775, half_width=0.83)

sql_in(SWING_CALLS)   # -> "('StrikeSwinging','FoulBall',...)"   for the DuckDB callers
ZONE.sql()            # -> "ABS(PlateLocSide) <= 0.83 AND PlateLocHeight BETWEEN 1.5 AND 3.3775"
BATTED_BALL = "PitchCall = 'InPlay' AND ExitSpeed IS NOT NULL"   # the thing Finding 1.0 got wrong
```

**Why this serves traceability specifically:** it makes "where does Hard Hit% come from?" a question
with **one** answer instead of four. Grep for `HARD_HIT_MPH`, get one definition site and its call
sites. That is the property the maintainer asked for.

**Status:** ⚠️ awaiting a decision on scope — see Q1.

---

## 5b. Multi-source future-proofing — where the seam goes

**Goal stated by the maintainer:** `baseball.py` should be the ground truth for advanced statistics,
and the app should survive data arriving from a source other than TrackMan.

### ⛔ Rejected: one canonical parquet tree that all sources are merged into

*This was the first proposal in this session. It was wrong and is recorded here so it doesn't get
re-proposed.* The idea was that every source's ingest would write into one shared 201-column schema,
so nothing downstream learned there was more than one source.

**Why it's wrong** (maintainer's objection, correct):

1. Sources do not have the same columns. A union schema means one mostly-null mega-table; an
   intersection schema throws away most of what each vendor measures.
2. You would never physically merge a Hawk-Eye or Statcast file into the TrackMan parquets — they
   are different measurement systems, and often different *populations* entirely (college TrackMan
   vs MLB Statcast). There is no query where those rows belong in one scan.
3. `trackman_schema.py` is therefore **correctly named**. It is the ingest schema for TrackMan files
   only, and should stay that way. So should any future `hawkeye_schema.py`.

### Design decision 5b.1 — the contract is the 36-column REPORT VOCABULARY, not the ingest schema ✅

The measurement that resolves this:

```
canonical TrackMan ingest schema : 201 columns
what the app actually reads      :  36 columns   (18%)
   PITCHER_COLS  29  ·  BATTER_COLS  25  ·  shared core  18
```

The other ~165 columns exist only to be passed through to the CSV/parquet export
(`data.download_frame` does `SELECT *`). **The app's real contract with the data is 36 columns wide,
and it is already declared** — `data.py:41` (`PITCHER_COLS`) and `data.py:49` (`BATTER_COLS`).

That is the thing that must be source-agnostic. Not the ingest schema.

```
data_pipeline/
  trackman_schema.py  (201 cols) ──► wbaserunners/     TrackMan tree, TrackMan schema
  hawkeye_schema.py   (N cols)   ──► hawkeye/          separate tree, separate schema
        ▲ each is its own ingest contract — NEVER merged on disk

delispice_app/data.py
  PITCHER_COLS / BATTER_COLS  ── the 36-column REPORT VOCABULARY
        ▲ source-agnostic. A new source must be able to PRODUCE these 36 names
          from its own tree, via its own projection. That projection is the adapter.

baseball.py
  metrics defined over the 36-column vocabulary
```

**So the seam moves from ingest to read** — but it is a *narrow* seam (36 columns, not 201), and
`data.py` is already organized for it. `ROLES` at `data.py:56` is already a registry keyed by one
dimension, holding per-key column lists and cache names. A `SOURCES` registry is the same shape.

**Naming note:** the 36-column vocabulary currently uses TrackMan's own names (`RelSpeed`,
`HorzBreak`). Renaming it to vendor-neutral names would touch every module for a source that may
never arrive. **Don't.** Keep TrackMan's names as the app vocabulary and let future adapters alias
into them — pragmatic, zero migration, and honest as long as §5b.4 is respected.

### 5b.2 — ⚠️ The expensive part is not column mapping *(dormant — see 5b.6)*

Renaming columns is the cheap half and the half everyone plans for. The costly half:

> **Not every column means the same thing across measurement systems, and pooling them silently
> corrupts every percentile and every trained model.**

| Column | Portable across sources? |
|---|---|
| `RelSpeed`, `ExitSpeed`, `PlateLocHeight/Side`, `Angle` | ✅ mostly — same physical quantity, minor calibration offsets |
| `InducedVertBreak`, `HorzBreak` | ❌ **different reference frames AND units** (TrackMan inches vs Statcast `pfx_z` feet). Mapping them into one column makes cross-source comparison wrong *and silent*. |
| `SpinAxis`, `SpinRate`, `Extension` | ❌ device-dependent calibration |
| `AutoPitchType` | ❌ each vendor ships its own classifier |

Three things in this repo break if two sources are pooled naively:

1. **`data.percentile_pool`** partitions by `(Level, Year)`. A TrackMan pitcher and a Hawk-Eye
   pitcher in one velo/break percentile distribution produces a number that is not wrong-looking,
   just wrong.
2. **`cq_store` / `eye_store`** train per `(Level, Year)`. A k-NN over `(ExitSpeed, Angle, Direction)`
   trained on one system and scoring another is a silent calibration error, not an exception.
3. **`PitchUID`** uniqueness must hold *across* sources, since every artifact joins on it.

**Therefore, whatever else we do:** the canonical schema needs a **`Source` column**, and any pooled
or trained statistic over a device-dependent column must partition by `Source` too.

### 5b.3 — THREE artifacts, not two (common misreading — worth stating explicitly)

A natural but wrong reading is *"`baseball.py` takes columns from however many sources and maps them
to common names."* That would make `baseball.py` know about every source — centralizing the coupling
rather than removing it. Split it:

| # | Artifact | Owns | Today |
|---|---|---|---|
| 1 | **Per-source ingest schema** (one per source) | that vendor's own columns, dtypes, enum fixes | `trackman_schema.py` + `fix_dictionary.py` — **correctly named**, TrackMan-only |
| 2 | **Report vocabulary** (one, shared) | the 36 columns the app reads | `data.py:41` `PITCHER_COLS` / `data.py:49` `BATTER_COLS` — **already exists** |
| 3 | **`baseball.py`** | metrics defined *over* #2 | to build |

Flow: each source ingests into **its own** tree under **its own** schema (#1). A per-source
projection produces the 36-column vocabulary (#2) at read time. `baseball.py` (#3) computes Hard Hit%
over those 36 names and never learns which source produced them.

`baseball.py` should not contain the string `"Statcast"` or `"Hawk-Eye"` anywhere. (It will contain
TrackMan-derived *column names*, because those are the app vocabulary — see the naming note in 5b.1.)

### 5b.4 — ⚠️ A mapping is sometimes a RENAME and sometimes a MODEL. Do not conflate them.

Worked example, because this is the one that gets missed:

| | TrackMan `HorzBreak` | Statcast `pfx_x` |
|---|---|---|
| unit | **inches** | **feet** (Savant *displays* inches — how this sneaks through) |
| window | full release-to-plate flight | PITCHf/x convention, ~last 40 feet |
| sign | vendor convention | vendor convention — **verify, never assume** |

`ExitSpeed → exit_velocity` is a **rename**: same quantity, same unit, safe.
`pfx_x → horizontal_break` is a **model**: unit conversion + a different integration window + a sign
convention. The honest version is a documented conversion whose scale factor is *calibrated against
overlapping pitches* if any exist, carrying an explicit "approximately comparable" caveat.

Pushing both through one undifferentiated "mapping" layer is exactly how you get a percentile chart
that is wrong and looks fine. The canonical schema should therefore record, per column, **which kind
of mapping produced it** — and device-dependent columns keep `Source` alongside so
`data.percentile_pool` and model training can refuse to pool them.

### 5b.5 — build the seam, not the framework ✅

The standard failure here is designing a two-source abstraction against one source, producing an
interface shaped exactly like source #1 that breaks on source #2. So:

**Do now** (each is justified on its own merits even if a second source never arrives):
- `baseball.py`, metrics defined over the 36-column report vocabulary. *Already justified by bug #0.*
- Promote `PITCHER_COLS` / `BATTER_COLS` from "the columns we happen to SELECT" to **the documented
  contract between the app and any data source**. This is a docstring, not a refactor — the list
  already exists and is already correct.
- Write down the portability table (5b.4), so the calibration trap is a documented fact rather than
  a future discovery.

**Do NOT do now:**
- ~~Add a `Source` column to a shared schema~~ — there is no shared schema. If sources live in
  separate trees, the **tree path already identifies the source**, exactly as `serving.py` already
  parses `(level, year)` from the path today.
- An abstract `SourceAdapter` base class or plugin registry — no second source exists to validate it.
- Any attempt to unify break/spin semantics. Needs real data from the real second source.
- Renaming the 36-column vocabulary to vendor-neutral names.

### ✅ 5b.6 — RESOLVED: no report spans two sources

**Decision (maintainer):** a single report will never draw on two sources. Source is therefore *not*
a row-level concern — it does not need a column, a read-time union, per-source percentile pools, or
source-keyed models.

**Consequences — this closes most of §5b as "not needed":**

| Concern raised earlier | Status now |
|---|---|
| `Source` column on rows | ❌ not needed — tree path identifies the source |
| Read-time union across trees | ❌ not needed |
| `percentile_pool` partitioned by source | ❌ not needed — a pool never mixes sources |
| Models keyed by source | ❌ not needed — a model is trained and scored within one tree |
| Break/spin calibration hazard (5b.4) | 🟡 **dormant, not solved** — it only bites if two sources ever land in one chart. Keep 5b.4 written down; do not act on it. |

**What survives from §5b:** exactly one thing — the 36-column report vocabulary
(`PITCHER_COLS` / `BATTER_COLS`) is the app's contract with any data source, and `baseball.py`'s
metrics are written over it. That was already justified by bug #0 on its own merits.

Net effect: multi-source future-proofing costs **zero additional work today.**

---

## 5c. Round 2 findings — `data.py` (the serving layer)

### 🔧 Finding 2.1 — `data.py` is only 28% "DuckDB serving layer"

Its docstring says *"DuckDB serving layer for delispice_app."* Measured by section:

| Concern | Lines | Share | Belongs in a module called "serving layer"? |
|---|---:|---:|---|
| **Edit stores** (retags + autocluster) | 271 | 28% | ❌ user-edit persistence, no DuckDB at all |
| **Serving** (index, options, per-player scan, pool) | 267 | 28% | ✅ |
| **Model scoring** (xRV / RV / Eye attach) | 227 | 24% | ❌ artifact joins, could stand alone |
| Infra (paths, conn, column lists, P4, team maps) | 124 | 13% | ✅ |
| **Bio store** (heights.csv read/append) | 76 | 8% | ❌ unrelated CSV store |

The single largest concern is **AutoCluster (203 lines)** — GMM cluster persistence, hand-review
queues, cluster splitting. It touches no database and belongs beside `backend/models/cluster.py`.

**Why this matters for the stated goals:** a contributor asked to "add a stat to the pitcher report"
opens the serving layer and finds a 200-line cluster-review state machine. There is no signpost
telling them which 28% is relevant.

**Proposed split** (mechanical — these are already separated by section comments, with no
cross-calls between them except through `get_rows`):

```
data.py         serving only: index, cascading options, per-player scan, percentile pool   (~390)
scoring.py      xRV / RV / Eye attach                                                      (~227)
edits.py        retags.json + autocluster.json stores                                      (~271)
bio.py          heights.csv read/append                                                    (~76)
```

**Status:** ⚠️ proposed.

---

### 🐛 Finding 2.2 — CONFIRMED BUG: **⟳ Rebuild index** leaves 7 caches stale

`cb_refresh` (`app.py:1872`) clears exactly three things:

```python
data.get_index(role, force_rebuild=True)   # index parquet + _INDEX_MEM
data.clear_percentile_pools()              # percentile_pool lru + pctpool_*.parquet
leaderboard.clear_pools()                  # leaderboard pools
```

Every other cache in `data.py` survives it:

| Cache | maxsize | Holds | Consequence of surviving a rebuild |
|---|---:|---|---|
| **`_rows_cached`** | 64 | a player's raw rows | **new games missing from an already-viewed player's report** |
| **`_scored_cached`** | 64 | those rows + xRV/RV/Eye | same, one layer up — this is what `get_rows` returns |
| `_xrv_cache` | 16 | PitchUID → xRV | freshly-built xRV artifacts not picked up |
| `_eye_cache` | 4 | PitchUID → eye | freshly-built Eye artifacts not picked up |
| `_cq_model` / `_eye_model` | 16 | trained models | retrained models not picked up |
| `re_matrix_options` | 1 | which (level, year) have artifacts | new artifacts don't appear in the RE picker |

**Reproduction:** open a player's report → new games land in `wbaserunners/` → click **⟳ Rebuild
index** → reselect the same player. The status line reports a larger index; the report still shows
the pre-rebuild rows, because `_scored_cached` hits on `(role, player, level, team, years_key,
re_level, re_year)` — none of which changed.

This is precisely the failure mode the maintainer named: a number is wrong and there is no
indication where it came from. The index says one thing, the report shows another, and the button
that is supposed to fix it reports success.

**Fix:** `cb_refresh` should clear all of them. `data.py` should expose one
`clear_caches()` that owns the list, so a future cache added to `data.py` doesn't have to be
remembered in `app.py`. That inversion is the actual fix — the current design requires a caller in a
different file to know `data.py`'s private cache inventory.

**Status:** 🔧 fix proposed, not applied.

---

### 🔧 Finding 2.3 — `_with_xrv` / `_with_rv` / `_with_eye` are one function written three times

~90 lines across three functions with **identical control flow**:

```python
out = df.with_columns(pl.lit(None, Float64).alias(NAME))     # 1. null column
...guard on empty / missing columns...                       # 2. guard
for (lvl, yr), grp in df.partition_by(["Level","Year"], as_dict=True).items():
    model_level = P4_LEVEL if sel_level == P4_LEVEL else lvl # 3. resolve P4  ← repeated 3x
    ...score the group, append...                            # 4. score
return out.join(pl.concat(scored).unique(subset=["PitchUID"]),  # 5. unique-join-rename-drop
                on="PitchUID", how="left")...
```

They differ in exactly three places: the output column name, which scorer to call, and whether the
Run-Expectancy override applies (xRV and RV take it; Eye deliberately does not — see `_with_eye`'s
docstring, which is a genuinely good explanation and must survive any refactor).

**Bonus:** this also carries **3 of the 7 `P4_LEVEL` branches in `data.py`** (lines 436, 467, 548 —
the identical `P4_LEVEL if sel_level == P4_LEVEL else lvl` idiom). Consolidating the attachers
retires nearly half of ledger #4 for free, without the tree reprocess that the full P4 fix needs.

**Status:** ⚠️ proposed.

---

### 🔧 Finding 2.4 — the parquet-scan target is built four times

The same "index → partitions → glob list → WHERE + params" sequence appears at:

| Site | Note |
|---|---|
| `data._rows_cached:351` | per-player scan |
| `data.download_frame:843` | CSV export — a near-verbatim copy of the above |
| `data.percentile_pool:900` | percentile pool |
| `leaderboard._scan_target:263` | **already extracted into a named function** |

`leaderboard.py` solved this and `data.py` did not. The extracted version should move somewhere both
can import, and `data.py`'s three copies should call it.

**Status:** ⚠️ proposed.

---

### 🧹 Finding 2.5 — `get_pitches` is dead code

`data.py:660`, documented as a *"back-compat helper for the pitcher role."* Nothing outside `data.py`
calls it — every caller uses `get_rows` directly. Delete.

**Status:** ⚠️ proposed.

---

### 📉 Finding 2.6 — performance items (real, but small at this scale — do not prioritize)

Measured, and reported honestly given that speed is explicitly *not* this session's goal:

| Item | Measured | Verdict |
|---|---|---|
| `_con()` opens a **new DuckDB connection per query** | 7.9 ms setup; a 40-file scan is **2.3× slower** on a fresh connection (26 ms vs 11 ms) than a reused one, because parquet metadata is re-read | **Keep as-is.** A shared connection is not thread-safe for concurrent use — Dash/gunicorn serve callbacks on multiple threads, so a shared handle needs `con.cursor()` per thread. Per-query connections are the *simple correct* choice. ~15 ms on a report load is not worth the concurrency risk. |
| `search_players` runs on every keystroke | 20–42 ms; O(index) with a `str.contains` pass **per token, per role index** — a 2-word query does 4 full scans of 213k rows | Minor. Noticeable in a type-ahead but not broken. Cheap fix if ever wanted: precompute the lowercased "name + school" search column once at index build instead of rebuilding it per keystroke. |
| Picker indexes | pitcher 89,007 rows × 7 cols · batter 123,946 × 7; load 7–80 ms | ✅ working exactly as designed — this is the load-bearing optimization and it pays off |

---

### ✅ Design decisions in `data.py` that are RIGHT and must survive any refactor

Recording these so a future refactor doesn't "simplify" them away:

| Decision | Why it's load-bearing |
|---|---|
| **Typed empty-frame schema** (`_schema_for`) | An untyped empty frame is all-`Utf8`; report builders do numeric ops and would crash on every no-data selection. Subtle and easy to delete by accident. |
| **`.unique(subset=["PitchUID"])` on every artifact join** | A pitch can be written into two physical partitions. A duplicate key fans out the join and double-counts — silently. Defended in 5 places, correctly. |
| **Models never trained at request time** | Train offline → artifact → per-PitchUID cache → left join. Keeps report latency bounded and makes scores reproducible. |
| **Lazy `backend.models` imports** | sklearn / LightGBM only load if clustering or scoring actually runs; the app starts fast and runs with no models trained. |
| **`_rows_cached` separate from `_scored_cached`** | Split toggles and RE-override changes re-score without re-querying DuckDB. Correct layering. |
| **Fetch cached before live scoring** (`_xrv_for`, `_eye_for`) | Last night's games are correct before the next batch run, paying the k-NN cost only on the delta. |

⚠️ **One caveat on the last one:** `_xrv_for`'s live path is an 800-neighbour k-NN search. Its own
docstring says ~20 s per season. If a large uncached selection is requested, that cost lands
*inside a Dash callback* with no progress indicator. Not a bug — worth knowing about.

---

## 6. Open questions (current round)

*(answers get folded into the sections above, then this list clears)*

- **Q1** — Scope of the shared-constants module. *(deferred — more explanation requested; delivered
  in Finding 1.1 above, decision pending)*
- **Q2** — Is `P4` worth its five-module branching cost? *(deferred)*
- ~~**Q3** — What complexity pain do you actually feel?~~ ✅ answered: traceability + modularity
  (recorded in §0).

---

## 7. Complexity ledger

Running list of everything worth fixing, ordered as we agree on it.

| # | Item | Size | Status |
|---|---|---|---|
| 0 | ~~**BUG**: arsenal + heatmap `Avg EV` / `Hard Hit %` count foul balls~~ | S | ✅ **fixed + verified** |
| 1 | Metric definitions implemented 4× (Polars ×2, SQL ×2); constants duplicated across 6 modules | M | ⚠️ proposed |
| 2 | `app.py` is 2,106 lines / 47 callbacks in one file | L | ⬜ round 3 |
| 2a | **BUG**: ⟳ Rebuild index leaves 7 lru caches stale → stale reports after new games | S | ⚠️ round 2 |
| 2b | `data.py` is 28% serving; split out edits / scoring / bio | M | ⚠️ round 2 |
| 2c | `_with_xrv`/`_with_rv`/`_with_eye` — one function written 3× (also 3 of 7 P4 branches) | S | ⚠️ round 2 |
| 2d | Scan-target construction duplicated 4× (`leaderboard._scan_target` already solves it) | S | ⚠️ round 2 |
| 2e | `get_pitches` is dead code | XS | ⚠️ round 2 |
| 3 | **Zero tests** — nothing pins the metric definitions, which is why #0 survived | M | ⬜ round 4 |
| 4 | `P4` special-casing in 5 modules | M | ⚠️ open |
| 5 | No backup job for the un-regenerable SQLite scouting DB | S | ⬜ round 7 |
| 6 | Document `PITCHER_COLS`/`BATTER_COLS` as *the* app↔data contract (docstring, not refactor) | S | ⚠️ proposed |
| ~~7~~ | ~~Add `Source` column~~ · ~~partition pooled stats by source~~ · ~~rename `trackman_schema.py`~~ | — | ❌ withdrawn — §5b.6 |

---

## 8. Future considerations / projects

Not scheduled. Recorded so the reasoning isn't re-derived, and so today's decisions don't
accidentally foreclose them.

### 8.1 — Biomechanics / video data → a NEW report, not a new source

**Maintainer's framing (Round 1):** if biomech or video data arrives, it would drive **an entirely
separate report**, not feed the existing pitcher/batter reports.

This is a materially easier problem than the Hawk-Eye/Statcast case debated in §5b, and the reason
is worth stating because the two get conflated:

| | **Alternative source** (Hawk-Eye, Yakkertech) | **Additive modality** (biomech, video) |
|---|---|---|
| Relationship to TrackMan | *competes* — same measurements, different vendor | *extends* — different measurements, same events |
| Example columns | `RelSpeed`, `HorzBreak` — TrackMan already has these | joint angles, hip-shoulder separation, pose keypoints — TrackMan has nothing comparable |
| Merge pressure | high — both want to be "the" pitch data | low — nothing to reconcile |
| Core hazard | **calibration** (§5b.4): two systems disagree about the same quantity | **join grain + coverage**: which events are covered, at what resolution |
| Effect on existing reports | would need per-source everything | **none** — new report, new vocabulary |

**Why this is the easy case:** there is no shared statistic to corrupt. Nothing pools biomech data
into a velo percentile. The §5b.4 unit/reference-frame trap does not apply, because no biomech column
is competing with a TrackMan column for the same meaning.

**The real design questions when it happens** (do not pre-solve these):

1. **Join grain.** Biomech is usually captured per *pitch* and often syncable to `PitchUID`; video
   may only be per *session* or per *at-bat*. The grain determines whether this is a join or a
   parallel store. Answer it against real data, not in advance.
2. **Coverage sparsity.** Biomech exists for a handful of pitchers on lab/bullpen days, not for
   30,000 games. Every aggregate over it needs an explicit denominator, or it silently reports on a
   biased sample. This is the biomech equivalent of bug #0.
3. **Storage.** Video is not parquet. It would need its own store and a pointer column, not a
   column of blobs in the pitch tree.
4. **Its own report module.** Under the current architecture that means a new `report_*.py` builder
   plus its own tab — which is exactly the modularity argument for splitting `app.py` (ledger #2).
   **If biomech is on the horizon, ledger #2 gets more valuable, not less.**

**What today's decisions cost this project:** nothing. Separate trees + a per-report vocabulary is
already the shape a new modality wants.

### 8.2 — Dormant: cross-source calibration (§5b.4)

Only becomes live if the "no report spans two sources" decision in 5b.6 is ever reversed. The
portability table in §5b.4 stays in this file as the record of *why* it would be expensive. Do not
act on it.

### 8.3 — Deferred from the ledger

- `P4` as a real column in the tree rather than a 5-module special case (ledger #4) — requires a
  full reprocess of the parquet tree and invalidates every artifact. Worth it only if bundled with
  another reprocess.

---

## 9. Facts an agent should not have to re-derive

- Python 3.14, `.venv` at repo root. Three requirements files: `requirements.txt` (core),
  `requirements-desktop.txt` (+pywebview), `deploy/requirements-server.txt` (+gunicorn).
- Run web: `python -m delispice_app.app` → `http://127.0.0.1:8765`. Run desktop:
  `python -m delispice_app.launch`.
- Deployed at `mausington@10.0.0.245`, gunicorn + systemd behind Caddy. **Never `sudo`** on that box
  (it breaks `.cache` ownership).
- Retraining: `python -m backend.models.cq_store` (xRV), `python -m backend.models.eye_store --eye`.
- The app tolerates missing artifacts — it just shows null columns. You can run it with no models trained.
