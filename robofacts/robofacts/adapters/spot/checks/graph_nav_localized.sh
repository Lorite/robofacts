#!/usr/bin/env bash
# Spot is LOCALIZED to its GraphNav map, not merely running the node.
#
# 2026-08-13: the rehearsal/flight gates checked that graph_nav_map_tf was
# present and publishing, which a de-localized robot can still look like for a
# while, and left "is Spot actually localized" as an operator confirmation. On
# the same day a `list_graph` call re-uploaded the map mid-session and silently
# DE-LOCALIZED the robot; the node caught it (it refuses the all-zero
# quaternion an unlocalized robot returns) but nothing in the gate would have.
# Outside the motion-capture volume a de-localized Spot means the estimator has
# no world to publish into, so the whole trial stops being a rehearsal of
# anything -- and that is the one campaign with no ground truth to notice.
#
# Reads the node's own status line, which already carries the verdict:
#   localized age=0.1s src=service tf=graph_nav_map->... n_pub=169 n_rej=1 n_frozen=0
#
#   graph_nav_localized.sh [topic=/graph_nav_map_tf/status] [max_age_s=2.0] [max_frozen=0]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../../../preflight/lib/common.sh}"
PF_HELP="graph_nav_localized.sh [topic=/graph_nav_map_tf/status] [max_age_s=2.0] [max_frozen=0]"
pf_parse_args "$@"
pf_require_ros

TOPIC="$(pf_arg topic /graph_nav_map_tf/status)"
MAX_AGE="$(pf_arg max_age_s 2.0)"
MAX_FROZEN="$(pf_arg max_frozen 0)"

LINE="$(timeout 8 ros2 topic echo "$TOPIC" --field data --once 2>/dev/null | head -1 | tr -d "'\"")"
[ -z "$LINE" ] && pf_fail "$TOPIC published NOTHING - graph_nav_map_tf is absent or silent (start the rig with spot_map_frame_source:=graph_nav)"

case "$LINE" in
    localized*) : ;;
    not_localized*) pf_fail "Spot is NOT LOCALIZED to the map. Upload it and localize to a fiducial before the trial: $LINE" ;;
    *) pf_fail "unreadable status from $TOPIC: '$LINE'" ;;
esac

# age=<x>s — how stale the correction is. A frozen map alignment reports a
# recent-looking pose that stopped tracking the robot.
AGE="$(printf '%s\n' "$LINE" | grep -oE 'age=[0-9.]+' | head -1 | cut -d= -f2)"
if [ -n "$AGE" ] && awk "BEGIN{exit !($AGE > $MAX_AGE)}"; then
    pf_fail "localization is STALE (age=${AGE}s > ${MAX_AGE}s): the correction stopped updating - $LINE"
fi

FROZEN="$(printf '%s\n' "$LINE" | grep -oE 'n_frozen=[0-9]+' | head -1 | cut -d= -f2)"
if [ -n "$FROZEN" ] && [ "$FROZEN" -gt "$MAX_FROZEN" ] 2>/dev/null; then
    pf_fail "map alignment has FROZEN $FROZEN time(s) (max $MAX_FROZEN) - $LINE"
fi

pf_pass "Spot LOCALIZED to its GraphNav map (${AGE:-?}s old, frozen ${FROZEN:-?}) - $LINE"
