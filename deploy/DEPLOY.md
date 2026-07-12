# Deploying delispice_app on a Linux server

This app is a Dash web app (`delispice_app.app:server`, a Flask WSGI app) that
reads TrackMan parquet from `data_pipeline/wbaserunners/`. In production it runs
as **gunicorn** (the Python web server) behind **Caddy** (the reverse proxy that
faces the internet and, later, handles HTTPS).

```
browser ──▶ Caddy (:80 / :443) ──▶ gunicorn (127.0.0.1:8765) ──▶ delispice_app ──▶ DuckDB/parquet
```

Target box assumed here: **8 cores, 16 GB RAM**, and the `data_pipeline/` data
**already present** at the same relative path the app uses.

---

## 0. Prerequisites

- A Linux box you can `sudo` on (Ubuntu/Debian assumed for `apt` commands).
- **Python 3.11+** (dev used 3.14). The easiest way to get a matching Python is
  [`uv`](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `git`, and the ability to reach `github.com/maus3004/delispice`.
- The **data already on the box** (5.9 GB / ~60k parquet files). It is *not* in
  git and never will be — see step 1.

---

## 1. Get the code beside the data

The data can't travel in git, so the goal is: **the repo folder ends up
containing your existing `data_pipeline/`**. Pick the case that matches you.

### Case A — the data is already in a folder (e.g. `~/delispice/data_pipeline/`)

You can't `git clone` into a non-empty folder, so lay the repo *over* it. Because
`data_pipeline/` is git-ignored, this never touches your data:

```bash
cd ~/delispice                 # the folder that already holds data_pipeline/
git init
git remote add origin https://github.com/maus3004/delispice.git
git fetch origin
git checkout -f main
```

### Case B — starting fresh, data will be copied in afterward

```bash
git clone https://github.com/maus3004/delispice.git ~/delispice
# then put the data at ~/delispice/data_pipeline/wbaserunners/ (rsync/scp/etc.)
```

### Case C — the data lives elsewhere (e.g. `/data/wbaserunners`)

Clone fresh, then symlink so the app finds it at the expected path:

```bash
git clone https://github.com/maus3004/delispice.git ~/delispice
ln -s /data ~/delispice/data_pipeline     # so ~/delispice/data_pipeline/wbaserunners exists
```

**Sanity check** — this must list parquet files before you continue:

```bash
ls ~/delispice/data_pipeline/wbaserunners/**/*.parquet | head
```

---

## 2. Install + warm the cache

```bash
cd ~/delispice
bash deploy/setup.sh
```

This creates `.venv`, installs `deploy/requirements-server.txt`, and builds the
picker index once (the slow step — it scans every parquet file). Doing it now
means the service starts instantly and any data problem surfaces here with a
clear message instead of as a mysterious timeout later.

Quick local test before wiring up the service:

```bash
.venv/bin/gunicorn --chdir ~/delispice --workers 1 --threads 4 --timeout 600 \
    --bind 127.0.0.1:8765 delispice_app.app:server
# in another shell:
curl -sI http://127.0.0.1:8765/ | head -1     # expect: HTTP/1.1 200 OK
```

Stop it with Ctrl-C once you see the 200.

---

## 3. Run it as a service (systemd)

```bash
sed -i "s/<LINUX_USER>/$USER/g" deploy/delispice.service   # fill in your username
# double-check WorkingDirectory=/home/<you>/delispice is correct if not in $HOME
sudo cp deploy/delispice.service /etc/systemd/system/delispice.service
sudo systemctl daemon-reload
sudo systemctl enable --now delispice
systemctl status delispice          # should be "active (running)"
journalctl -u delispice -f          # live logs
```

The unit already sets the two things that bite first-time deploys:
`LimitNOFILE=65536` (for the 60k files) and `DUCKDB_MEMORY_LIMIT=10GB`.

---

## 4. Put Caddy in front

```bash
# install Caddy (Debian/Ubuntu) — see https://caddyserver.com/docs/install
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Now visit `http://<server-ip>/`. You should get the app.

> If port 80 is firewalled off, open it: `sudo ufw allow 80/tcp` (and `443/tcp`
> once you enable HTTPS).

---

## 5. Enabling HTTPS later (when you have a domain)

1. Point a DNS **A record** at the box (e.g. `reports.example.com -> <server-ip>`).
2. Edit `/etc/caddy/Caddyfile`: delete the `:80 { ... }` block, uncomment the
   domain block, set your domain.
3. `sudo systemctl reload caddy` — Caddy fetches and auto-renews the certificate.
   (Open `443/tcp` in the firewall if needed.)

---

## Preserving your cluster names / retags

Your AutoCluster names and pitch retags live in
`delispice_app/.cache/autocluster.json` (and `retags.json` if present). These are
git-ignored **user edits**, so they don't come across with `git pull`. The picker
index in `.cache` rebuilds itself automatically; only the `.json` files are worth
carrying over. To copy them from your Mac after the server's first run:

```bash
scp delispice_app/.cache/*.json <user>@<server>:~/delispice/delispice_app/.cache/
sudo systemctl restart delispice
```

## Model artifacts (contact quality / xRV)

The xRV column in the reports is served from pre-trained model artifacts
(`backend/models/artifacts/cq_{level}_{year}.npz/.json`, ~4–10 MB each). They are
**git-ignored** — each machine builds its own from the data it already has:

```bash
cd ~/delispice
.venv/bin/python -m backend.models.cq_store          # trains every D1 year (~4 s each)
sudo systemctl restart delispice                     # reload so the app picks them up
```

Run that once at deploy, and again whenever a season's data grows (new games) —
retraining refreshes the model's reference set. Reports render fine without
artifacts; the xRV cells are just blank until the level+year is trained.

## Updating after you push new code

```bash
cd ~/delispice
git pull
.venv/bin/pip install -r deploy/requirements-server.txt   # only if deps changed
sudo systemctl restart delispice
```

---

## Troubleshooting

First stop is always: `journalctl -u delispice -e` (app) and
`journalctl -u caddy -e` (proxy).

| Symptom | Cause & fix |
|---|---|
| `ModuleNotFoundError: No module named 'delispice_app'` | gunicorn not started from the repo root. Check `--chdir` / `WorkingDirectory` point at the repo, and you're using `.venv/bin/gunicorn`. |
| `OSError: [Errno 24] Too many open files` | The fd limit. In systemd it's `LimitNOFILE=65536` (already set) — confirm with `systemctl show delispice -p LimitNOFILE`. For a manual run: `ulimit -n 65536` first. |
| Reports empty / "no data" / `IOException ... No files found` | The app can't see the data. Confirm `ls ~/delispice/data_pipeline/wbaserunners/**/*.parquet` lists files and that `WorkingDirectory` is the repo root. |
| gunicorn worker killed / timeout on first page load | The index is still building over 60k files. You skipped the warmup — run `bash deploy/setup.sh` (or just `.venv/bin/python -c "import delispice_app.app"`) once, then restart. |
| Worker killed under load (OOM) | Two heavy concurrent queries. Lower `DUCKDB_MEMORY_LIMIT` (e.g. `6GB`) and/or drop `--threads` to `1` in the service, then `daemon-reload` + restart. |
| Caddy shows **502 Bad Gateway** | gunicorn isn't up or is on a different port. `systemctl status delispice`; `curl -sI http://127.0.0.1:8765/`. |
| `Address already in use` on `:8765` | Something else has the port. `sudo ss -ltnp | grep 8765`; change the port in *both* the service `--bind` and the Caddyfile `reverse_proxy`. |
| `pip install` fails building numpy/polars/scipy | Python version mismatch (needs 3.11–3.14). `.venv/bin/python --version`; recreate the venv with `uv venv --python 3.14`. |
| First page load is slow, later loads fast | Expected — that's the parquet metadata/cache warming. |

### The three knobs, in one place

- **Memory**: `Environment=DUCKDB_MEMORY_LIMIT=10GB` in the service (or the
  `DUCKDB_MEMORY_LIMIT` env var anywhere). Caps DuckDB so it spills instead of
  OOM-ing. Code default if unset: ~65% of RAM.
- **Concurrency**: `--threads N` in the service. More = more simultaneous
  requests, but watch memory. `--workers` should stay at 1.
- **Boot patience**: `--timeout 600` covers the cold index build.
