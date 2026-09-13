#!/usr/bin/env bash
# Run the CampusLingo core-ai test suite.
#
#   ./run_tests.sh                 # everything (unit + LLM path + PostgreSQL E2E)
#   ./run_tests.sh -m "not e2e"    # fast: no database, no uvicorn
#   ./run_tests.sh tests/test_core_contracts.py -k fsrs
#
# Creates .venv on first run and installs requirements-dev.txt (which pulls in
# requirements.txt plus pytest and the bundled pgserver PostgreSQL binaries, so
# the end-to-end suite needs no Docker).
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo "==> creating virtualenv (.venv)"
  "$PYTHON_BIN" -m venv .venv
fi

echo "==> installing test dependencies"
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements-dev.txt

echo "==> pytest $*"
exec ./.venv/bin/python -m pytest "$@"
