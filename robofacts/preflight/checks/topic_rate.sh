#!/usr/bin/env bash
# Generic: a topic is not just advertised, its data is actually flowing.
# "RealSense Node Is Up!" means nothing - flight v2 recorded 0 camera frames
# from an "up" node (2026-07-22).
#
#   topic_rate.sh topic=/poses min_hz=85 [max_hz=<n>] [window_s=10] [required=true]
#
# appear_s=<n> (default 0) waits up to n seconds for the topic to be advertised AND for
# its first message, then measures the rate over window_s. For a source that starts
# sending only once it is ready: OpenVINS creates /ov_msckf/odomimu at its start but
# publishes only after its initializer fired, here on the operator's lift about 20 s
# later (2026-11-04, the K2 VIO gate: "not advertised" before the node was up, then
# "0 Hz over 5 s" before the lift, then 66 Hz once initialized). 0 keeps the old semantics.
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
PF_HELP="topic_rate.sh topic=<name> min_hz=<n> [max_hz=<n>  # rate must stay below] [window_s=10] [appear_s=0  # wait this long for the topic to appear] [required=true]"
pf_parse_args "$@"
pf_require_args topic min_hz
TOPIC="$(pf_arg topic)"
MIN_HZ="$(pf_arg min_hz)"
MAX_HZ="$(pf_arg max_hz "")"
pf_require_ros

WINDOW="$(pf_arg window_s 10)"
REQUIRED="$(pf_arg required true)"
APPEAR="$(pf_arg appear_s 0)"

# Wait for the topic to appear, at most APPEAR seconds (0: ask once, as before).
appear_deadline=$(( $(date +%s) + ${APPEAR%.*} ))
until pf_topic_exists "$TOPIC"; do
    [ "$(date +%s)" -ge "$appear_deadline" ] && break
    sleep 1
done

if [ "${APPEAR%.*}" -gt 0 ] && pf_topic_exists "$TOPIC"; then
    # Advertised: now wait for its first message, for the rest of the appear window.
    remaining=$(( appear_deadline - $(date +%s) ))
    [ "$remaining" -lt 1 ] && remaining=1
    timeout "$remaining" ros2 topic echo --once --no-arr "$TOPIC" >/dev/null 2>&1 \
        || pf_fail "$TOPIC advertised but no message within ${APPEAR}s (the source never started sending)"
fi

if ! pf_topic_exists "$TOPIC"; then
    if [ "${APPEAR%.*}" -gt 0 ]; then
        [ "$REQUIRED" = "false" ] && pf_skip "$TOPIC was not advertised within ${APPEAR}s (not used by this experiment)"
        pf_fail "$TOPIC was not advertised within ${APPEAR}s"
    fi
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
