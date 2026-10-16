#!/usr/bin/env bash
# =============================================================================
# Kill the ROS 2 processes of your ROS_DOMAIN_ID - clean slate before a new test
# =============================================================================
# Half of the "mystery" failures in this workspace are leftovers from the
# previous run, not bugs in the new one. Observed repeatedly:
#
#   - a previous rig still holding the RealSense D435, so the new one starts
#     with an "up" camera node that never produces frames (2026-07-23)
#   - stacked motion_capture_tracking clients on UDP 1511 starving every new
#     NatNet client, which looks exactly like "Motive is unreachable"
#     (2026-07-23; see docs/TROUBLESHOOTING_REFERENCE.md fault C)
#   - a stale ros2 daemon making `ros2 node list` / `service list` silently
#     empty, which NO-GO'd a healthy stack four times (2026-07-22)
#   - orphaned component_container processes still publishing on the topics the
#     new run is about to record
#
# Run this BEFORE launching a new test. It is deliberately blunt: everything
# ROS-ish on your ROS_DOMAIN_ID dies, then the daemon is restarted fresh.
#
# Only your domain, since 2026-10-15. Several sessions and agents share one
# container, each on its own ROS_DOMAIN_ID, and the old kill-everything run
# stopped their trials in the middle. A process belongs to a domain by the
# ROS_DOMAIN_ID in its environment (/proc/<pid>/environ, unset means 0, as in
# ROS 2), which an orphan keeps after its launch dies, so a reparented gz sim of
# your previous run is still found. Processes on other domains are listed, not
# killed: a camera driver or a NatNet client started on another domain still
# holds its device, and the list is how you find it. --all kills them too.
#
# It will NOT kill this script, its ancestors (your shell, the VS Code server,
# sshd), so it is safe to run from an integrated terminal.
# =============================================================================

set -uo pipefail

DRY_RUN=0
GRACE_S=3
VERBOSE=0
ALL_DOMAINS=0
MY_DOMAIN="${ROS_DOMAIN_ID:-0}"

usage() {
    cat <<'EOF'
Usage: scripts/kill_ros.sh [--all] [--dry-run] [--grace <seconds>] [--verbose]

  --all            Kill the ROS processes of every ROS_DOMAIN_ID, not only yours.
  --dry-run        List what would be killed, kill nothing. Exit 0 always.
  --grace <s>      Seconds to wait after SIGTERM before SIGKILL (default: 3).
  --verbose        Print the full command line of every matched process.
  -h, --help       Show this help.

Kills the ROS 2 nodes, launches, bag recorders, RViz, Gazebo/gz, the RealSense and
motion-capture drivers and component containers of your ROS_DOMAIN_ID (unset
means 0), then restarts the ros2 daemon of that domain. Processes on other
domains are listed and left alone, unless --all. Safe to run from a terminal you
are sitting in.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --all) ALL_DOMAINS=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --grace) GRACE_S="$2"; shift 2 ;;
        --verbose) VERBOSE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# -----------------------------------------------------------------------------
# Never kill ourselves or anything we are running inside of (shell, VS Code
# server, sshd, the docker exec). Walk the ppid chain up to init.
# -----------------------------------------------------------------------------
declare -A PROTECTED=()
probe=$$
while [ -n "$probe" ] && [ "$probe" -gt 1 ] 2>/dev/null; do
    PROTECTED["$probe"]=1
    probe="$(ps -o ppid= -p "$probe" 2>/dev/null | tr -d ' ')"
done

# -----------------------------------------------------------------------------
# What counts as a ROS process.
#
# Most nodes live under /opt/ros/<distro>/ or a workspace install/build tree,
# which catches ros2 launch/run/bag, rviz2, robot_state_publisher and every
# workspace node. Any colcon workspace named *_ws counts, not only ros2_ws:
# an overlay such as ~/ov_ws (OpenVINS, 2026-10-14) left an orphaned
# run_subscribe_msckf that the next trial's gate then refused. The explicit
# patterns below cover the ones that do not: the daemon (a bare python
# module), Gazebo, and vendor binaries.
# -----------------------------------------------------------------------------
PATTERNS=(
    '/opt/ros/'
    '_ws/install/'
    '_ws/build/'
    '_ros2_daemon'
    'ros2 daemon'
    'component_container'
    'realsense2_camera_node'
    'motion_capture_tracking'
    'crazyflie_server'
    'crazyswarm'
    'micro_ros_agent'
    'MicroXRCEAgent'
    'px4_sitl_default/bin/px4'
    'rviz2'
    'gzserver'
    'gzclient'
    'ign gazebo'
    'gz sim'
    'parameter_bridge'
    'ros_gz_bridge'
)

matches() {
    local args="$1"
    local p
    for p in "${PATTERNS[@]}"; do
        case "$args" in *"$p"*) return 0 ;; esac
    done
    return 1
}

# The ROS_DOMAIN_ID a process runs on: from its environment, 0 when unset (the
# ROS 2 default), "?" when the environment cannot be read (another user's
# process). A "?" process is never killed without --all.
domain_of() {
    local env_file="/proc/$1/environ"
    [ -r "$env_file" ] || { echo "?"; return; }
    local value
    value="$(tr '\0' '\n' < "$env_file" 2>/dev/null | sed -n 's/^ROS_DOMAIN_ID=//p' | head -n 1)"
    echo "${value:-0}"
}

show() {
    local pid="$1" args="$2" note="$3"
    if [ "$VERBOSE" -eq 1 ]; then
        echo "  $pid  $note$args"
    else
        echo "  $pid  $note$(echo "$args" | cut -c1-100)"
    fi
}

# -----------------------------------------------------------------------------
# Collect victims.
# -----------------------------------------------------------------------------
PIDS=()
DESCS=()
DOMAINS=()
OTHER_PIDS=()
OTHER_DESCS=()
OTHER_DOMAINS=()
SELF_NAME="$(basename "${BASH_SOURCE[0]}")"

while read -r pid args; do
    [ -z "${pid:-}" ] && continue
    [ -n "${PROTECTED[$pid]+x}" ] && continue
    # Don't match on our own command line (it contains every pattern above).
    case "$args" in *"$SELF_NAME"*) continue ;; esac
    matches "$args" || continue
    domain="$(domain_of "$pid")"
    if [ "$ALL_DOMAINS" -eq 1 ] || [ "$domain" = "$MY_DOMAIN" ]; then
        PIDS+=("$pid")
        DESCS+=("$args")
        DOMAINS+=("$domain")
    else
        OTHER_PIDS+=("$pid")
        OTHER_DESCS+=("$args")
        OTHER_DOMAINS+=("$domain")
    fi
done < <(ps -eo pid=,args= 2>/dev/null)

if [ "$ALL_DOMAINS" -eq 1 ]; then
    scope="on every ROS_DOMAIN_ID (--all)"
else
    scope="on ROS_DOMAIN_ID=$MY_DOMAIN"
fi
if [ ${#PIDS[@]} -eq 0 ]; then
    echo "✓ No ROS processes $scope - already a clean slate."
else
    echo "Found ${#PIDS[@]} ROS process(es) $scope:"
    for i in "${!PIDS[@]}"; do
        if [ "$ALL_DOMAINS" -eq 1 ]; then
            show "${PIDS[$i]}" "${DESCS[$i]}" "[domain ${DOMAINS[$i]}] "
        else
            show "${PIDS[$i]}" "${DESCS[$i]}" ""
        fi
    done
fi
if [ ${#OTHER_PIDS[@]} -gt 0 ]; then
    echo "Left alone, ${#OTHER_PIDS[@]} ROS process(es) on other domains (another session's,"
    echo "or a leftover that may still hold a device; --all kills them too):"
    for i in "${!OTHER_PIDS[@]}"; do
        show "${OTHER_PIDS[$i]}" "${OTHER_DESCS[$i]}" "[domain ${OTHER_DOMAINS[$i]}] "
    done
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo ""
    echo "--dry-run: nothing killed."
    exit 0
fi

# -----------------------------------------------------------------------------
# SIGTERM, grace, then SIGKILL whatever is still standing.
# -----------------------------------------------------------------------------
if [ ${#PIDS[@]} -gt 0 ]; then
    kill -TERM "${PIDS[@]}" 2>/dev/null
    sleep "$GRACE_S"

    SURVIVORS=()
    for pid in "${PIDS[@]}"; do
        kill -0 "$pid" 2>/dev/null && SURVIVORS+=("$pid")
    done

    if [ ${#SURVIVORS[@]} -gt 0 ]; then
        echo "  ${#SURVIVORS[@]} process(es) ignored SIGTERM - sending SIGKILL"
        kill -KILL "${SURVIVORS[@]}" 2>/dev/null
        sleep 1
    fi

    STILL=()
    for pid in "${PIDS[@]}"; do
        kill -0 "$pid" 2>/dev/null && STILL+=("$pid")
    done
    if [ ${#STILL[@]} -gt 0 ]; then
        echo "  ⚠ still alive after SIGKILL (not ours to kill?): ${STILL[*]}" >&2
    fi
fi

# -----------------------------------------------------------------------------
# Fresh daemon. A stale daemon reports an empty node/service list on a perfectly
# healthy stack, which reads as "everything is dead" (2026-07-22). `ros2 daemon`
# acts on the daemon of the caller's ROS_DOMAIN_ID only.
# -----------------------------------------------------------------------------
if command -v ros2 >/dev/null 2>&1; then
    ros2 daemon stop >/dev/null 2>&1
    sleep 1
    ros2 daemon start >/dev/null 2>&1
    echo "✓ ros2 daemon restarted"
else
    echo "  (ros2 not on PATH - skipped daemon restart; source the workspace first)"
fi

echo "✓ Clean slate."
