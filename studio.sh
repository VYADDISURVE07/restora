#!/usr/bin/env sh
# Copyright 2026 Vedant Surve
# SPDX-License-Identifier: Apache-2.0
# Restora Studio - the point-and-click app. Your browser opens; press Ctrl+C here to stop.
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$HERE/.venv/bin/python" ]; then
  echo "Run 'sh setup.sh' first."; exit 1
fi
PYTHONUTF8=1 exec "$HERE/.venv/bin/python" "$HERE/run.py" --studio "$@"
