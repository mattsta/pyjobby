#!/usr/bin/env bash
#
# The GitHub CI loop, one entry point. All fetching, parsing and filtering
# lives in scripts/ci.py (stdlib only; needs `gh` authenticated):
#
#   ./scripts/ci.sh                    # status of the latest run for this branch
#   ./scripts/ci.sh failures           # parsed failure evidence, per failed step
#   ./scripts/ci.sh failures --json    # same, machine-readable stable keys
#   ./scripts/ci.sh failures --full    # raw failed-step logs
#   ./scripts/ci.sh watch              # block until finished, then status
#   ./scripts/ci.sh rerun              # rerun only the failed jobs
#
# Every command takes an optional RUN_ID; default is the newest run.

set -euo pipefail
exec python3 "$(dirname "$0")/ci.py" "$@"
