#!/usr/bin/env bash
# Generic: a topic is not just advertised, its data is actually flowing.
# "RealSense Node Is Up!" means nothing - flight v2 recorded 0 camera frames
# from an "up" node (2026-07-22).
#
#   topic_rate.sh topic=/poses min_hz=85 [window_s=10] [required=true]
#
# required=false turns "topic not advertised" into SKIP instead of FAIL, for
# checks that only apply to some experiments.
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="topic_rate.sh topic=<name> min_hz=<n> [window_s=10] [required=true]"
pf_parse_args "$@"
pf_require_args topic min_hz
TOPIC="$(pf_arg topic)"
MIN_HZ="$(pf_arg min_hz)"
pf_require_ros

WINDOW="$(pf_arg window_s 10)"
REQUIRED="$(pf_arg required true)"

if ! pf_topic_exists "$TOPIC"; then
    [ "$REQUIRED" = "false" ] && pf_skip "$TOPIC is not advertised (not used by this experiment)"
    pf_fail "$TOPIC is not advertised at all"
fi

HZ="$(pf_topic_hz "$TOPIC" "$WINDOW")"
if pf_ge "$HZ" "$MIN_HZ"; then
    pf_pass "$TOPIC flowing at ~${HZ} Hz (>= ${MIN_HZ})"
fi
pf_fail "$TOPIC advertised but only ~${HZ} Hz over ${WINDOW}s (need >= ${MIN_HZ}) - publisher stalled or dead"
