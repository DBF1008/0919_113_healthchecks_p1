#!/usr/bin/env bash
#
# test.sh - manually run the unit tests for the sendalerts / prune / email
#           concurrency & consistency fixes.
#
# Usage:
#   ./test.sh                 # run the targeted unit-test modules for the fixes
#   ./test.sh all             # run the full Django test suite
#   ./test.sh <django label>  # forward one or more test labels to manage.py
#                             # e.g. ./test.sh hc.api.tests.test_sendalerts
#
# Environment:
#   PYTHON   - python interpreter to use (default: first of ./venv, python3)
#   DB       - optional DB backend ("postgres" / "mysql"), passed to settings
#
set -euo pipefail

cd "$(dirname "$0")"

# Pick an interpreter. Prefer a local ./venv if it exists.
if [[ -z "${PYTHON:-}" ]]; then
    if [[ -x "./venv/bin/python" ]]; then
        PYTHON="./venv/bin/python"
    else
        PYTHON="$(command -v python3)"
    fi
fi

echo "Using interpreter: $PYTHON (""$("$PYTHON" --version 2>&1))"

# Make sure Django can import the project.
export PYTHONPATH="$(pwd):${PYTHONPATH:-}"

# Default: the modules directly covering the fixes.
DEFAULT_TARGETS=(
    hc.api.tests.test_sendalerts
    hc.api.tests.test_check_model
    hc.api.tests.test_ping
    hc.api.tests.test_prunepingsslow
    hc.lib.tests.test_emails
    hc.lib.tests.test_s3
)

if [[ $# -eq 0 ]]; then
    TARGETS=("${DEFAULT_TARGETS[@]}")
else
    TARGETS=("$@")
fi

if [[ "${TARGETS[0]:-}" == "all" ]]; then
    # "all" => run the entire Django test suite (no labels)
    echo "Running: $PYTHON manage.py test (full suite)"
    exec "$PYTHON" manage.py test
fi

echo "Running: $PYTHON manage.py test ${TARGETS[*]}"
exec "$PYTHON" manage.py test "${TARGETS[@]}"
