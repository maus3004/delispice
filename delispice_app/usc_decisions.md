# USC tab: decisions

The **USC** tab runs on Matt's Mac and, since 2026-11-18, on the site for the signed-in owner only (guests never get it: local tabs aren't guest tabs, see `auth.py`). Since 2026-11-17 it opens on two clickable cards, **Pitcher Dive** and **MAC** (Matt's call). A card opens its view; **← Back to USC** at the top of a view returns to the cards. The open view lives in the `usc-view` store (`home` = the cards); leaving the tab and coming back keeps it. The cards' look is in `assets/usc.css`.

- **Pitcher Dive** is everything this file describes: it pairs hand-charted CSVs with TrackMan data (a practice export or a game) and builds a pitcher report from both.
- **MAC** is the matchup calculator, which used to be its own top tab. Its decisions live in `backend/research/mac/mac.md` §10; it moved over unchanged (`mac_tab.panels(m, switch="usc-view", prop="data")`).

Every decision here is Matt's unless it says otherwise.

**Tracked since 2026-11-18 (Matt's call).** This file, `local_tabs.py` (the layout and callbacks), `usc.py` (the logic), `mac_tab.py` and `assets/usc.css` were Mac-only (in `.git/info/exclude`) until then; now they're in git and on the server, and the site shows the tab only to the owner. `app.py` still loads `local_tabs.py` through an optional hook that skips it when the file is missing (8fec58a, 2026-10-31). The MAC engine stays on the Mac (`backend/research/` is git-ignored), so on the server the MAC card says it isn't on that machine.

---

## 1. Data

- **Sources** (a picker at the top):
  - **Upload files** (the default): one box. Usually one TrackMan file plus its chart file. With several, a chart goes with the TrackMan file whose Date matches the date in the chart's name ("scoutday 10:30" = Oct 30), then the one that pairs the most rows.
  - **Database games:** a team (USC Trojans by default) and one or more of its games, scored straight from the database. *(Until 2026-11-19 a chart could be added here too; removed with Loc%, Matt's call.)*
- **Chart files:** a CSV with a `ChartPitcher` column, which is how a chart is told apart from a TrackMan file. Columns: `ChartPitcher, ChartBatter, ChartPitchType, PitchCall, PlayResult, Notes` (an `IntendedLocation` column is read but no longer used). Every column gets a `Chart` prefix (`PitchCall` → `ChartPitchCall`) so nothing collides with TrackMan's.
- **Names:** charts use TrackMan's full names, "Last, First" (decided 2026-10-31). Older charts with last names only still pair.
- **Practice files:** only the `Live` rows are compared (`PitchSession`), because warmups are mixed in between live pitches.

## 2. Chart values → TrackMan's

Matching ignores case and spaces. A value that isn't recognized becomes Undefined and is listed in orange above the results.

| Chart | TrackMan |
|---|---|
| PitchCall `Foul` | `FoulBallNotFieldable` (a chart can't tell fieldable from not; 98.5% of 2026 D1 fouls are NotFieldable, and every delispice calculation counts the three foul calls the same) |
| PitchCall `CalledStrike` | `StrikeCalled` |
| PitchCall blank + PlayResult `HBP` | PitchCall `HitByPitch` |
| PlayResult `SO` / `Walk` | KorBB `Strikeout` / `Walk` |
| PlayResult `Single`, `Double`, `Triple`, `HR`, `FC`, `Error` | PlayResult `Single`, `Double`, `Triple`, `HomeRun`, `FieldersChoice`, `Error` |
| PlayResult `Flyout` / `Groundout` / `Popout` / `Lineout` | PlayResult `Out` + TaggedHitType `FlyBall` / `GroundBall` / `Popup` / `LineDrive` |
| PlayResult `Sac`, `SacFly`, `SacBunt` | PlayResult `Sacrifice` (+ `FlyBall` / `Bunt`) |
| PlayResult `good loc` | moved to Notes |
| Pitch type `FourSeamFastball` | `FourSeamFastBall` (the schema's spelling) |

## 3. Pairing

The files don't line up row for row: TrackMan misses single pitches and whole plate appearances. So they're paired **plate appearance by plate appearance** (decided 2026-10-31):

1. Split each file into plate appearances: runs of the same pitcher and batter.
2. Line the two lists up in order, like a text diff. Names match exactly, or at an 80% letter match (Gonzales = Gonzalez).
3. Same pitch count: pair in order. Different count: keep the best-scoring alignment. Same pitch-type group +2, different −1. TrackMan exit velocity with charted contact +2, without −3. Each pitch left unpaired −2. On a tie, the earliest pitch is left out.
4. Unmatched pitches stay in as "chart only" or "TrackMan only".

- **A plate appearance whose pitcher differs between the files doesn't pair.** No fix is built; fix the chart instead. The scout day chart's Johnson–Orr plate appearance was changed to Govel–Orr: TrackMan shows Govel warming up right before it.
- **Scout day files** (`~/Desktop/usc/scoutday 10:30.csv` + `LiveBpPitching_2026-10-31T005003_verified.csv`): 250 paired, 17 chart only, 0 TrackMan only.

## 4. The report's numbers

**TrackMan first; the chart fills in only what TrackMan doesn't have** (decided 2026-10-31). In `usc.report_rows`:

- **Calls and results** (PitchCall, KorBB, PlayResult, TaggedHitType): TrackMan's where the file has them (game files), the chart's otherwise (practice files have none). The upload reader's stand-in PitchCall ("InPlay" wherever there's an exit velocity) is dropped for USC uploads.
- **Pitch type** of a pitch TrackMan missed: the chart's, translated into that pitcher's TrackMan tags, so a charted FourSeamFastBall joins TrackMan's "Fastball" row.
- **Count, PitchofPA and outs:** the file's when it has them. Otherwise they're rebuilt from the chart's sequence. A plate appearance ends on a strikeout, walk, ball in play or hit by pitch, or when the pitcher or batter changes. Out, Sacrifice and FieldersChoice count 1 out each; double plays aren't charted, so IP can come out low.
- **Database games** are scored like Game Reports: retags, xRV, RV, Stuff+.
- **No small-sample rule:** rates show for every pitch type, however few were thrown (decided 2026-10-31, in Game Reports too).

## 5. Report layout

Top to bottom:

1. **Pitcher** dropdown: pitchers in the order they first threw, with pitch counts.
2. The pitcher's name.
3. **Line table:** Game Reports' line. Its Strike %, Zone %, Zone-Pre2S%, Chase% and IZ-Whiff% are colored against the D1 all-pitch numbers (section 7).
4. **One row:**
   - the **Stuff table**, titled just "Stuff" (the "Stuff+ on the D1 scale" note for a Level-less file was dropped, 2026-11-04, Matt), with every pitch's vs RHB and vs LHB rows open by default. Each grade cell is colored on a gradient (2026-10-31, USC only; first as 10-point bands, then a gradient at Matt's request): white at 100 (since 2026-11-05, the average P4 arm's pitch type vs P4 hitters), fading to green above and red below. The color is full at 130 and 70 (3 SD; 10 points = 1 SD); the full colors are a spreadsheet's red-white-green (`#63be7b`, `#f8696b`);
   - the **movement** chart;
   - the **release point** chart.
5. **Arsenal:** Game Reports' arsenal without RV/100.
6. **Stuff+ distribution** (added 2026-11-05, Matt; moved above pitch by pitch 2026-11-06): Game Reports' chart (`game_report.stuff_dist_fig`), drawn like the velo chart. One usage-weighted curve per pitch type plus a dashed total, using only graded pitches in the type's main Stuff+ group; a type needs 5 graded pitches to draw. It isn't in the reports PDF, same as pitch by pitch.
7. **Why this grade** (added 2026-11-06, Matt): a Pitch dropdown, the What, Why and Who explanations of that Stuff table row, and a Stuff+ grid on any two of the Why inputs (section 9). It isn't in the reports PDF.
8. **Pitch by pitch** (added 2026-10-31): Game Reports' widget (pitch log, the selected pitch's release / movement / location / spin panels, tag, grades, outcome).
   - Same code (`pitch_by_pitch.py`), with its own `usc-pbp-*` ids so the two tabs' clicks never cross, and **no retag controls**.
   - The log lists every pitch in paired order, grouped by batter, with how each plate appearance ended. In a practice file the plate appearances come from the chart; in a game, the sections are innings.
   - A pitch only the chart has sits where it was thrown, shows "—" for its numbers, and its card says "Charted only: TrackMan didn't track this pitch". It gets the batter's side from his tracked pitches.

- **Alignment:** the line table, the chart row and the arsenal share the same left and right edges.
- **No sideways scrolling** (2026-10-31): the block fills the window, up to 1,500 px. The Stuff table keeps its width and the two charts share the rest; the release chart gets a little more, since it has a legend. At 1,280 px the row fits with no scroll.
- **Which pitches:** the tables count every pitch, including ones TrackMan missed. The charts show only pitches TrackMan measured.
- **Below the report** (uploads only): the pairing check (summary, the plate appearances that didn't line up, a rows preview) and a CSV download of the paired rows.

## 6. Loc% (removed 2026-11-19)

Loc% (did a pitch cross inside its charted `IntendedLocation` target), the pitch card's "Intended location" box and its teal target shading, and the chart upload under Database games were all removed on 2026-11-19: Matt won't be charting targets any more. The chart stays for uploads, for pitch results and the pitches TrackMan missed. The definitions (3×3 zone squares, a 3 in margin, ExpandOutside 5 in past the edge; the side measured from the batter) are in git history before that date if they're ever wanted back.

## 7. League reference and the arsenal's rate colors

The arsenal's six rate cells (Strike %, Zone %, Zone-Pre2S%, Chase%, Whiff%, IZ-Whiff%) are colored against the 2026 D1 league (2026-10-31).

- **File:** `pipeline_v2/artifacts/usc_league_D1_2026.json`, git-ignored, next to Pitch Lab's reference. The build command is at the top of `usc.py`: `.venv/bin/python -m delispice_app.usc --build` (about a second; `--year 2027` for another season). The report reads it with `usc.league_reference()`.
- **What's in it:** per pitch type and overall, for each of the six rates:
  - **avg:** the pooled D1 rate, where every pitch counts once.
  - **sd:** the spread of D1 pitchers' own rates for that pitch type, counting pitchers with 20+ pitches in the rate's denominator (`LEAGUE_MIN`).
- **How:** 2026 D1, verified games, both serving partitions, by the report's own definitions. An independent recount matched all six overall averages.
- **Combined pitch types** (Matt): Four-Seam with Fastball, Two-Seam with Sinker, Sweeper with Slider (`LEAGUE_GROUPS`). An arsenal row is compared with its combined type.
- **Colors:** the Stuff table's gradient. White at the D1 average for that pitch type, green above, red below, full at 3 SD (`LEAGUE_FULL_SD`). Higher is green for all six rates.
- **2026 D1 overall:** Strike 60.7% ± 6.4, Zone 42.9% ± 5.9, Zone-Pre2S 44.9% ± 6.6, Chase 25.7% ± 6.0, Whiff 24.0% ± 7.1, IZ-Whiff 15.1% ± 5.7 (avg ± SD; 1,993,314 pitches).
- **Small samples:** with no minimum-pitch rule, a pitch thrown 2–3 times can show 0% or 100% and take full color.

## 8. Pitcher reports PDF

A **Generate pitcher reports** button sits next to the Pitcher dropdown (2026-10-31). It replaced the earlier Download PDF / Download JPEG buttons.

- **What it makes:** one PDF with every pitcher's report, one 17 × 11 in landscape page each, like slides, in the dropdown's order. A page has the name, the line table, the Stuff table with the movement and release charts, and the arsenal, colored as on screen. The pitch by pitch and everything below it are left out. A report taller than a page (many pitch types) is scaled down to fit its page.
- **Where it goes:** in a browser (the site, or browser mode on the Mac) it downloads as `USC_pitcher_reports_<date>.pdf` (2026-11-18). In the desktop applet it's saved to `~/Downloads/USC_pitcher_reports_<date>.pdf` on the Mac and opened in Preview; a name that's taken gets `-2`, `-3`.
  - **Why the applet saves it:** the desktop applet (`launch.py`, a pywebview window) ignores browser downloads, which is why the first download buttons saved nothing. `launch.py` sets `DELISPICE_DESKTOP=1`, which picks the save-and-open path.
- **How:** the same components as the screen (`_report_view(..., export=True)`) at fixed sizes, 1,500 px wide, written out as one HTML page with a page break per pitcher. The app's CSS and Plotly are inline, and headless Chrome prints it (`usc.render`), about 3 s for 9 pitchers. The movement chart's WebGL points become plain scatter, since WebGL prints blank. Chrome uses a throwaway profile, so an open Chrome window isn't touched.
- **Needs** Chrome or Chromium on the machine running the app (`usc.chrome()`: `$DELISPICE_CHROME`, the Mac app at `/Applications/Google Chrome.app`, or `google-chrome` / `chromium` on the PATH). Without one, the button says so.
- **The paired-rows CSV button** uses a browser download: it works on the site and in browser mode, not in the applet.

## 9. Why this grade

The app's version of `why()` from `backend/research/stuff_model/stuff_explain.ipynb` §10 (added 2026-11-06, Matt), under the Stuff+ distribution. That notebook section has the reasoning and its audit. Code: `usc.explain` and its helpers, drawn by `local_tabs._why_view`.

- **What you pick:** a row of the Stuff table, in its order. The dropdown keeps the same pitch when the next pitcher throws it. A row is every Live pitch with that tag in the row's main Stuff+ group, plain average of each pitch's own grade, so it matches the table. Pitches only the chart has can't be graded and aren't in it, which is why the dropdown's count can be higher than the graded count.
- **What it shows:** the grade and one sentence, then three boxes:
  - **What:** from 100 to the grade through whiffs, fouls and contact. Adds up exactly.
  - **Why:** from the average of that pitch type to the grade through velo, IVB, HB, spin, release height and side, and for a pitch that isn't the main fastball, its fastball velo, IVB and HB deltas (this pitch minus its fastball, as the model sees them; the average in brackets is the average delta, Matt, 2026-11-06). Exact Shapley values of the row's average shape vs. the average shape of its pitch type. A "typical numbers" line and an "everything else" line close the gap. Adds up exactly.
    - **What the lines split (changed 2026-11-06):** the nine are the model's own inputs, so a delta line earns the credit for the whole delta, and a velo, IVB or HB line holds its delta where it is. The notebook's `why()` splits it differently (its "its fastball's velo" line moves only the fastball), so its velo, IVB, HB and fastball lines don't match the tab's line for line, though the start, the typical and everything-else lines and the total do. A primary fastball has no deltas and matches line for line.
    - **"Typical numbers" is a constant per model pitch type and hand** (lefty slider +11.8, righty +12.1; four-seam +8.9 / +9.3; changeup +6.7 / +7.3; splitter +1.7 / +1.8; cutter +6.0 / +4.0). It isn't a group adjustment: fastballs have it too, and it comes from the average shape being a low spot.
    - **"Everything else" isn't a penalty for scatter:** it averages about 0, and more scatter tends to raise it (stuff_explain.ipynb §10).
  - **Compare with** (added 2026-11-06, Matt, after Matias Michel's fastball read as a 56/44 sinker): a dropdown next to Pitch picks what the Why ladder is measured against. The choices are the model's read of the row (the default, shown as e.g. "Sinker (the model's read)"), the same role's other pitch types (Four-seam / Sinker; Slider / Curveball / Cutter; Changeup / Splitter), and all of them together ("All primary fastballs", "All breaking balls", ...; every pitch type counts once). Only the row's own group is offered: each group has its own model and a grade only compares inside it, so a cutter can't be measured against a four-seam. The choice changes the start, the averages in brackets, the input lines, "typical numbers" and "everything else". The grade, What, Who and the grid don't move. It stays put when the next row has that choice ("All" carries to the next row's group), and goes back to the model's read when it doesn't. Each row-and-comparison is kept with the session's files, so flipping back is instant. Code: `usc._why_comparison`.
    - **Same row, different stories:** Michel's fastball (101.8) reads IVB +6.6 vs sinkers, −9.9 vs four-seams and −1.0 vs all primary fastballs. Against a very different type the own and delta lines can come out large and opposite (Matson's cutter vs curveballs: IVB +35.1, IVB delta −37.4), so read them as a pair.
  - **What's "the group's average pieces" line:** a constant per pitch group and hand (RHP / LHP: fastball −1.7 / −2.7, breaking ball −3.3 / −4.0, offspeed −4.1 / −3.5). It's a pitch with the group's average whiff, foul and contact chances vs the view's hitters, against the bar for 100. About 3 of it is the bar being P4 arms; hitters are on both sides of it, so they aren't the reason (in the D1 view the same line runs from −1.6 to +2.0).
  - **Who:** all 16 of the model's hitter types, best to worst, against an average pitch of the group vs. the same type, with the grade and each type's share of the view's hitter mix.
- **Stuff+ grid** (added 2026-11-06, Matt, from the chart in the Matson changeup dive): under the three boxes, Stuff+ across any two of the Why inputs, picked in X and Y dropdowns. `usc.why_grid`, drawn by `local_tabs._why_grid_fig`.
  - **What a cell is:** the row's real pitches, each moved by the same amount and regraded, everything else held. Moving velo, IVB or HB carries that delta with it, because the fastball stays put, unless that delta is the other axis, which then sets it. A primary fastball is its own reference, so its deltas stay, and it gets only the six inputs of its own. The "now" cell is exactly the Stuff table's number.
  - **Colors:** the Stuff table's (white at 100, full at 70 and 130). A box marks where the row is now and a dashed line marks 100.
  - **Cells:** 9 × 9, each step about half a D1 SD: velo 1 mph, IVB 2 in, HB 1.5 in, spin 150 rpm, release height and side 0.25 ft, and the fastball's the same as the pitch's.
  - **Lean (Claude's call, after a centered grid wasted half its cells on shapes nobody would throw):** each axis puts 6 cells on the side that raises the grade and 2 on the other, like the chart Matt liked, which started at the pitch and ran toward improvement. An input whose two sides are within 2 points stays centered (`_why_lean`). The better side is measured with the other axis where it is now.
  - **Velo ticks say the delta** to the fastball, unless the velo delta is the other axis and says it already.
  - **Speed:** a row over 15 pitches grades 15 of them, evenly spaced across their grades, and shifts the grid so the "now" cell is exact: 2–4 s a grid. On 12 final grids from 6 real rows of 30–139 pitches, against grading every pitch (4–38 s): typical cell error (RMS) 0.82 Stuff+ points on average, 2.01 on the worst grid, worst single cell 3.84, on grids spanning 21–66 points. On the earlier centered grids, 20 pitches was no better and one average pitch was far worse (8.3). Grids are kept with the session's files, so going back to one is instant.
  - **Checked:** with every pitch graded, cells match the Matson changeup analysis's harness to 3 decimals. 20 axis pairs on a cutter and a fastball: no failures, and "now" exact on all. The lean matches baseball sense on the rows looked at: a firm changeup leans slower, less IVB, more run; a fastball leans harder, more ride, lower release.
- **Same rules that graded the pitch** (`data._stuff_for`), or it wouldn't add up to the table: a pitch in that season's score table keeps its saved tags and fastball reference; any other pitch is tagged with the rows it was graded with, warmups included. So the session also keeps each file as it was graded (`sources` in the upload), not just the Live rows. A file's tagging runs once, on the first Why for that file (about 5 s for a 6,000-pitch file); after that a row takes a second or two.
- **Comparisons:** `pipeline_v2/artifacts/usc_why_D1_<model>_<year>.json` (git-ignored), built by the same `.venv/bin/python -m delispice_app.usc --build`, about 50 s. Per model pitch type, the 2026 D1 pitch types with 100+ pitches: their average inputs, average grade and the average grade at their own average shapes. Rerun after a Stuff+ retrain. ("Better than X% of D1" came out on 2026-11-06, Matt; a file built before that still carries the unused grades.) The comparison season stays 2026 whatever the file's season. Without the file, What and Who still show and Why says what to run.
- **Notes under the boxes, when they apply:** a thin sample (under 100 pitches); a row that mixes the model's pitches; TrackMan's tag differing from the model's pitch type (by default the row is compared with what the model reads, so a tagged cutter is compared with sliders; shown only on that default); a pitcher under two IDs in the data, so two fastball references (the fall file does this from 10/09, see the note below); and a warning if the row ever stops matching the table.
- **Checked 2026-11-06, after the delta change too:** every graded row of `uscfall_260901-261104.csv` (192 rows, 70 pitchers) and a database game (11 rows): grade vs. Stuff table and both ladders vs. the grade, largest difference 0.0000. A row differs from the notebook's when the notebook counts a tagged warmup (Matson's cutter, 16 vs 15), because the tab's tables use Live rows only.
- **Fall tracker offsets:** release height, extension and probably movement shift by date for the whole staff in the fall file (date explains 73% of pitcher-outing release-height variance; Matson reads 4.2 ft on 9/10 and 5.6 ft on 10/23 vs. 4.8 ft on 765 stadium pitches). Why repeats what the file says, so a release height line on fall data can be partly the tracker.
- **A pitcher with no hand:** TrackMan sometimes lists `PitcherThrows` as Both (Camp Allen in the fall file). The model needs a hand, so the Stuff table is blank and Why says there's nothing graded.

## Open

- Pairing a plate appearance whose pitcher differs between the files (offered, not built).
