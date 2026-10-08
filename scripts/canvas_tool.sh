#!/usr/bin/env bash
# Wrapper for the forum agent: loads config, runs the Python tool, passes stdin through.
# Usage: canvas_tool.sh fetch|submit|status|drafts [args]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if [ -f config/local.env ]; then
  set -a
  # shellcheck disable=SC1091
  . config/local.env
  set +a
fi

exec .venv/bin/python -m agent "$@"
