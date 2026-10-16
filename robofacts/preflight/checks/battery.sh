#!/usr/bin/env bash
# Battery voltage at or above the launch threshold. UNREADABLE IS A FAIL:
# a battery you cannot read is a battery you cannot trust.
#
#   battery.sh cf=cf1 [min_v=3.9] [topic=/<cf>/status]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="battery.sh cf=<name> [min_v=3.9] [topic=/<cf>/status]"
pf_parse_args "$@"
pf_require_args cf
CF="$(pf_arg cf)"
pf_require_ros

MIN_V="$(pf_arg min_v 3.9)"
TOPIC="$(pf_arg topic "/$CF/status")"

V="$(timeout 8 ros2 topic echo "$TOPIC" --once 2>/dev/null \
    | grep -oE 'battery_voltage: [0-9.]+' | grep -oE '[0-9.]+$' | head -1)"

[ -z "${V:-}" ] && pf_fail "battery voltage UNREADABLE on $TOPIC (drone off, radio down, or wrong name '$CF')"
pf_ge "$V" "$MIN_V" && pf_pass "battery ${V} V (>= ${MIN_V})"
pf_fail "battery ${V} V is below the ${MIN_V} V launch threshold"
