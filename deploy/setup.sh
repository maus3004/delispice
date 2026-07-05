#!/usr/bin/env bash
# delispice_app — one-shot server bootstrap.
#
# Run from the repo root on the Linux box:
#   bash deploy/setup.sh
#
# Idempotent (safe to re-run) and verbose (so you can see exactly what happens
# and where it stops if something is wrong). It does NOT touch systemd or Caddy —
# it just gets the Python side working. See deploy/DEPLOY.md for the rest.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
echo "==> Repo root: $REPO"

# 1. The TrackMan data must already be present at the identical relative path.
if [ ! -d "$REPO/data_pipeline/wbaserunners" ]; then
	echo "!! MISSING: $REPO/data_pipeline/wbaserunners"
	echo "   The app reads TrackMan parquet from there (it is NOT in git). Put the"
	echo "   data in place at that exact path, then re-run this script."
	exit 1
fi
PARQUET_N="$(find data_pipeline/wbaserunners -name '*.parquet' | wc -l | tr -d ' ')"
echo "==> Found data_pipeline/wbaserunners ($PARQUET_N parquet files)"

# 2. Raise the open-file limit for THIS shell (the index build opens many files).
#    systemd handles this separately via LimitNOFILE in delispice.service.
ulimit -n 65536 2>/dev/null || echo "   (could not raise ulimit -n; continuing)"

# 3. Python virtualenv. Needs Python 3.11+ (dev used 3.14). uv is easiest.
if [ ! -d "$REPO/.venv" ]; then
	if command -v uv >/dev/null 2>&1; then
		echo "==> Creating .venv with uv (Python 3.14)"
		uv venv --python 3.14 "$REPO/.venv"
	else
		echo "==> Creating .venv with system python3 ($(python3 --version 2>&1))"
		python3 -m venv "$REPO/.venv"
	fi
else
	echo "==> Reusing existing .venv"
fi

# 4. Install server dependencies.
echo "==> Installing server dependencies"
"$REPO/.venv/bin/python" -m pip install --upgrade pip
"$REPO/.venv/bin/python" -m pip install -r "$REPO/deploy/requirements-server.txt"

# 5. Warm the cache: the first import builds the picker index over EVERY parquet
#    file. Doing it here (once, in the foreground) means the service starts fast
#    and any data problem shows up now, with a clear error, instead of as a
#    gunicorn worker timeout later.
echo "==> Warming index cache (first build scans all $PARQUET_N files; may take a few minutes)"
"$REPO/.venv/bin/python" -c "import delispice_app.app; print('index cache ready')"

echo
echo "==> Bootstrap complete. Quick local test:"
echo "    .venv/bin/gunicorn --chdir $REPO --workers 1 --threads 4 --timeout 600 \\"
echo "        --bind 127.0.0.1:8765 delispice_app.app:server"
echo "    then, in another shell:  curl -sI http://127.0.0.1:8765/ | head -1"
echo
echo "    For the always-on service + Caddy, follow deploy/DEPLOY.md."
