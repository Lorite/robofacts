#!/usr/bin/env bash
# =============================================================================
# Kill every ROS 2 process in this container - clean slate before a new test
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
# ROS-ish in this container dies, then the daemon is restarted fresh.
#
# It will NOT kill this script, its ancestors (your shell, the VS Code server,
# sshd), so it is safe to run from an integrated terminal.
# =============================================================================

set -uo pipefail

DRY_RUN=0
GRACE_S=3
VERBOSE=0

usage() {
    cat <<'EOF'
Usage: scripts/kill_ros.sh [--dry-run] [--grace <seconds>] [--verbose]

  --dry-run        List what would be killed, kill nothing. Exit 0 always.
  --grace <s>      Seconds to wait after SIGTERM before SIGKILL (default: 3).
  --verbose        Print the full command line of every matched process.
  -h, --help       Show this help.

Kills ROS 2 nodes, launches, bag recorders, RViz, Gazebo/gz, the RealSense and
motion-capture drivers, component containers and the ros2 daemon, then starts a
fresh daemon. Safe to run from a terminal you are sitting in.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
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
# Most nodes live under /opt/ros/<distro>/ or the workspace install/build tree,
# which catches ros2 launch/run/bag, rviz2, robot_state_publisher and every
# workspace node. The explicit patterns below cover the ones that do not:
# the daemon (a bare python module), Gazebo, and vendor binaries.
# -----------------------------------------------------------------------------
PATTERNS=(
    '/opt/ros/'
    'ros2_ws/install/'
    'ros2_ws/build/'
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

# -----------------------------------------------------------------------------
# Collect victims.
# -----------------------------------------------------------------------------
PIDS=()
DESCS=()
SELF_NAME="$(basename "${BASH_SOURCE[0]}")"

while read -r pid args; do
    [ -z "${pid:-}" ] && continue
    [ -n "${PROTECTED[$pid]+x}" ] && continue
    # Don't match on our own command line (it contains every pattern above).
    case "$args" in *"$SELF_NAME"*) continue ;; esac
    matches "$args" || continue
    PIDS+=("$pid")
    DESCS+=("$args")
done < <(ps -eo pid=,args= 2>/dev/null)

if [ ${#PIDS[@]} -eq 0 ]; then
    echo "✓ No ROS processes running - already a clean slate."
else
    echo "Found ${#PIDS[@]} ROS process(es):"
    for i in "${!PIDS[@]}"; do
        if [ "$VERBOSE" -eq 1 ]; then
            echo "  ${PIDS[$i]}  ${DESCS[$i]}"
        else
            echo "  ${PIDS[$i]}  $(echo "${DESCS[$i]}" | cut -c1-100)"
        fi
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
# healthy stack, which reads as "everything is dead" (2026-07-22).
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
