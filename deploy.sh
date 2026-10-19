#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for candidate in python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then
        exec "$candidate" "$ROOT/tools/release/bootstrap.py" "$@"
    fi
done
echo 'Install Python 3.12 with venv/pip, then run bash deploy.sh again.' >&2
exit 2
