#!/usr/bin/env bash
# The whole loop on the example project, in five steps: linter, planner, dry run, the
# campaign (gate, trial, recording, verify steps, metadata) and the coverage index.
# Needs ROS 2 Humble sourced and the package installed (pip or colcon). Runs inside
# the quickstart image too: docker build -t trials-quickstart -f docker/Dockerfile . && docker run --rm trials-quickstart
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/example_project"

CAMPAIGN=experiments/demo_talker_listener/campaigns/rates.yaml

# The command: `trials` on the PATH (a pip install), else through ros2 run (a colcon
# install puts it in lib/declarative_robot_trials, not on the PATH), else from source.
if command -v trials >/dev/null 2>&1; then
    trials() { command trials "$@"; }
elif ros2 pkg prefix declarative_robot_trials >/dev/null 2>&1; then
    trials() { ros2 run declarative_robot_trials trials "$@"; }
else
    trials() { PYTHONPATH="$(dirname "${BASH_SOURCE[0]}")/..:${PYTHONPATH:-}" python3 -m declarative_robot_trials.cli "$@"; }
fi

echo "== 0. Where the roots resolve to (trials.yaml in this directory)"
trials roots

echo "== 1. The linter checks every declaration against the rules"
trials lint

echo "== 2. The planner lists the trials the campaign still needs"
trials plan "$CAMPAIGN"

echo "== 3. A dry run prints the exact commands and touches nothing"
trials run "$CAMPAIGN" --limit 1

echo "== 4. The campaign runs: gate, trial, recording, verify steps, metadata"
trials run "$CAMPAIGN" --execute --no-pause

echo "== 5. The coverage index counts only valid trials"
trials plan "$CAMPAIGN"
trials index --experiment demo_talker_listener --summary
