#!/usr/bin/env bash
# Generic: a topic is not just advertised, its data is actually flowing.
# "RealSense Node Is Up!" means nothing - flight v2 recorded 0 camera frames
# from an "up" node (2026-07-22).
#
#   topic_rate.sh topic=/poses min_hz=85 [max_hz=<n>] [window_s=10] [required=true]
#
# required=false turns "topic not advertised" into SKIP instead of FAIL, for
# checks that only apply to some experiments.
#
# max_hz (optional, 2026-10-14) requires the rate to stay BELOW it. A rate can
# identify a configuration the rig cannot read any other way: the AI deck's
# GAP8 JPEG build streams decoded frames at 8.0 to 8.2 fps on the same topic
# as the raw build at 10.2 to 11 fps, so a `stream_format` factor checks
# raw with min_hz=9 and jpeg with min_hz=7 max_hz=9. Empty = no upper bound.
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="topic_rate.sh topic=<name> min_hz=<n> [max_hz=<n>  # rate must stay below] [window_s=10] [required=true]"
pf_parse_args "$@"
pf_require_args topic min_hz
TOPIC="$(pf_arg topic)"
MIN_HZ="$(pf_arg min_hz)"
MAX_HZ="$(pf_arg max_hz "")"
pf_require_ros

WINDOW="$(pf_arg window_s 10)"
REQUIRED="$(pf_arg required true)"

if ! pf_topic_exists "$TOPIC"; then
    [ "$REQUIRED" = "false" ] && pf_skip "$TOPIC is not advertised (not used by this experiment)"
    pf_fail "$TOPIC is not advertised at all"
fi

HZ="$(pf_topic_hz "$TOPIC" "$WINDOW")"
if ! pf_ge "$HZ" "$MIN_HZ"; then
    pf_fail "$TOPIC advertised but only ~${HZ} Hz over ${WINDOW}s (need >= ${MIN_HZ}) - publisher stalled or dead"
fi
if [ -n "$MAX_HZ" ]; then
    if pf_ge "$HZ" "$MAX_HZ"; then
        pf_fail "$TOPIC flowing at ~${HZ} Hz over ${WINDOW}s, at or above max_hz=${MAX_HZ} - the source runs faster than the declared configuration allows"
    fi
    pf_pass "$TOPIC flowing at ~${HZ} Hz (>= ${MIN_HZ}, < ${MAX_HZ})"
fi
pf_pass "$TOPIC flowing at ~${HZ} Hz (>= ${MIN_HZ})"
