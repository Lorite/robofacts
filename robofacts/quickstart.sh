#!/usr/bin/env bash
# The whole loop on the example project of the general package, in five steps: linter, planner, dry
# run, the campaign (gate, trial, recording, verify step, metadata) and the coverage index.
# Needs Python 3.10+ and the package robofacts (pip install ./robofacts), nothing else.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/example_project"

CAMPAIGN=experiments/demo_log_recorder/campaigns/grid.yaml

# The command: `trials` on the PATH (the installed package), else from this source tree.
if command -v trials >/dev/null 2>&1; then
    trials() { command trials "$@"; }
else
    export PYTHONPATH="$HERE:${PYTHONPATH:-}"
    trials() { python3 -m robofacts.cli "$@"; }
fi

echo "== 0. Where the roots resolve to (trials.yaml in this directory)"
trials roots

echo "== 1. The linter checks every declaration against the rules"
trials lint

echo "== 2. The planner lists the trials the campaign still needs"
trials plan "$CAMPAIGN"

echo "== 3. A dry run prints the exact commands and touches nothing"
trials run "$CAMPAIGN" --limit 1

echo "== 4. The campaign runs: gate, trial, recording, verify step, metadata"
trials run "$CAMPAIGN" --execute --no-pause

echo "== 5. The coverage index counts only valid trials"
trials plan "$CAMPAIGN"
trials index --experiment demo_log_recorder --summary
