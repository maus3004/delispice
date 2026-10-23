# Game Reports: decisions

A new **Game Reports** tab: pick a player and one of their games, and see that player's report for the game (a pitcher's outing or a batter's game). This file records each decision before we build, one question at a time. Nothing is built yet.

Context (2026-10-16): Lookup and Shortlist are retired from the tab bar (`RETIRED_TABS` in `app.py`). Their code is kept, so either can come back by removing it from that set.

---

## 1. Where does the code go?

**Question:** a new Python file, or add it all to `app.py`?

### How the app is organized today

| File | Lines | What it holds |
|---|---|---|
| `app.py` | 2,102 | Every tab's page layout and callbacks, plus the shared styles (`FONT`, `MAROON`, table styles) and small UI helpers (`html_table`, `_title`, `_graph`) |
| `data.py` | 1,018 | Reading the pitch data (DuckDB over `pipeline_v2/serving/`), the picker index, caches |
| `report.py` | 757 | The player reports' tables and Plotly figures (no Dash code) |
| `leaderboard.py` | 696 | The Lookup tab's queries and metrics (no Dash code) |
| `scouting.py` | 358 | The Shortlist's database |

The pattern: **each feature's "what to show" logic lives in its own file, and `app.py` lays out the page and wires the callbacks.** Lookup is `leaderboard.py` plus ~295 lines in `app.py`; the player reports are `report.py` plus their section of `app.py`.

### Options

| | Option | Good | Costs |
|---|---|---|---|
| A | Everything in `app.py` | No new file | `app.py` grows past ~2,500 lines; the game logic can't be tried in a notebook without starting the app; harder to retire later |
| B | **New `game_report.py` for the logic; the tab's layout + callbacks in `app.py`** | Same pattern as Lookup and the player reports; the logic can be built and checked in a notebook first; no new wiring | `app.py` still grows a few hundred lines |
| C | New file holding the logic **and** its own layout + callbacks | A fully self-contained tab | A new pattern for this app. It needs the shared styles and helpers, which live in `app.py`, and `app.py` would import it back (a circular import), so those would first have to move to their own file |

### Recommendation: B

- **`game_report.py`** (no Dash): find the games for the picker, load one game's pitches, and build each section as a polars table or a Plotly figure.
- **`app.py`**: the Game Reports tab, its picker, and the callbacks that call `game_report.py` and render the results with the existing helpers (`html_table`, `_graph`).

It keeps the split the app already uses, so each piece goes where you'd look for it. And since `game_report.py` returns plain tables and figures, each section can be built and checked in a notebook before it's wired into the page. If `app.py` gets unwieldy later, moving each tab into its own file (option C) is a separate cleanup, not something to start this feature with.

**Decision (2026-10-16): B.** `game_report.py` holds the logic; the tab's layout and callbacks go in `app.py`.

---

## 2. How does a user find a game?

**Decision (2026-10-16, Matt's design):** the same picker as the Pitchers and Batters tabs. **Level → Conference → Team → game date (or a date range) → Pitcher or Batter.** The player dropdown is searchable on its own; choosing a level, conference, team or date only narrows it. *(Order changed 2026-10-19: the date sits above the player, and a typed date narrows the list to the players in games that started on that date or in that range; with no date the list spans every season.)* Once a game is picked, the report shows that player's data from that game, and an area shows **the game's file name** so the user can confirm it's the right game. *(This replaces the team-first game list proposed earlier: the report is about one player in one game.)*

**The date (decided 2026-10-16):** the user **types the full date, year included**; there's no separate Year dropdown. The date's year decides which season of the player's rows to load. How it behaves:
- A date field that accepts typing (and a calendar click); the calendar opens on the player's latest game. *(Built 2026-10-19 without greying out the days the player didn't play: Dash's date picker won't accept a greyed-out day even when typed, which would block a range's Start / End and the no-game message below.)*
- A date with no game for that player says so and lists their nearest game dates.
- A date with two games (doubleheader) asks which one: G1 or G2, by the `GameID`'s game number or first-pitch time. D1 2026 has 6,605 batter-days with two games, so this comes up often.
- Changing the date drops the picked player if they didn't play then. When one name belongs to several TrackMan players, "Which player?" lists only the ones who played on the typed date.

**A date range (added 2026-10-16):** two fields, **Start** and **End**, so one report can cover a stretch of games (a weekend series, conference play, the postseason, a fall of live BP sessions).

**Decision (2026-10-16):**
- **End is optional.** Left empty, the report covers the Start date only; End equal to Start means the same thing. Both work, so a single game takes one date, and someone who fills both boxes isn't penalized. The End box says "(optional)".
- **End before Start shows a message** instead of silently swapping the dates: a slip like typing 2025 for 2026 would otherwise turn into a year-long report without anyone noticing.
- **A doubleheader inside a range is included whole** (both games). The G1 / G2 choice only appears for a single date that has two games, and it gets a third choice, "both".
- **A range can cross seasons** (e.g. the summer league into the next spring): each season it touches is loaded, 1–2 s each the first time. Within one season it's just a filter on rows that are already loaded.
- **What a range report shows:** the totals across its games, plus one row per game (date, opponent, the game's key numbers) so a stretch can be read game by game. What goes in each is part of the report design (section 4).
- **The file-name area lists every game in the range** (date, opponent, file name, Verified / Unverified), collapsed when there are more than a few.
- **The unverified disclaimer** says how many of the games are unverified, e.g. *"2 of 6 games are unverified"*, and marks them in the per-game rows.
- **No games in the range:** a message with the player's nearest game dates before and after.
- **Uploads use the same fields:** the files' `Date` column filters them, which helps with a file holding several practice days.
- **Dates are typed as MM/DD/YYYY**, the same format the calendar shows.

### Where the report reads the game from: `games/` or `serving/`?

Matt's first thought was the per-game parquets in `pipeline_v2/games/`. Measured 2026-10-16:

| | `games/` (one file per game) | `serving/`, through the player's own rows |
|---|---|---|
| Speed | 6 ms to read one game file | The Pitchers tab already loads a player's season (~1.2 s the first time, then cached); one game is then a filter on those rows, effectively instant |
| What's in it | Every loaded game, **including 353 games the build holds back** because they duplicate a served game (unverified copies filed under another date or game number, the double-dated Riddle Pace game). The report could show the same outing twice, under two dates | Exactly what the site serves: one version of each game |
| RV / xRV / Eye | Not attached; the scoring steps would have to run again for the report | Already attached (`data.get_rows` joins the score tables) |
| File name | `source_file` column | The same `source_file` column, e.g. `raw/2026/06/23/20260622-CharlesSchwabField-1.csv` |
| Design rule | Breaks plan.md §3: the app reads only `serving/`. `games/` is the pipeline's working copy, rewritten while a run loads | Follows it |

**Decision (2026-10-16): `serving/`, through the player's rows.** Pick the player and type the date → load that season of the player's rows the way the Pitchers / Batters tabs do (cached) → the report is those rows filtered to the chosen game, or to the games in the date range. The only data change is adding `GameID` and `source_file` to the columns the reports read.

**Unverified games come with it.** `serving/` already holds one version of each game: the verified file when it's in, otherwise the unverified one (verified files arrive a median ~13 h later, at most ~19 days); held-back unverified files (broken pitch numbering, or a duplicate of another served game) wait for their verified file. So the report only has to:
- show a **disclaimer** when the game's rows have `is_verified = false` (for a range: how many of its games), e.g. *"Unverified game: TrackMan hasn't sent the verified file yet. Names, pitch types and results may still be corrected."*
- expect the report to change overnight when the verified file replaces it (names fixed on ~2% of pitches; some pitch IDs change in ~12% of games); RV / xRV / Eye are re-scored in the nightly run.

---

## 3. Reports from an uploaded file

**Decisions (2026-10-16):**
- **The uploaded file is the data:** a scrimmage, live BP or game file; the player dropdown lists its players, and reports use only its rows.
- **Everything is read by column name, never by position**, so a broken or reordered CSV still works: missing columns make the sections that need them say "not in this file", extra columns are ignored, and values that don't parse become blank (with a count shown).
- **Anyone can upload; uploads are session only:** held for that browser session, never saved, never visible to anyone else.
- **Accepted:** TrackMan game CSVs, TrackMan practice / live BP exports, and parquet. Matt is checking the practice exports' column names (see below).
- **A simple upload box capped at 70 MB** (decided after the 2 GB idea, which doesn't fit how the site is set up; see below), with the protections listed below.

### What the file layouts give us

TrackMan's game CSV has 170 columns (167 before 2025). The two live BP files in `backend/notebooks/` (Oct 9–10, `PitcherTeam` `SOU_TRO`: 16 pitchers, 20 batters) have **68**:
- **56 share a name with the game layout** (pitch metrics, release, movement, location, batted-ball metrics, player names and IDs).
- **12 are practice-only or named differently:** `PitchSession`, `Flag`, `CalibrationId`, `EffVelocity`, `PracticeType`, `Device`, `HitType` (game files have `TaggedHitType` / `AutoHitType`), and five `SpinAxis3d*` columns.
- **Missing:** `PitchCall`, `PlayResult`, `KorBB`, `Balls` / `Strikes`, `Inning` / `Outs`, `PitchUID`, `GameID`, `Level`, `BatterTeam`.

So a practice file supports pitch and batted-ball metrics, but not strike / whiff / chase rates, K / BB, RV or Eye. A game CSV supports everything; it goes through the same per-column cleaning as `pipeline_v2/load.py` (typo fixes, base states for run value). If the practice exports use other names for the same things, a small rename map (e.g. `HitType` → `TaggedHitType`) handles them.

### The size limit: why 2 GB doesn't fit as is

- **Cloudflare**, which sits in front of www.delispice.cc, rejects any request over **100 MB** on its free plan, so a bigger upload never reaches the server.
- **Dash's upload box** (`dcc.Upload`) reads the whole file in the visitor's browser and sends it as text, about a third bigger (base64). A 100 MB request is therefore a ~75 MB file at most, and a 2 GB file would likely crash the browser tab first.
- **Server memory:** the box has 14 GB, shared by the app (1.2 GB now, up to ~10 GB during heavy queries) and the nightly pipeline. Session-only uploads live in memory, so a few visitors each holding a large file at once could run it out.

For scale: a TrackMan game CSV is ~0.3 MB, a team's season ~16 MB, a whole D1 season ~2 GB.

| | Option | Max file | Costs |
|---|---|---|---|
| A | **Simple upload box** (`dcc.Upload`), capped at ~70 MB | ~70 MB (a team's season, a fall of practice files) | None beyond the cap |
| B | **Chunked upload** to a temporary file on the server's disk (sent in pieces under 100 MB, e.g. with the `dash-uploader` package), read without loading it all into memory, deleted when the session ends or after a few hours | 2 GB | A new dependency or upload route, disk cleanup, more to build and test |

Either way, add these protections: only `.csv` / `.parquet`; one upload per session (a new one replaces it); uploads expire after a couple of idle hours; a total memory budget across sessions (the oldest is dropped first); and a row cap, since a small parquet file can expand to many GB in memory.

**Decision (2026-10-16): A, capped at 70 MB**, with the protections above. Move to B only if whole-season uploads turn out to matter.

### Built (2026-10-19, local, not committed)

- **Where:** a "Data: Database games / Upload a file" choice next to "Report for". Upload hides Level / Conference / Team (the file is the data) and shows a drop box; the date fields and the player list then work on the file. After an upload, Start / End are set to the file's dates so its players show straight away.
- **Reading** (`game_report.read_upload`): every column matched by name against the 170-column TrackMan schema (plus our parquet's extra columns); unknown columns ignored; missing ones blank; values that don't read as their type blank and counted; the pipeline's typo fixes (`fix_dictionary`). A file without `PitchUID` / `GameID` gets made-up keys (one "game" per date). A full game file gets its base states (`baserunner.annotate_game`, as `load.py` does), so RV works; xRV runs the same way as for served rows and stays blank without a `Level`; Stuff+ is graded on the D1 scale whatever the `Level` (below).
- **What the report blanks:** each number lists the columns it needs; a file without them shows that number blank, and the report lists what's missing and why (e.g. a live BP export has no PitchCall, counts, innings or results, so IP, TBF, K %, BB %, whiff % and the model numbers are blank). With no `PitchCall`, every pitch with an exit velocity counts as a batted ball (fouls included), so Avg EV and Hard Hit % still work; the balls-in-play chart then doesn't mark hits vs outs.
- **Protections:** .csv / .parquet only; 70 MB (checked in the browser and again on the server); at most 1,000,000 rows; one upload per browser session (a new one replaces it); dropped after 2 idle hours; all sessions' uploads capped at 2 GB of memory together, oldest dropped first; a file that can't be read shows a message and keeps the previous upload. Uploads live in the app's memory only, so an app reload (the nightly run) clears them.
- **Stuff+ for uploads that aren't D1** (decided 2026-10-19 for no Level; extended 2026-10-23 to any other level, Matt's request): graded on the D1 scale automatically (the model's only scale; it grades D1 pitches only), with the model table titled "Stuff+ on the D1 scale" and a note. It isn't a sample-size question: the Oct 16 live BP file gets 324 of 360 pitches graded (the rest lack spin or movement), down to a 23-pitch outing. A file that names another level (e.g. a JUCO game file: 234 of 234 graded) is graded the same way, with a note naming its level; database games at other levels still keep Stuff+ blank. The run environment never changes Stuff+.
- **Not for uploads:** retags (the retag panel is hidden; an upload's pitches aren't ours to retag) and the "Which player?" split.
- **Tested** (locally): the UNC–Virginia game CSV from `raw/` reproduces the served report (Lynch 03/07/2026: 4.0 IP, 91 pitches, RV, xRV, Stuff+); the Oct 16 live BP export (57 of 68 columns recognized: pitch and batted-ball numbers, the rest blank with the notes); a served game exported as parquet; refusals for a non-TrackMan CSV, a .txt, a header-only file and a file over 70 MB.

---

## 4. Pitcher and batter reports

**Decisions (2026-10-16):**
- **Player-game reports only for now** (no whole-game view with both teams).
- **A Pitcher | Batter toggle** inside the Game Reports tab switches the player dropdown and the report.
- Pitchers and batters get **different reports**. The pitcher report is section 6; the batter report comes later.

---

## 5. Comparing with our data

**Decision (2026-10-16):** reports will offer all three: **the numbers alone**, **vs the player's own season**, and **vs his level**. Which numbers get which comparison is decided with each report's contents (the pitcher report is section 6). This applies to game reports and to uploads (for uploaded players who are in our data: TrackMan's `PitcherId` / `BatterId` find them exactly; in the Oct 9–10 live BP files that's 3 of 16 pitchers and 10 of 20 batters).

Example (from the live BP files): Rohan Kasanagottu's fastball averaged **89.6 mph**. Alone: *89.6*. vs his own season: *89.6 (spring 2026, D1: 88.4, so +1.2)*. vs his level: *89.6, with a D1 percentile next to it*.

### What it costs to keep these current with the nightly runs

Measured on the Mac, 2026-10-16 (the server runs the same queries somewhat slower).

**vs his own season: close to free.**
- A game report already loads the player's season rows to find the game (1–2 s the first time, then cached). Season averages are a group-by on those rows: ~1 ms for Ryan Lynch's 2,015 pitches.
- Nothing extra at night: the nightly run rebuilds `serving/` and reloads the app, which clears the cached rows, so the next report includes the new games.
- "Season so far, entering this game (or this date range)" or "the whole season" are the same rows with or without a date filter, so either costs the same.
- Uploads: one extra lookup of the player's season by ID (1–2 s the first time).
- No new files.

**vs his level: two ways.**

| | (a) Season percentiles (what the Pitchers tab shows today) | (b) Per-game pools (new) |
|---|---|---|
| What it compares | The game's number against every qualified player's **season** number (a 94 mph game vs D1 pitchers' season fastball averages) | The game's number against **every game** at that level (this outing's whiff rate vs all D1 outings), and/or against the level's season so far, as of any date |
| What's stored | One pool per level and season: D1 2026 is 5,563 pitchers, 333 KB | One row per player per game, holding sums (not rates): D1 2026 is 55,056 pitcher-games (1.4 MB) and 136,807 batter-games (2.9 MB). Every served season and level: ~270k pitcher-games and ~690k batter-games, roughly 20–50 MB depending on how many metrics |
| Nightly cost | None beyond today: the nightly run already deletes these pools and the first visitor of the day rebuilds them (~1 s for D1 2026). They could be built in the nightly run instead, so nobody waits | Rebuild the changed seasons: ~0.1 s per level-season |
| Per view | Instant once built | Instant: the level's season-so-far on any date is a sum over the pool, ~4 ms for every D1 pitcher |
| "As it stood that day" | No: the pool is always today's season, so a March game viewed in June compares against June's numbers | Yes, for any date, with no daily snapshots |
| New code | Little (reuse `data.percentile_pool`) | A pool builder (nightly, after `build`) and its reader |

Neither needs model retraining or any change to the pitch data; both are derived from `serving/`.

**Early season, either way:** level comparisons are thin at first. By the number of D1 pitchers who had faced 80+ batters (~20 IP, the qualifier the Pitchers tab uses): **Feb 22: 0**, Mar 8: 232, Mar 29: 1,073, Apr 26: 2,044, end of season: 2,553. So early on, level percentiles need a lower qualifier that grows with the season, a comparison against last season until enough players qualify, or a note that there's not enough data yet.

To decide with the report contents: (a) or (b); "entering this game" or "whole season"; and the early-season rule.

Still with Matt: the practice exports' column names (section 3).

---

## 6. Pitcher report

**Matt's design (2026-10-16).** It looks like the Pitchers tab and will grow as we build it. Top to bottom:

### Header

- Name, **RHP / LHP**, the biographic line (height, age, born, draft eligibility), team, and the **game date or date range**.
- Plus, from section 2: the file-name area and the unverified disclaimer.

*(Order as built, 2026-10-19: header, file area, line table (+ per-game rows for a range), arsenal and model tables, then the movement and release charts, the retag panel, and the location charts. Matt moved the tables above the charts.)*

### Line table (one row)

**IP · Pitches thrown · Strike % · Zone % · Pitches per PA · Batters faced · Batters retired · Walks + HBP · Strikeouts · Hits · Extension · K % · BB %**

- **Strike %** (added 2026-10-19) = strikes ÷ all pitches, where a strike is a called strike, a swinging strike, any foul (`FoulBall`, `FoulBallNotFieldable`, `FoulBallFieldable`) or a ball in play: `report.STRIKE_CALLS`, the Pitchers tab's definition, so both tabs agree. Balls, balls in the dirt, hit-by-pitches, intentional balls, `AutomaticBall` / `AutomaticStrike` and `Undefined` count as non-strikes.
- **Zone %** = pitches in the zone ÷ pitches with a plate location, using the zone the Pitcher and Batter reports already use (`report.ZONE`: 0.83 ft either side of the plate's center, 1.5 to 3.3775 ft high; the leaderboard's `ZONE_SQL` is the same box).

### Two charts, side by side

**Movement chart** (induced vertical break vs horizontal break, colored by pitch type), **with retagging of errant pitches**:
- Retags live in **their own JSON file**, `delispice_app/state/retags.json`, separate from the pitch data, the same file the Pitchers tab uses: one entry per retagged pitch, `pitch ID → {new type, pitcher}`, applied whenever a report reads the rows. **The raw data (`raw/`, `games/`, `serving/`) is never edited**; reverting deletes entries, and TrackMan's original pitch types come back.

**Release point chart**: release side (x) vs release height (y), each pitch colored by pitch type.
- **Arm angle, when the pitcher's height is known** (decided 2026-10-16):
  - **Each pitch gets its own arm angle.** The shoulder is estimated at **70% of the pitcher's height**, and the angle comes from the right triangle between the shoulder and the release point: `arm angle = atan((release height − 0.7 × height) / |release side|)`, in degrees **from horizontal** (0° = sidearm, 90° = straight over the top, negative = released below the shoulder).
  - **The arsenal table gets an Arm angle column:** the average of the pitches' angles, per pitch type.
  - **The chart gets one line: the average arm angle across all pitches**, starting at the **estimated shoulder, (0, 0.7 × height)** (changed 2026-10-19 from the chart's (0, 0)), a constant length, labeled with its degrees (e.g. `38°`). It points toward the pitcher's arm side (right for a RHP, left for a LHP, in the pitcher's view). Starting at the shoulder, the point the angle is measured from, puts the line through the cloud of release points, so it reads like the arm.
- Example: Ryan Lynch, 6-2 (shoulder 0.7 × 6.17 ft = 4.32 ft), releases at 5.16 ft high and 2.51 ft to the side: atan(0.84 / 2.51) ≈ **18.5°**, a low slot.
- Heights on file (local `heights.csv`, 2026-10-16): **71% of D1 2026 pitchers** (4,113 of 5,764) but **29% across all levels** (4,477 of 15,444). Without a height there's no arm angle (a blank column, no line), only the release points. Heights can be added by hand with the existing ✎ Edit height / birthday.

### Arsenal table (one row per pitch type)

**Pitch type · Pitch count with usage % in parentheses (e.g. `34 (52%)`) · Avg velo · Max velo · Avg spin · Strike % · Zone % · Whiff % · Avg IVB · Avg HB · Avg tilt · Avg extension · Arm angle · Hard-hit % · Avg EV · RV/100 · xRV/BBE · Stuff+**

- **Stuff+ waits:** it exists only in the research notebooks so far (`backend/research/stuff_model/`); Matt is writing the production module.
- **Always two tables** (decided 2026-10-16; revisit once it's built): the pitch data first, then a second table underneath with just the model numbers (RV/100, xRV/BBE, Stuff+). The screen and the PDF match.
- **Small samples:** the pitch count always shows; rates (zone %, whiff %, hard-hit %, RV/100, xRV/BBE) are blank for a pitch type thrown fewer than 5 times.
- Same definitions as the Pitchers tab: whiff % = swinging strikes ÷ swings; hard-hit = exit velocity ≥ 95 mph on batted balls; tilt = the circular average of the spin axis, shown as a clock time.

### Location charts (four, side by side)

**All pitches · vs LHB · vs RHB · Balls in play**, each pitch colored by its pitch type, drawn from the pitcher's view with the strike zone box. On the balls-in-play chart, hits are filled and outs hollow, and hovering shows exit velocity, launch angle and the result.

### Downloadable PDF

The report is meant to be downloadable as a PDF. How it's made (the browser's print-to-PDF, or built on the server with Plotly's `kaleido` and a PDF library) is decided after it's built locally.

### Built (2026-10-19, local, not committed)

`delispice_app/game_report.py` (the logic) and the **Game Reports** tab in `app.py` (after Batters): the picker band, the pitcher report, retags and the height / birthday edit. Clicked through locally: a single game, a March range (4 games, per-game table, files collapsed), a doubleheader (G1 / G2 / Both), a date with no game (nearest dates), an unverified game (banner, `_unverified.csv`), a pitcher with no height (blank arm angle), and a retag + "Revert this game" (`retags.json` came back byte-identical).

Where the build goes beyond or differs from the decisions above:
- **One name can be several players.** "Lynch, Ryan" is three TrackMan players (Notre Dame LHP 2022–24, Coastal Carolina RHP 2024–26, North Carolina RHP 2025–26); 2,505 of 46,216 pitcher-seasons share a name with another ID. The picker shows **"Which player?"** (team · hand · games) when a name has more than one TrackMan ID, preselects the most recent, and the report keeps only that ID's pitches. *(The Pitchers tab still merges them: its "Lynch, Ryan" 2026 report mixes the Coastal Carolina and North Carolina pitchers.)*
- **A game's date is the whole game's first pitch**, not the player's: a reliever who only pitched after midnight would otherwise move the game to the next day and fake a doubleheader.
- **The release chart is the Pitchers tab's** (`report.release_fig`): a line per pitch type from the shoulder plus an "Avg arm angle" label, rather than decision 6's single average line of constant length. *(Pending Matt's call.)* Its legend is clipped on the right in both tabs.
- **Labels follow the Pitchers tab's current names**: NP, TBF, BB + HBP, SO, plus P/PA for pitches per PA. The arsenal is `report.build_arsenal` without Strike % and with Arm Angle; the arsenal ends with RV/100; under it, side by side (wrapping on a narrow window; 2026-10-23), are the Contact table (`report.build_contact_table`: Avg EV, Hard Hit %, xRV/BBE) and the Stuff table (`report.build_stuff_rows`: Stuff+, Whiff+, Foul+, Contact+, each pitch opening to vs RHB / vs LHB; changed 2026-10-23).
- **⚙ Change run environment** (added 2026-10-19, Matt's request): the Pitchers tab's control under the name. It re-scores xRV and RV against the chosen (level, year), for database games and uploads alike; Stuff+ doesn't change (its prices are frozen in the model). For an upload with no `Level` (a practice export) it's how to get xRV; RV still needs a game file's base states.
- **"Revert this pitcher"** clears every retag under the name (`data.clear_retags`, same as the Pitchers tab), so it would include same-named players' retags too.

Not built yet: the batter report (the toggle shows a note), uploads (section 3), the PDF, comparisons (section 5).

### Decisions (2026-10-16)

1. **Opponent:** `vs` / `@ Opponent` next to the date; for a date range, in the per-game rows.
2. **Age:** on the game date (for a range, its first game); draft eligibility stays tied to that season's draft.
3. **Date range:** totals in the line table, with a per-game table right under it.
4. **Who can retag:** anyone on the public site, saved for everyone (as today).
5. **Retags:** in their own JSON file (above). Carried over from the recommendation unless Matt changes them: shared with the Pitchers tab (same pitch ID, same file); still blocked on unverified pitches (their IDs can change when the verified file arrives); revert buttons for "this game" and "this pitcher"; no AutoCluster here.
   - Because anyone can retag, the Pitchers tab's **"Reset all"** lets any visitor wipe every pitcher's retags. Leaving it off this page, and the nightly backup of `retags.json` planned in plan.md phase 9, limit the damage.
6. **Arm angle:** each pitch's angle from horizontal, averaged per pitch type in the arsenal table; one line on the chart for the average across all pitches, from the estimated shoulder (0, 0.7 × height), constant length, labeled in degrees (above). *(Changed 2026-10-19: the line started at (0, 0).)*
7. **Orientation:** the pitcher's view for the movement, release and location charts.
8. **Balls in play:** hits filled, outs hollow; exit velocity, launch angle and result on hover.
9. **Arsenal:** always two tables; revisit once built.
10. **PDF:** decided after building it locally.
11. **Small samples:** rates blank under 5 pitches of a type; the count always shows.
12. **Comparisons** (section 5): added later as the report grows.

---

## Later

- The batter report (section 4); more of the pitcher report as it's built (section 6), including which numbers get which comparison (section 5).
- Where the Game Reports tab sits in the tab bar.
