#!/usr/bin/env bash
# The emergency-stop service is visible FROM THIS SHELL, and a LIVE NODE offers
# it. Must be run in the exact kill-terminal context: on 2026-07-22 the e-stop
# terminal had no DDS discovery, so the kill command could not have reached the
# drone while everything else looked fine.
#
# Why a name match is not enough (2026-08-18)
# -------------------------------------------
# `ros2 service list` resolves names across the WHOLE DDS domain, so it is
# satisfied by any participant on the network. That morning the rig advertised
# /all/emergency while `ros2 node list` was EMPTY and the only topics were
# /parameter_events and /rosout - a foreign or stale participant. The old
# name-only gate PASSED against a kill path that did not exist, which is exactly
# the "the stop can be aimed at the wrong machine entirely" failure the flight
# checklist says this gate exists to catch.
#
# So the gate now resolves the OWNING NODE, and optionally asserts which node it
# must be - pin `node=` whenever the stack's host is known, because a reachable
# service on somebody else's machine is worse than no service at all: it reads
# as a verified kill path.
#
#   estop_reachable.sh [service=emergency] [node=/driver_server]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="estop_reachable.sh [service=emergency] [node=<expected owning node>]"
pf_parse_args "$@"
pf_require_ros
SVC="$(pf_arg service emergency)"
WANT="$(pf_arg node "")"

pf_service_exists "$SVC" \
    || pf_fail "NO service matching '$SVC' visible from this shell - the kill path is dead (check CYCLONEDDS_URI / ROS_DOMAIN_ID here, not elsewhere)"

OWNER="$(pf_service_owner "$SVC")" \
    || pf_fail "'$SVC' is advertised but NO live node offers it - a stale or foreign participant satisfies the name while the kill path is dead. Check 'ros2 node list' from THIS shell"

if [ -n "$WANT" ] && [ "$OWNER" != "$WANT" ]; then
    pf_fail "'$SVC' is served by '$OWNER', expected '$WANT' - the stop would be aimed at the wrong machine, which is the failure this gate exists to catch"
fi

pf_pass "emergency service matching '$SVC' served by live node $OWNER"
