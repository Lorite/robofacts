#!/usr/bin/env bash
# The whole loop on the example project, in five steps: linter, planner, dry run, the
# campaign (gate, trial, recording, verify steps, metadata) and the coverage index.
# Needs ROS 2 Humble sourced and the two packages installed (pip or colcon). Runs inside
# the quickstart image too: docker build -t trials-quickstart -f docker/Dockerfile . && docker run --rm trials-quickstart
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO/robofacts_ros2/example_project"

CAMPAIGN=experiments/demo_talker_listener/campaigns/rates.yaml

# The command: `trials` on the PATH (a pip install, or a sourced colcon workspace), else
# through ros2 run (the ROS 2 package installs it there too), else from source.
if command -v trials >/dev/null 2>&1; then
    trials() { command trials "$@"; }
elif ros2 pkg prefix robofacts_ros2 >/dev/null 2>&1; then
    trials() { ros2 run robofacts_ros2 trials "$@"; }
else
    trials() { PYTHONPATH="$REPO/robofacts:$REPO/robofacts_ros2:${PYTHONPATH:-}" python3 -m robofacts.cli "$@"; }
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
