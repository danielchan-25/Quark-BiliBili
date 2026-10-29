#!/usr/bin/env bash
set -euo pipefail
umask 077
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$PROJECT_DIR"
PYTHON="$PROJECT_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  printf '%s\n' 'Project virtual environment is missing; run bash scripts/install-user.sh first.' >&2
  exit 1
fi
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
case "${1:-}" in
  account|account-check|resource|toutiao|promotion|promotion-candidates|promotion-summary|nurture)
    set -- "$PROJECT_DIR/main.py" "$@"
    ;;
esac
exec "$PYTHON" "$@"
