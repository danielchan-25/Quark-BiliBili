#!/usr/bin/env bash
set -euo pipefail
umask 077
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd -- "$PROJECT_DIR"
if [[ "${EUID}" -eq 0 ]]; then
  printf '%s\n' 'Run the installer as the ordinary user who will own the project, not root.' >&2
  exit 1
fi
INSTALL_UNITS=false
case "${1:-}" in
  '') ;;
  --with-report-units) INSTALL_UNITS=true ;;
  *) printf '%s\n' 'Usage: bash scripts/install-user.sh [--with-report-units]' >&2; exit 2 ;;
esac
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12 or newer is required"'
if [[ ! -f requirements-linux.lock ]]; then
  printf '%s\n' 'requirements-linux.lock is missing; use the complete release.' >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements-linux.lock
mkdir -p data logs
copy_missing() {
  local source="$1" destination="$2"
  if [[ ! -e "$destination" && ! -L "$destination" ]]; then
    cp -- "$source" "$destination"
    chmod 600 -- "$destination"
  fi
}
copy_missing .env.example .env
copy_missing data/account.example.csv data/account.csv
copy_missing config/schedules.example.toml data/schedules.toml
if [[ "$INSTALL_UNITS" == true ]]; then
  .venv/bin/python scripts/render_report_units.py
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
  fi
fi
printf '%s\n' 'Installation complete. Existing local configuration was preserved.'
printf '%s\n' 'Replace the demonstration accounts and configure the browser before use.'
printf '%s\n' 'No browser was started, no report sent, and no timer was enabled.'
