# delispice_app

A standalone desktop app — the successor to the `interactive_pitcher` notebook. One Python
program, **no API to run**: it queries the parquet tree with DuckDB, builds the report with
Polars/Plotly, and shows it in a native pop-up window (pywebview) driven by a Dash UI.

## Run it

From the repo root (`delispice/`):

```bash
# native pop-up window (what you want day to day)
.venv/bin/python -m delispice_app.launch

# or, same app in your browser (handy for debugging)
.venv/bin/python -m delispice_app.app      # then open http://127.0.0.1:8765
```

First launch scans the parquet tree once (~10 s) to build a small picker index per role and caches
it under `delispice_app/.cache/`; later launches are instant. When new games are added to
`data_pipeline/wbaserunners/`, click **⟳ Rebuild index** in the app.

### Double-click launchers (no terminal)

Two options are provided so you never have to touch the terminal:

- **`delispice_app.app`** (repo root) — double-click; the report window pops up, no Terminal. Drag it
  to the Dock / Applications / Desktop. It's a generated bundle (git-ignored); rebuild with:
  ```bash
  osacompile -o "delispice_app.app" -e 'do shell script "nohup /Users/matt/Desktop/delispice/.venv/bin/python /Users/matt/Desktop/delispice/delispice_app/launch.py > /tmp/delispice_app.log 2>&1 &"'
  ```
  (logs go to `/tmp/delispice_app.log`).
- **`delispice_app/delispice_app.command`** — double-click in Finder; opens a small Terminal window
  (handy for seeing logs) alongside the report window.

## Using it

0. **Choose a role** with the **Pitchers / Batters** tab at the top — it switches what the picker
   looks up and which report renders.
1. **Pick a player** with the cascading filters: **Year(s)** (check any; none = all years) +
   **Level** → **Conference** → **Team** → **Pitcher/Batter** (type to search). Conference is the
   team's own conference (SEC = the 16 SEC schools). The report loads automatically.
2. **Pitcher report**: header + summary line, then the **Splits** — the **Batter** and **Count**
   checkboxes drive both the **arsenal table** and the **location heatmap** together. The heatmap's
   stat (Pitch density / Whiff% / Called strike% / Hard hit%) is the **dropdown on the chart itself**.
   The **movement** scatter and **velocity** distribution always show all pitches. Between the
   movement chart and the heatmap sits the **Percentile Rankings** panel (Savant-style sliders):
   FB Velo, Avg EV, Chase%, Whiff%, K%, BB%, Barrel% (Statcast definition), Hard Hit%, GB%,
   Extension — ranked vs **qualified pitchers (≥10 IP; `QUAL_OUTS` in data.py) at the selected
   Level + Year(s)**, goodness-oriented (100 = elite even for the lower-is-better stats). Pools are
   computed once per Level/Years combo and disk-cached (`.cache/pctpool_*.parquet`); **⟳ Rebuild
   index** refreshes them when new games land.
3. **Batter report**: batting-line summary (AVG/OBP/SLG/OPS, K%/BB%, batted-ball EV/LA), the
   collapsible by-pitch-type family table, a **spray chart** (Direction→Bearing flight paths on the
   field; follows the vs RHP/LHP selector), and a **location heatmap** — smoothed **Exit Velo by location** with an on-chart toggle
   to **Whiff%** or **Chase%** by location (chase = swing rate on out-of-zone pitches; the zone
   interior stays blank), strike zone drawn. It obeys both the **vs RHP/LHP** selector and the
   **Family** radio (All / Fastballs / Breaking / Offspeed); the stat toggle survives filter changes.
4. **AutoCluster** (pitcher report → 🏷 Retag pitches → ③): GMM pitch clustering from the shared
   **`backend/models/autotagger.py`** (the reusable module extracted from `autotagger.ipynb` —
   StandardScaler → GaussianMixture full/n_init=20/rs=42, k=1–6 by min ICL; also exposes
   `load_pitches`, `autotag_by_pitcher`, and `cluster_means` for other models/reports). Default features RelSpeed/SpinRate/IVB/HB; tick **+ RelHeight & Extension** for the
   6-feature run. Runs on the pitcher's current selection, includes Undefined/Other pitches, and
   renumbers clusters by usage (Cluster 0 = most thrown). While active, the whole pitcher report
   (arsenal, movement colours, velocity) shows **Cluster 0…k** — rename each cluster to a TrackMan
   pitch type via the dropdowns (updates live), **Revert clustering** restores the tags. Cluster
   state lives in its own `.cache/autocluster.json` (separate from retags, so reverting never touches
   manual retags). **⬇ CSV / Parquet with clusters** exports the pitcher's full raw rows (all
   original columns) plus a `ClusterTag` column appended last.

## How it works

| File | Role |
|------|------|
| `data.py` | DuckDB serving layer, generalized by **role** (`"pitcher"` / `"batter"`). Builds/caches a `(Part, Level, Team, Player, Year)` picker index per role (`.cache/{role}_index.parquet`); per player, scans **only** the partitions they appear in, projecting just that role's report columns (trajectories / notes / UIDs / etc. are never read). Results are cached in process. |
| `report.py` | Pure report builders + Plotly figures. Pitchers: summary, arsenal (velo/spin/tilt), movement, heatmap, velocity. Batters: batting-line summary, spray chart. No Dash/DuckDB. |
| `app.py` | Dash UI — the Pitchers/Batters tab, the shared role-aware cascade, both reports, and in-place pitcher split updates (the heatmap uses a Dash `Patch` so the on-chart stat selection is preserved). Run it directly for browser mode. |
| `backend/models/cluster.py` | Thin AutoCluster adapter (lives with the models, not in the app) — the GMM is the sibling `backend/models/autotagger.py`; the adapter keys labels by PitchUID and `data.py` loads it by file path. |
| `launch.py` | Starts the Dash server on a local port and opens it in a native window via pywebview. |

Memory stays small: the full ~7M-row / 206-column dataset is never loaded — only the tiny per-role
index and one player's few-thousand rows at a time.

## Dependencies

Installed in the repo's `.venv` (see `requirements.txt`): `dash`, `plotly`, `duckdb`, `polars`,
`pyarrow`, `numpy`, `scipy`, `scikit-learn`, `pywebview` (Python 3.14; pywebview uses macOS WebKit
via pyobjc).
