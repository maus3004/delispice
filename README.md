# delispice

TrackMan pitcher & batter scouting reports — a [Dash](https://dash.plotly.com/)
app over DuckDB/parquet. Runs as a native desktop window on a Mac, or as a plain
web app on a server.

## Layout

```
delispice_app/        the app — Dash UI/callbacks (app.py), DuckDB serving (data.py),
                      report builders + Plotly figures (report.py), desktop launcher (launch.py)
backend/models/       reusable model toolkit (autotagger.py) + PitchUID cluster adapter (cluster.py)
deploy/               server deployment kit — see deploy/DEPLOY.md
data_pipeline/        TrackMan parquet data — NOT in git; must be present on each machine
```

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-desktop.txt      # web-only: use requirements.txt
python -m delispice_app.launch               # native window (desktop)
# or:
python -m delispice_app.app                  # plain web server on http://127.0.0.1:8765
```

The app expects TrackMan parquet at `data_pipeline/wbaserunners/`. The first run
builds a cached picker index (`delispice_app/.cache/`) over all parquet files.

## Deploy to a server

See **[deploy/DEPLOY.md](deploy/DEPLOY.md)** — gunicorn behind Caddy, with a
one-shot `deploy/setup.sh` bootstrap and a troubleshooting table.
