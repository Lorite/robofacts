#!/usr/bin/env bash
# Nothing is left over from the previous run. Leftovers are the single most
# common cause of "mystery" failures here: a previous rig still holding the
# D435 so the new camera node never produces frames; stacked
# motion_capture_tracking clients starving every new NatNet client; orphaned
# component_containers publishing onto the topics we are about to record.
#
# This check REPORTS; it does not kill. Run scripts/kill_ros.sh before the
# launch, then this verifies the slate is actually clean.
#
#   ros_clean_slate.sh [max_duplicates=1] [pattern=motion_capture_tracking,realsense2_camera_node]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="ros_clean_slate.sh [pattern=a,b,c] [max_each=1]  # max_each = instances allowed per pattern; the CURRENT rig is 1, so >1 means a leftover"
pf_parse_args "$@"

PATTERNS="$(pf_arg pattern 'motion_capture_tracking,realsense2_camera_node,component_container')"
MAX_EACH="$(pf_arg max_each 1)"

OFFENDERS=""
IFS=',' read -ra PATS <<<"$PATTERNS"
for pat in "${PATS[@]}"; do
    [ -z "$pat" ] && continue
    n="$(pf_count_processes "$pat")"
    if [ "$n" -gt "$MAX_EACH" ]; then
        OFFENDERS="${OFFENDERS}${OFFENDERS:+, }${pat} x${n}"
    fi
done

[ -n "$OFFENDERS" ] \
    && pf_fail "duplicate processes from a previous run: $OFFENDERS - run scripts/kill_ros.sh and relaunch"
pf_pass "no duplicate ROS processes (checked: ${PATTERNS//,/, })"
