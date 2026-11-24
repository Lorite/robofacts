#!/usr/bin/env bash
# A node's own /rosout reports contain what the trial needs, and NOT what it must not have.
#
# Born 2026-10-01: a camera streamer can have a build-time JPEG mode. Flashed with it,
# the stream still "flows" (8.0 to 8.2 fps of decoded frames on image_raw) and every rate check can be satisfied, but the trial records the wrong
# representation. the camera node says which mode it receives in its 5 s rate report: raw
# "10.8 fps, 1234 frames total", JPEG "8.1 fps, 1234 frames total, JPEG 5303 B mean". So the
# evidence is the node's own report: at least one line with `present`, none with `absent`.
#
# Seeing NO report is a FAIL, never a pass: silence would also mean "no JPEG".
#
#   rosout_report.sh node=camera_node present='fps, ' absent=JPEG [window_s=7]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="rosout_report.sh node=<node name substring> present=<text> [absent=<text>] [window_s=7]"
pf_parse_args "$@"
pf_require_args node present
pf_require_ros

NODE="$(pf_arg node)"
PRESENT="$(pf_arg present)"
ABSENT="$(pf_arg absent '')"
WINDOW="$(pf_arg window_s 7)"

# rcl_interfaces/msg/Log prints `name:` before `msg:`, one line each (the echo's YAML width is
# unlimited). Values may be quoted, and a substring match on the node name ignores that.
REPORTS="$(timeout "$WINDOW" ros2 topic echo /rosout 2>/dev/null | awk -v node="$NODE" '
    /^name: / { name = substr($0, 7); next }
    /^msg: /  { if (index(name, node)) print substr($0, 6) }')"

MATCHED="$(printf '%s\n' "$REPORTS" | grep -F -- "$PRESENT" | tail -1)"
[ -z "$MATCHED" ] && pf_fail "no '$PRESENT' report from '$NODE' on /rosout in ${WINDOW} s - is the node running and logging?"
if [ -n "$ABSENT" ]; then
    BAD="$(printf '%s\n' "$REPORTS" | grep -F -- "$ABSENT" | tail -1)"
    [ -n "$BAD" ] && pf_fail "'$NODE' reports: $BAD (contains '$ABSENT')"
fi
pf_pass "'$NODE' reports: $MATCHED"
