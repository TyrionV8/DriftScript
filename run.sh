#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ -x "$DIR/.venv/bin/python" ]; then
  PY="$DIR/.venv/bin/python"
elif [ -x "/home/davi/repos/CutScript/backend/.venv/bin/python" ]; then
  PY="/home/davi/repos/CutScript/backend/.venv/bin/python"
else
  PY="python3"
fi

export PYTHONPATH="$DIR:${PYTHONPATH:-}"
exec "$PY" -m driftscript.server "$@"
