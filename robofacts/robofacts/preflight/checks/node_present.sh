#!/usr/bin/env bash
# A named node is in `ros2 node list`. Deliberately WEAK: presence is not
# effectiveness (2026-07-23: the watchdog was present and blind). Prefer a
# health/data check where the node offers one.
#
# Substring match by default. Pass exact=true to require the FULL node name
# exactly as `ros2 node list` prints it (e.g. /detector_node). Use exact
# whenever the target has similarly-named neighbours: the substring "detector"
# was satisfied by a pose bridge node while both detector
# components had failed to load, so the gate blessed a rig with no detector
# (2026-08-12).
#
#   node_present.sh name=/driver_server
#   node_present.sh name=/detector_node exact=true
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="node_present.sh name=<node name substring> [exact=true  # full-name match]"
pf_parse_args "$@"
pf_require_args name
NAME="$(pf_arg name)"
EXACT="$(pf_arg exact false)"
pf_require_ros
if [ "$EXACT" = "true" ]; then
    pf_node_exists_exact "$NAME" && pf_pass "node '$NAME' present (exact match)"
    pf_fail "node '$NAME' NOT in ros2 node list (exact match required - a similarly-named node does not count)"
fi
pf_node_exists "$NAME" && pf_pass "node '$NAME' present"
pf_fail "node '$NAME' NOT in ros2 node list"
