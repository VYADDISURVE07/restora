#!/usr/bin/env sh
# Restora - one-time setup for Mac / Linux.   Run:  sh setup.sh
set -e
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "[ERROR] Python was not found. Install Python 3.9 or newer (https://www.python.org/downloads/) and run this again."
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "[ERROR] Python 3.9 or newer is needed. Found: $("$PY" --version)"
  exit 1
fi
echo "Using $("$PY" --version) at $PY"

if [ ! -x ".venv/bin/python" ]; then
  "$PY" -m venv .venv || { echo "[ERROR] Could not create the virtual environment (on Debian/Ubuntu: sudo apt install python3-venv)."; exit 1; }
fi

if [ -d wheels ]; then
  echo "Installing from the local 'wheels' folder (offline mode)..."
  .venv/bin/python -m pip install --no-index --find-links=wheels -r requirements.txt
else
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt || {
    echo "[ERROR] Could not download the packages. See SETUP_GUIDE.md (offline install, company proxy)."; exit 1; }
fi

echo
echo "Checking the installation (Restora's own tests)..."
.venv/bin/python run.py --selftest
echo
echo "Setup finished. Next:"
echo "  sh studio.sh          opens Restora Studio in your browser"
echo "  sh run.sh --demo      runs the Demo Bank example in this terminal"
echo "  sh ai_setup.sh        optional: installs the free local AI model (Ollama)"
