#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
exec bash "$PROJECT_DIR/scripts/run.sh" scripts/report_runner.py "$@"
