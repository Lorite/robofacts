#!/usr/bin/env bash
# Generic: a sensor stream is ALIVE, not only flowing. A topic can publish at its
# full rate while the sensor behind it is stuck: on 2026-11-04 a drone's
# optical-flow sensor came up after a boot with motion.squal 0, motion.shutter
# frozen at 900 and zero flow deltas, while /<robot>/flow_log streamed at 50 Hz and
# the boot log was normal. topic_rate passed it. One radio power cycle fixed it.
#
#   topic_value.sh topic=<name> field=<path> [min=<n>] [max=<n>] [vary=true]
#                  [window_s=2] [min_samples=5] [required=true]
#
# field is a path into the message: `values[2]` (an index into a LogDataGeneric's
# values, in the order of the log block's vars) or a dotted path such as
# `pose.position.z` or `range`. The check echoes the topic for window_s seconds
# and judges every sample it got:
#   min= / max=  the MEDIAN of the samples is at least min / at most max (the
#                median, so one odd sample does not fail a healthy sensor);
#   vary=true    the value is not constant over the window (a frozen register).
# At least one of min, max and vary is required. Fewer than min_samples samples,
# a field that is not in the message, or a value that is not a number is a FAIL.
# required=false turns "topic not advertised" into SKIP.
#
# Example (an optical-flow sensor, flow_log = deltaX, deltaY, squal, shutter, zrange, asl):
#   topic_value.sh topic=/robot1/flow_log field=values[2] min=50
#   topic_value.sh topic=/robot1/flow_log field=values[3] vary=true window_s=2
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="topic_value.sh topic=<name> field=<values[i] | dotted.path> [min=<n>] [max=<n>] [vary=true] [window_s=2] [min_samples=5] [required=true]"
pf_parse_args "$@"
pf_require_args topic field
TOPIC="$(pf_arg topic)"
FIELD="$(pf_arg field)"
MIN="$(pf_arg min "")"
MAX="$(pf_arg max "")"
VARY="$(pf_arg vary false)"
WINDOW="$(pf_arg window_s 2)"
MIN_SAMPLES="$(pf_arg min_samples 5)"
REQUIRED="$(pf_arg required true)"
if [ -z "$MIN" ] && [ -z "$MAX" ] && [ "$VARY" != "true" ]; then
    pf_usage_error "topic_value.sh: give min=, max= or vary=true ($PF_HELP)"
fi
pf_require_ros

if ! pf_topic_exists "$TOPIC"; then
    [ "$REQUIRED" = "false" ] && pf_skip "$TOPIC is not advertised (not used by this experiment)"
    pf_fail "$TOPIC is not advertised at all"
fi

# The echo needs about PF_ECHO_CONNECT_S to attach before the window starts.
ECHO_S="$(awk -v w="$WINDOW" -v c="$PF_ECHO_CONNECT_S" 'BEGIN{printf "%.1f", w + c}')"
RESULT="$(timeout "$ECHO_S" ros2 topic echo "$TOPIC" 2>/dev/null | python3 -c '
import math
import re
import statistics
import sys

import yaml

field, lo, hi, vary, min_samples = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "true", int(sys.argv[5])
if not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*|\[\d+\])*", field):
    print(f"FAIL|field {field!r} is not a path such as values[2] or pose.position.z")
    sys.exit(0)
tokens = re.findall(r"([A-Za-z_]\w*)|\[(\d+)\]", field)
values = []
missing = 0
try:
    docs = list(yaml.safe_load_all(sys.stdin.read()))
except yaml.YAMLError:
    docs = []
for doc in docs:
    if not isinstance(doc, dict):
        continue
    node = doc
    try:
        for name, index in tokens:
            node = node[name] if name else node[int(index)]
        value = float(node)
    except (KeyError, IndexError, TypeError, ValueError):
        missing += 1
        continue
    if math.isfinite(value):
        values.append(value)
if len(values) < min_samples:
    why = f" ({missing} sample(s) without a numeric {field})" if missing else ""
    print(f"FAIL|{len(values)} sample(s) of {field}, need {min_samples}{why}")
    sys.exit(0)
median = statistics.median(values)
detail = (f"{field}: {len(values)} samples, median {median:g}, "
          f"range {min(values):g} to {max(values):g}")
problems = []
if lo != "" and median < float(lo):
    problems.append(f"median {median:g} < min {lo}")
if hi != "" and median > float(hi):
    problems.append(f"median {median:g} > max {hi}")
if vary and max(values) == min(values):
    problems.append(f"constant at {values[0]:g} over the window (a frozen sensor register?)")
print(("FAIL|" + "; ".join(problems) + " | " + detail) if problems else ("PASS|" + detail))
' "$FIELD" "$MIN" "$MAX" "$VARY" "$MIN_SAMPLES" 2>&1 | tail -n 1)"

case "$RESULT" in
    PASS\|*) pf_pass "$TOPIC ${RESULT#PASS|} over ${WINDOW}s" ;;
    FAIL\|*) pf_fail "$TOPIC ${RESULT#FAIL|} over ${WINDOW}s" ;;
    *) pf_fail "topic_value produced no verdict on $TOPIC ($RESULT)" ;;
esac
