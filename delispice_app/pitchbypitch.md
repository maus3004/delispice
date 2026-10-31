# Pitch by pitch: design

A pitch-by-pitch section in the Game Reports **pitcher report**, modeled on TrackMan's iPad pitch screen: a log of every pitch on the left, and clicking one opens its card (grade and shape). Designed and **built 2026-10-24** (local, not committed); see "Built" at the end. The rest of the report is in `game_report_decisions.md`.

---

## Decided (2026-10-24, Matt's design)

- **Look:** as close to TrackMan's iPad pitch screen as we can get (mapped piece by piece below).
- **Where:** in the pitcher report, **under the four location charts** (after the `gr-loc-*` row in `app.py`). It reads the report's own rows, so it's already filtered to one pitcher (one `PitcherId`) and the chosen game or date range.
- **Left: the pitch log.** Clicking a row opens that pitch's card.
- **The card:** grade, shape, count, result, EV/LA, RV, and xRV on batted balls (Q8).
- **Grades: only the grade vs the side the batter stood on for that pitch** (Q1). *(Changed 2026-10-24: the first plan showed the overall grade plus the split.)*
- **Order:**
  - **Game files** (database games, uploaded game CSVs) are **grouped by inning**.
  - **Live BP and practice exports** are in **sequential order**.
  - The split follows the upload's existing `kind` (`"game"` vs `"pitch data"` in `game_report.read_upload`). Database games are always game files.
- **Spin panel: speed, spin and tilt only** (option A in "Spin numbers we don't have").
- **Code: its own file, `delispice_app/pitch_by_pitch.py`** (no Dash), with the layout and callbacks in `app.py`. That's the same split as decision 1 in `game_report_decisions.md`; it isn't added to `game_report.py`.
- **Arm angle, when the pitcher's height is known** (added 2026-10-24, after the build):
  - **The formula** is the one the release chart and the arsenal's Arm Angle column use. The shoulder sits at 70% of height, and the angle is atan(release height above the shoulder / |release side|).
  - **On the card:** an "Arm angle" tile in the release panel (e.g. 13°, marked est.), and a line on the release chart from the estimated shoulder to this pitch's release point.
  - **Without a height,** neither is shown.
- **The Stuff table's "(click a pitch for vs RHB / vs LHB)" hint is removed** (2026-10-24). The table is shared, so it's gone from the Pitchers tab and the Pitch Lab too.

### The questions, answered (2026-10-24)

1. **Split grade:** only the batter's side for that pitch. A slider to a LHB shows its vs LHB grades and nothing else. There is no overall grade and no other side.
   - **Fallback** (decided 2026-10-24): with no batter named (practice warmups) or no split on file (~0.08% of rows), the card shows the overall grade labeled "overall", so the box isn't empty.
2. **Grades on the card:** all four (Stuff+, Whiff+, Foul+, Contact+), each vs the batter's side, with **Stuff+ as the headline**.
3. **The charts behind the selected pitch:** (b), the selected pitch on top of his other pitches of the same type, faded. *(Changed 2026-10-24 after Matt's review: the toggle to show only the selected pitch is removed, so (b) is the only view.)*
4. **The log's big number:** a dropdown like TrackMan's "Pitch Speed ▾", **defaulting to velo**. The options are Velo, Stuff+, Spin, IVB and HB; Stuff+ there is the batter's-side grade, the same as the card.
5. **Order:** first pitch on top.
6. **Batter sub-headers** inside each inning: yes (name, side, and how the PA ended).
7. **Practice warmups:** under "Warmup" / "Live" headers. This needs `PitchSession` kept on upload (below).
   - Warmup starts collapsed (decided 2026-10-24).
8. **On the card:** grade, shape, **count, result, EV/LA, RV, and xRV on batted balls** from our xRV model.
   - RV and xRV use the same sign as the report's tables, which are offense-positive: −0.33 for a strikeout, so negative is good for the pitcher.
   - Both follow the report's ⚙ Change run environment control.
9. **Retag from the card:** yes, written to `retags.json`. A pitch-type dropdown in the Tag box, with the lasso's rules:
   - blocked on unverified pitches;
   - the existing "Revert this game" / "Revert this pitcher" buttons;
   - hidden for uploads.
10. **Units:** feet and inches for release height, release side and extension (6'3"), the way TrackMan shows them. Movement stays in inches with one decimal.

---

## The TrackMan screen, piece by piece

| TrackMan | What it shows | Ours |
|---|---|---|
| Top bar | Session date and time, pitcher, hand, TrackMan ID | Already in the report header, so the section doesn't repeat it |
| **Pitches** column | One row per pitch: a pitch-type dot, a big number (pitch speed, chosen in a "Pitch Speed ▾" dropdown), and a small `#42`. Newest pitch on top. A Pitch / Group toggle | **The pitch log** (below). First pitch on top; no Group toggle in v1 |
| **Release (ft)** | The pitch's release point, pitcher's view. Tiles: release height 6'3", release side 2'8", extension 6'7" | Same: `RelHeight`, `RelSide`, `Extension`, in feet and inches |
| **Movement (in)** | IVB vs HB, pitcher's view. Tiles: I. Vert Mov 2.6, Horz Mov −6.4 | Same: `InducedVertBreak`, `HorzBreak` |
| **Location (ft)** | Strike zone with a 3×3 grid, and the pitch | Same: `report.ZONE` plus a 3×3 grid, from `PlateLocSide` / `PlateLocHeight` |
| **Ball spin (tilt)** | A 3D ball with a tilt clock. Tiles: pitch speed, total spin, measured tilt, efficiency, active spin, gyro | Speed, spin and tilt only (option A below), with a flat clock face instead of the 3D ball |
| Right column | Tags (SL, Slider), flag, pitch set, details (pitcher, date, time) | The tag (with retag), the model's pitch group, **the grades**, and the outcome |

### Layout sketch

```
┌ Pitch by pitch ──────────────────────────────────────────────────────────────────┐
│ PITCHES       │ RELEASE (ft)           │ MOVEMENT (in)          │ PITCH          │
│ Show: Velo ▾  │ Rel height  6'3"    ●  │             I. Vert 2.6│ SL  Slider   ▾ │
│               │ Rel side    2'8"       │       ●     Horz  −6.4 │ BB group       │
│               │ Extension   6'7"       │                        │────────────────│
│───────────────│────────────────────────┼────────────────────────│ vs LHB         │
│ Top 1st       │ LOCATION (ft)          │ SPIN                   │ STUFF+     108 │
│  Smith (L) K  │   ┌──┬──┬──┐           │ Speed  83.6 mph        │ Whiff+ 112     │
│ ●  93.7   #1  │   ├──┼──┼──┤  ●        │ Spin   2209 rpm    ◷   │ Foul+   97     │
│ ●  83.6   #2 ◀│   ├──┼──┼──┤           │ Tilt   9:30            │ Contact+ 101   │
│  Lee (R) 1B   │   └──┴──┴──┘           │                        │────────────────│
│ ●  74.1   #3  │                        │                        │ 1-1 · In play  │
│ …             │                        │                        │ Single         │
│               │                        │                        │ 98 mph, 12°    │
│               │                        │                        │ RV +0.41       │
│               │                        │                        │ xRV +0.29      │
└──────────────────────────────────────────────────────────────────────────────────┘
```

- **Widths:** roughly 180 px for the log, 2 × 380 px for the panels and 170 px for the right column, so about 1,100 px in all.
- **Scrolling:** the log scrolls on its own and the card stays put.

---

## The pitch log

- **A row:**
  - a pitch-type dot, in the same colors as the report's charts (`report.PITCH_COLORS`);
  - a big number, the dropdown's metric (velo by default);
  - a small `#n`, his n-th pitch of the outing (1 … NP). TrackMan's `PitchNo` counts both teams' pitches, so it isn't his count.
- **Game files:**
  - an inning header for each half he pitched ("Top 1st");
  - a batter sub-header for each PA (name, side, and how the PA ended);
  - pitches ordered by `PitchNo` within the game, first pitch on top.
- **Practice files:** "Warmup" and "Live" headers (from `PitchSession`), with pitches in `PitchNo` order inside each (or `Time` when it's missing). Warmup starts collapsed. A file without `PitchSession` is one sequential list.
- **A date range:** a game header (date and opponent) above each game's innings. A practice file holding several dates gets a date header for each.
- **The selected row** is highlighted the way TrackMan does it (a light blue row and a ring on the dot). The first pitch is selected when the report opens.

## The pitch card

- **Release, movement, location:** one small Plotly chart each, pitcher's view, the same axes as TrackMan's (release −4…4 ft by 0…7 ft; movement ±16 in or wider). The tiles sit beside the chart. With a height on file, the release panel adds the arm angle tile and the shoulder-to-release line.
  - The selected pitch is drawn large with a ring, over his other pitches of the same type, faded.
- **Spin:** speed (one decimal), total spin (rpm), and tilt as a clock time with a clock-face graphic. The tilt is `SpinAxis` through `report.spin_clock`, which gives the same value as TrackMan's own `Tilt` column (checked 2026-10-24: 9:45 = 9:45), so `Tilt` isn't loaded.
- **Right column, top to bottom:**
  - **Tag:** the pitch type, with a retag dropdown (Q9), and the Stuff+ model's group (FB, BB or OS).
  - **Grades vs the batter's side:** labeled "vs LHB" or "vs RHB". Stuff+ is large; Whiff+, Foul+ and Contact+ are under it.
  - **Outcome:**
    - the count, and the pitch call;
    - the play result;
    - EV and LA when the ball was put in play;
    - RV;
    - xRV when the ball was put in play.

---

## Data

### Already on every row (`data.get_rows`, then `game_report.game_rows`)

- **Shape:** `RelSpeed`, `SpinRate`, `SpinAxis`, `RelHeight`, `RelSide`, `Extension`, `InducedVertBreak`, `HorzBreak`.
- **Location and outcome:** `PlateLocSide`, `PlateLocHeight`, `BatterSide`, `Balls`, `Strikes`, `PitchCall`, `PlayResult`, `ExitSpeed`, `Angle`.
- **Grades:** `StuffRHB` / `StuffLHB`, and the same for `WhiffPlus`, `FoulPlus` and `ContactPlus`. The overall `Stuff` (and the other three) is the fallback. `StuffGroup` is the model's group.
- **Run values:** `RV` (every pitch) and `xRV` (batted balls), both re-scored by the run environment control.

### Added for this section

- **`PITCHER_COLS`** (`data.py:44`) needs `Batter`, `Inning`, `Top/Bottom`, `PAofInning`, `PitchNo` and `Time`.
  - All six are in `serving/` (checked 2026-10-24).
  - The Pitchers tab reads the same list, so its cached rows get a little bigger. That's the simplest route.
- **`PitchSession`** goes into `game_report._EXTRA`, so `read_upload` keeps it for the Warmup / Live headers (Q7). It isn't a game-file column, so the parser drops it today.
- **Uploaded game CSVs** already carry the game columns.
- **Practice exports** have `PitchNo`, `Time`, `Batter`, `BatterSide` and `PitchSession`, but no inning, outs or count.

### Which grade a pitch gets

- `BatterSide` = Right → `StuffRHB`, `WhiffPlusRHB`, `FoulPlusRHB`, `ContactPlusRHB`; Left → the `…LHB` four.
- **Only when a batter is named.** Practice warmups have a `BatterSide` with no batter: in the Oct 17 exports, 233 warmup pitches have 0 batters but 157 are marked Right and 76 Left. Those, and the ~0.08% of rows without a split, show the overall grade labeled "overall" (Q1's fallback).

### When a number is blank, the card says why

- **A database game outside D1:** grades stay blank there (decisions §3). Uploads are graded on the D1 scale, with the existing note.
- **A pitch the model can't grade** (no spin or movement): 36 of 360 pitches in the Oct 16 live BP.
- **RV** needs a game file's base states, so it's blank for practice exports.
- **xRV** is blank for anything not put in play, and for a practice export with no `Level` unless the run environment is set (decisions §6).

### Stuff+ group ≠ pitch tag

The grade comes from the model's own pitch groups, not from `TaggedPitchType`, so a retag changes neither the grade nor the group. A cutter is graded as BB. The card shows the group (`StuffGroup`: FB, BB or OS), so a "Cutter · BB" reads correctly.

---

## Spin numbers we don't have

TrackMan's spin panel also shows **efficiency, active spin, gyro and measured tilt**. Neither of our data sources has them:

- **Game CSVs:** there are no `SpinAxis3d*` columns in `serving/`'s 179-column schema.
- **Practice exports:** the five `SpinAxis3d*` columns are present but **empty**. 0 of the 766 pitches in the two Oct 17 exports are filled.

| | Option | Good | Costs |
|---|---|---|---|
| A | **Leave them out**: speed, spin and tilt only | Every number on the card is TrackMan's own measurement | The panel has 3 tiles, not 6 |
| B | Estimate efficiency from the movement (spin needed for the observed break ÷ total spin) and label it "est." | Fills the panel | Next to TrackMan's look, an estimate reads like their measurement. It's a physics estimate with real error (seam effects) |
| C | Show them only when the file has them filled | Ready for exports that do carry them | Needs `read_upload` to keep the columns. Today's files would still show nothing |

**Decision (2026-10-24): A.**

---

## Later, not v1

- ↑ / ↓ keys to step through pitches.
- TrackMan's Group toggle (the log grouped by pitch type).
- A phone layout. Below about 500 px the release panel itself is wider than the screen.

---

## Built (2026-10-24, local, not committed)

### Where the code is

- **`delispice_app/pitch_by_pitch.py`** (no Dash):
  - `pitch_store` turns the report's rows into one table in log order, kept column by column so the browser store stays small (34 KB for an 89-pitch outing).
  - `log_tree` builds the games → innings / sessions → batters structure.
  - `card` holds the card's display strings; `release_fig`, `movement_fig`, `location_fig` and `tilt_fig` draw the four charts.
- **`app.py`:**
  - `pbp_section` sits under the `gr-loc-*` row.
  - `cb_gr_build` fills `pbp-store`.
  - The callbacks are `cb_pbp_log`, `cb_pbp_click`, `cb_pbp_card` and `cb_pbp_retag`.
  - The log rows are styled `html.Button`s with pattern-matching IDs.
  - A click only moves the highlight and redraws the card. Nothing is re-read or re-scored.
- **`assets/pitcher.css`:** the log's `.pbp-*` styles (the selected row's light blue and ring).
- **`data.PITCHER_COLS`** now also loads `Batter`, `Inning`, `Top/Bottom`, `PAofInning`, `PitchNo` and `Time`. The row query quotes its column names because of `Top/Bottom`.
- **`game_report._EXTRA`** keeps `PitchSession` on uploads.

### Beyond or different from the design

- **"Undo retag"** shows on a retagged pitch's card. It removes just that pitch's entry from `retags.json`. The panel's "Revert this game" / "Revert this pitcher" still work.
- **The location panel has no tiles**, as in TrackMan.
- **Hovering a log row** shows its pitch type, count and call.
- **A retag keeps the same pitch selected** while the whole report redraws.
- **In a date range,** "his others of its type" means every game in the range.
- **Tile labels** (changed 2026-10-24, Matt's request):
  - They're in the app's maroon, not TrackMan's orange. The Tag, Stuff+ and Outcome labels share that style.
  - Movement uses the arsenal table's names, "Vert Break" and "Horz Break", not TrackMan's "I. Vert Mov" / "Horz Mov", which read as cut off.
  - Labels never wrap ("Release height" used to). A panel's tiles all widen to its longest label.
- **Nothing is cut off at any window width** (changed 2026-10-24, Matt's request). The section is about 1,220 px wide, and it used to scroll sideways like the other chart rows, so under about 1,265 px the right column ran off the edge. Now it wraps:
  - **Under about 1,265 px:** Tag / Stuff+ / Outcome drop under the panels.
  - **Under about 1,050 px:** the panels drop under the log.
  - **Under 900 px:** the panels go to one column (`.pbp-grid` in pitcher.css).
  - **Batter names** in the log wrap instead of being cut with "…".
- **Number lines have room above the digits** (2026-10-24). The tile values were at line height 1.15 and the Stuff+ grade at 1.1, and Matt's browser shaved the tops of the 8s, 6s and 0s. The preview's Chromium didn't reproduce it. They're now at 1.35 and 1.3, with about 8 px and 10 px of room above the digits, so a renderer that trims text to its line box has nothing to cut.
  - **Checked** with a script on the 06/05–06/21 range (12 pitches across the 3 games, at 1,440 / 1,280 / 1,100 / 960 / 820 px). It looked for text wider than its box, boxes spilling out of their panel, chart labels outside the chart, and anything past the window edge, and found nothing. The only flag was the "Velo" dropdown's own focus input, which is transparent.
- **Arm angle:**
  - `pitch_store(..., height_in=)` gives each pitch an `arm` value and keeps the estimated `shoulder`.
  - Editing the height (✎ Edit height / birthday) rebuilds the store.
  - Checked on Lynch 06/05/2026 (6-2): the per-pitch angles average to the arsenal's numbers exactly (fastball 13.2°, slider 8.8°, changeup 8.5°).

- **Stuff+ distribution chart** (added 2026-10-29, Matt's request). It sits right above the pitch-by-pitch section and is drawn like the Pitchers tab's velocity chart. Both now use `report.distribution_fig`, and the velo chart's figure is unchanged (checked byte for byte).
  - **What it plots:** one curve per pitch type, scaled by usage, plus a dashed All Pitches total, of each pitch's overall Stuff+. It uses the overall grade, not the batter's-side grade on the card.
  - **Which pitches:** a pitch type keeps only the pitches graded in its majority Stuff+ group, the same pitches the Stuff table averages, since a grade means something only within its group.
  - **Extras:** a dotted line at 100 (D1 average). A pitch type needs 5 graded pitches to get a curve. With nothing to draw (e.g. a D2 game), the chart shows a note instead.
  - **Code:** `game_report.stuff_dist_fig`.

### Tested

**In the app** (Lynch, Ryan, North Carolina):
- the 06/05/2026 game: 89 pitches, inning and batter headers with how each PA ended, and batter-side grades;
- clicking a row;
- the big-number dropdown (Stuff+), with the selection kept;
- the same-type faded charts (the "this pitch only" toggle was tried, then removed);
- a retag to Cutter and Undo: the grade and group are unchanged, and `retags.json` came back byte-identical;
- the 06/05–06/21 range: 220 pitches under 3 game headers.

**In Python, through the same functions the page calls:**
- the Oct 17 live BP export: two Warmup / Live runs, Warmup collapsed, the first Live pitch selected, the overall grade on warmups, and the "no spin or movement" note;
- a D2 game: blank grades with "Stuff+ is graded for D1 games only.";
- a doubleheader with "Both" picked: G1 / G2 game headers, and the pitch numbers restart at 1 in each game.

**Not tested:** an uploaded game CSV, and the run environment control. Both feed the same rows the store reads.

**Q3 settled** (2026-10-24): Matt kept the "this pitch over his others of its type" look, with no toggle.
