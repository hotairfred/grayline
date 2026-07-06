#!/bin/sh
# Grayline installer — sets up an isolated venv with the one dependency, so a
# fresh checkout runs on any Mac/Linux box without the "which pip fed which
# python" trap that bites crew installs. Run once, then ./run.sh. Idempotent —
# safe to re-run.
set -e
cd "$(dirname "$0")"

# 1. Find a Python 3.8+ interpreter (the union-annotation + venv floor).
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3 python; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    echo "ERROR: Grayline needs Python 3.8+, and none was found on your PATH."
    echo "  macOS:         brew install python@3.12"
    echo "  Debian/Ubuntu: sudo apt install python3 python3-venv"
    exit 1
fi
echo "==> Using $("$PY" --version 2>&1) ($PY)"

# 2. Create the venv — its own interpreter + its own site-packages, so the
#    host's system Python and pip never enter the picture.
if [ ! -d venv ]; then
    "$PY" -m venv venv
    echo "==> Created venv/"
fi

# 3. Install deps INTO the venv via its own python -m pip. No ambiguity about
#    which pip, and venvs are exempt from the PEP 668 "externally-managed" block
#    that trips Homebrew Python on a Mac.
./venv/bin/python -m pip install --upgrade pip >/dev/null 2>&1 || true
./venv/bin/python -m pip install -r requirements.txt
echo "==> Dependencies installed into venv/"

# 4. Seed config files from the examples on first run (never clobber existing).
for f in config secrets; do
    if [ ! -f "$f.json" ] && [ -f "$f.json.example" ]; then
        cp "$f.json.example" "$f.json"
        echo "==> Created $f.json — edit it before running"
    fi
done

echo ""
echo "Done. Set at least \"callsign\" and \"home_grid\" in config.json, then: ./run.sh"
