#!/usr/bin/env bash
# =============================================================================
# Helpers of the ROS 2 pre-flight checks
# =============================================================================
# The core's helpers (argument parsing, PASS, FAIL and SKIP, process counting) plus what
# asks the ROS 2 graph: topic rates, nodes, services. The gate passes this file to every
# check as $PF_LIB when the project runs on the ROS 2 backend, so a project's own check
# that sources $PF_LIB finds both sets.
#
# The core's helper file is $PF_CORE_LIB (set by the gate). Run by hand, it is found next to
# this package in a source checkout, else through Python.
# =============================================================================
if [ -z "${PF_CORE_LIB:-}" ]; then
    # A source checkout of the repository: the general package is next to this one.
    PF_CORE_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../../robofacts/robofacts/preflight/lib/common.sh"
    if [ ! -f "$PF_CORE_LIB" ]; then
        # An installed package: Python knows where it is.
        PF_CORE_LIB="$(python3 -c 'import os, robofacts.preflight as p; print(os.path.join(os.path.dirname(p.__file__), "lib", "common.sh"))' 2>/dev/null)"
    fi
fi
if [ -z "${PF_CORE_LIB:-}" ] || [ ! -f "$PF_CORE_LIB" ]; then
    echo "FAIL: the core helpers of the pre-flight checks were not found (PF_CORE_LIB, or the robofacts package on the Python path)"
    exit 1
fi
# shellcheck source=/dev/null
source "$PF_CORE_LIB"

# `ros2 topic echo` spends roughly this long establishing the subscription
# before the first sample arrives. Ignoring it made a healthy 100 Hz stream
# read exactly-at-threshold and NO-GO two good gates (2026-07-23).
PF_ECHO_CONNECT_S=1.2

# --- environment --------------------------------------------------------------
pf_require_ros() {
    command -v ros2 >/dev/null 2>&1 \
        || pf_fail "ros2 is not on PATH - the workspace is not sourced in this shell"
}

# --- measurement helpers ------------------------------------------------------
# Count messages on a topic over a window, correcting for the subscription
# connect time. Echoes the measured Hz (may be 0).
#
# Counts the '---' separator ros2 topic echo prints after EVERY message, NOT
# header stamps: `--field header.stamp` yields nothing for headerless types
# (std_msgs/String — e.g. an estimator's ~/status), which made a
# topic_rate check read a permanent 0.0 Hz "stalled or dead" against a node
# that was demonstrably firing (2026-07-31, a shadow campaign's T-0
# gate). --no-arr keeps image/array payloads cheap to render.
#
#   pf_topic_hz <topic> <window_s>
pf_topic_hz() {
    local topic="$1" window="$2" lines effective
    lines="$(timeout "$window" ros2 topic echo --no-arr "$topic" 2>/dev/null \
        | grep -c '^---$')"
    effective="$(awk -v w="$window" -v c="$PF_ECHO_CONNECT_S" 'BEGIN{d=w-c; print (d>0)?d:0}')"
    awk -v n="$lines" -v d="$effective" 'BEGIN{ if (d<=0) print 0; else printf "%.1f", n/d }'
}

# True when the topic is advertised at all (cheap existence probe).
pf_topic_exists() {
    timeout 6 ros2 topic list 2>/dev/null | grep -qx "$1"
}

pf_service_exists() {
    timeout 6 ros2 service list 2>/dev/null | grep -q "$1"
}

# Which LIVE node advertises <service> as a service server? Echoes the first
# owning node name, or returns 1 when no live node offers it.
#
# `ros2 service list` is a NAME lookup across the whole DDS domain, so it is
# satisfied by any participant on the network - a foreign machine's stack, or a
# stale endpoint whose node is already gone. On 2026-08-18 the rig saw
# /all/emergency advertised while `ros2 node list` was EMPTY and the only topics
# were /parameter_events and /rosout, and the name-only e-stop gate PASSED
# against a kill path that did not exist. Resolving the owner turns a name match
# back into evidence.
pf_service_owner() {
    local svc="$1" node
    while IFS= read -r node; do
        [ -n "$node" ] || continue
        if timeout 6 ros2 node info "$node" 2>/dev/null \
            | awk '/^  Service Servers:/{f=1; next} /^  [A-Za-z]/{f=0} f {sub(/:.*/, "", $1); print $1}' \
            | grep -q -- "$svc"; then
            echo "$node"
            return 0
        fi
    done < <(timeout 6 ros2 node list 2>/dev/null)
    return 1
}

pf_node_exists() {
    timeout 6 ros2 node list 2>/dev/null | grep -q "$1"
}

# Exact variant: the whole `ros2 node list` line must equal <name> (e.g.
# /detector_node). The substring form above let a pose bridge node
# satisfy a check aimed at the detector while both detector components had
# failed to load (2026-08-12), so any check whose target has similarly-named
# neighbours must use this one.
pf_node_exists_exact() {
    timeout 6 ros2 node list 2>/dev/null | grep -qxF -- "$1"
}
