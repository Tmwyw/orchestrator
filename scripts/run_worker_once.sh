#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"

cd "$REPO_ROOT"

if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PYTHON="$REPO_ROOT/.venv/bin/python"
elif [ -x /opt/netrun-orchestrator/.venv/bin/python ]; then
  PYTHON="/opt/netrun-orchestrator/.venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

"$PYTHON" - <<'PY'
from orchestrator.worker import run_once

processed = run_once()
print(f"processed={1 if processed else 0}")
PY
