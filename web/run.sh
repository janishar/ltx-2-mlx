#!/usr/bin/env bash
# Launch ltx studio from anywhere: bash web/run.sh [--model PATH] [--port 8720] [--host 127.0.0.1]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec uv run python web/server.py "$@"
