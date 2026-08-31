#!/usr/bin/env bash
# uniagent update - pulls the latest code and restarts the services.
#
#   scripts/update.sh                update now
#   scripts/update.sh --check        say what it would bring, change nothing
#   scripts/update.sh --no-restart   update, but leave the services as they are
#
# A wrapper, and deliberately nothing more. The update itself is update.py,
# which is shared with Windows and with the "update now" button on the settings
# page, so there is exactly one description of what an update does and what it
# is careful not to touch. Read that file's docstring, not this one.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The venv's interpreter if install.sh built one - that is where requests and
# cryptography live - and the system python otherwise.
PY="$ROOT/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "No python3 found. Re-run install.sh." >&2; exit 1; }

exec "$PY" "$ROOT/scripts/update.py" "$@"
