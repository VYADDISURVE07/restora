#!/usr/bin/env sh
# Restora - run.   Examples:  sh run.sh   |   sh run.sh my_apis.csv   |   sh run.sh --demo
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$HERE/.venv/bin/python" ]; then
  echo "Run 'sh setup.sh' first."; exit 1
fi
PYTHONUTF8=1 exec "$HERE/.venv/bin/python" "$HERE/run.py" "$@"
