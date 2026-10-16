#!/usr/bin/env bash
# The configured rigid body is present in nearly EVERY mocap frame, not just
# occasionally. 2026-07-23: a flight launched from a dead zone where the drone
# appeared in 1.0 % of /poses messages, with gaps up to 0.97 s - the EKF flew
# blind and ran away. Presence-on-average is not presence.
#
#   rigidbody_continuity.sh name=cf1 [topic=/poses] [min_pct=90] [window_s=5]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="rigidbody_continuity.sh name=<rigid body> [topic=/poses] [min_pct=90] [window_s=5] [min_frames=100] [cache_s=20]"
pf_parse_args "$@"
pf_require_args name
NAME="$(pf_arg name)"
pf_require_ros

TOPIC="$(pf_arg topic /poses)"
MIN_PCT="$(pf_arg min_pct 90)"
WINDOW="$(pf_arg window_s 5)"
MIN_FRAMES="$(pf_arg min_frames 100)"

CACHE_S="$(pf_arg cache_s 20)"

# ONE CAPTURE, MANY BODIES (2026-08-19). A checklist gates several rigid bodies
# on the same topic, and `name=` only feeds a grep AFTER the capture - it never
# changes what is captured. So the per-body entries were running the IDENTICAL
# `ros2 topic echo` two or three times over, and on this rig only the FIRST one
# ever returns data:
#
#   PASS rigidbody_continuity: 'cfb1_bare' present in 327/327 frames
#   FAIL rigidbody_continuity: only 0 then 0 frames across TWO attempts
#   FAIL rigidbody_continuity: only 0 then 0 frames across TWO attempts
#
# measured in the same gate where mocap_stream clocked /mocap/drone_pose at
# 90.5 Hz and an operator probe showed cfb1_bare, spot, spot_hand and tablet_cam
# all at 408/408. A retry does not help: both attempts come back empty, so this
# is systematic rather than a lost race - something about the first echo's
# teardown stops the next one attaching within the window.
#
# Rather than keep diagnosing DDS teardown on a lab day, take the redundancy
# out: capture once per topic and let the sibling checks read the SAME frames.
# That is also the more correct measurement - three bodies judged against three
# different captures can disagree about one stream, which is incoherent, and
# judging them on identical frames makes the percentages directly comparable.
#
# The cache is deliberately SHORT-LIVED. This framework's first rule is that a
# check which passed minutes ago is worthless, so the window only has to span
# the adjacent entries of one gate (seconds), never two gate runs (minutes).
# The reused age is printed so a stale read can never be silent.
CAP="/tmp/pf_rbc_$(id -u)_$(printf '%s' "$TOPIC" | tr -c 'A-Za-z0-9' '_').cap"

# stdbuf -oL keeps `ros2 topic echo` line-buffered: writing to a FILE makes
# Python block-buffer, and the SIGTERM from `timeout` can then discard the
# trailing partial block.
# NOTE: `grep -c` prints 0 AND exits 1 when nothing matches, so `|| echo 0`
# would emit "0\n0" and every later arithmetic test would blow up. Assign, then
# default only if the assignment itself produced nothing.
pf_frame_count() {
    local n
    n="$(grep -c '^---' "$CAP" 2>/dev/null)" || true
    printf '%s' "${n:-0}"
}

pf_capture() {
    : >"$CAP"
    timeout "$WINDOW" stdbuf -oL ros2 topic echo "$TOPIC" >"$CAP" 2>/dev/null
    pf_frame_count
}

CACHE_AGE=""
if [ -s "$CAP" ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y "$CAP" 2>/dev/null || echo 0) ))
    CACHED_FRAMES="$(pf_frame_count)"
    if [ "$AGE" -le "$CACHE_S" ] && [ "$CACHED_FRAMES" -ge "$MIN_FRAMES" ]; then
        FRAMES="$CACHED_FRAMES"
        CACHE_AGE=" (shared capture, ${AGE}s old)"
    fi
fi

if [ -z "$CACHE_AGE" ]; then
    FRAMES="$(pf_capture)"
    if [ "$FRAMES" -lt "$MIN_FRAMES" ]; then
        FIRST="$FRAMES"
        FRAMES="$(pf_capture)"
        [ "$FRAMES" -lt "$MIN_FRAMES" ] \
            && pf_fail "$TOPIC continuity: only $FIRST then $FRAMES frames in ${WINDOW}s across TWO attempts - the stream really is slow or dead (if another check just measured this stream as healthy, suspect the capture, not the rig)"
    fi
fi

HITS="$(grep -c "name: ${NAME}\$" "$CAP")"

if [ $((HITS * 100)) -ge $((FRAMES * MIN_PCT)) ]; then
    pf_pass "rigid body '$NAME' present in $HITS/$FRAMES frames (>= ${MIN_PCT} %)${CACHE_AGE}"
fi
pf_fail "rigid body '$NAME' present in only $HITS/$FRAMES frames (< ${MIN_PCT} %) - wrong name, or the launch spot is a tracking dead zone"
