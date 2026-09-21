#!/usr/bin/env bash
#
# Unit test runner for the concurrency / transaction hardening work.
#
# Usage:
#   ./test.sh                 # run the targeted test modules for this change
#   ./test.sh --all           # run the entire test suite
#   ./test.sh <extra args>    # forward any arguments to manage.py test
#                             # e.g. ./test.sh --all -v2
#                             #      ./test.sh hc.api.tests.test_sendalerts
#
# Database:
#   Uses the project default (SQLite) unless DB=postgres or DB=mariadb is set
#   in the environment, exactly like manage.py test itself. Example:
#       DB=postgres DB_HOST=127.0.0.1 DB_USER=postgres ./test.sh
#   (The sendalerts row-locking concurrency tests only run on databases which
#   support SELECT ... FOR UPDATE SKIP LOCKED; they are skipped on SQLite.)

set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

# Test modules directly covering the four fixes:
#   1. sendalerts TOCTOU (Flip claim + check going-down)
#   2. ping/create_flip visibility and atomic flip creation
#   3. prune S3/DB coordination and S3 helper retries
#   4. EmailThread SMTP connection lifecycle
TARGET_TESTS=(
    hc.api.tests.test_sendalerts
    hc.api.tests.test_check_model
    hc.api.tests.test_ping
    hc.api.tests.test_ping_model
    hc.api.tests.test_flip_model
    hc.api.tests.test_prunepingsslow
    hc.lib.tests.test_emails
    hc.lib.tests.test_s3
)

if [ "${1:-}" = "--all" ]; then
    shift
    echo "Running full test suite..."
    exec "$PYTHON" manage.py test "$@"
fi

if [ "$#" -gt 0 ]; then
    # Caller specified explicit test labels / options: run them as-is.
    echo "Running: $*"
    exec "$PYTHON" manage.py test "$@"
fi

echo "Running targeted tests for the concurrency/transaction fixes:"
printf '  %s\n' "${TARGET_TESTS[@]}"
exec "$PYTHON" manage.py test "${TARGET_TESTS[@]}"
