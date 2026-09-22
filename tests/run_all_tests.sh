#!/usr/bin/env bash
# tests/run_all_tests.sh -- every suite in this module, in one command.
#
# Written 2026-09-06. This module had no runner, so its suites were only ever
# run one at a time by whoever remembered they existed. The run that failed
# that night would have been caught by test_smoke_end_to_end.py, which nobody
# had a reason to run because nothing ran it for them.
#
# Usage:  bash tests/run_all_tests.sh
# Exit:   0 when every suite passes, 1 otherwise; the failing suites are named.
set -u
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH="$(pwd)"
# Default to the system interpreter: on this box PATH can start with an
# unrelated virtualenv (2026-09-11), and PYTHON= still overrides.
if [ -x /usr/bin/python3 ]; then PY="${PYTHON:-/usr/bin/python3}"; else PY="${PYTHON:-python3}"; fi

failed=0
names=""
for t in tests/test_*.py; do
    echo "== $t"
    if ! "$PY" "$t"; then
        echo "SUITE FAIL: $t"
        failed=$((failed + 1))
        names="$names $t"
    fi
done

echo
if [ "$failed" -eq 0 ]; then
    echo "== suites failed: 0"
    exit 0
fi
echo "== suites failed: $failed --$names"
exit 1
