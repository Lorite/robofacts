#!/usr/bin/env bash
# The localization watchdog is EFFECTIVE, not merely present.
# 2026-07-23: the node existed but watched a rigid body that appeared in ~1 %
# of /poses (name mismatch), so it never armed and never landed a runaway.
# It now publishes a latched health beacon; only armed / disarmed-ok are OK.
#
#   watchdog_health.sh [topic_match=watchdog/health]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="watchdog_health.sh [topic_match=watchdog/health]"
pf_parse_args "$@"
pf_require_ros

MATCH="$(pf_arg topic_match watchdog/health)"
TOPIC="$(timeout 6 ros2 topic list 2>/dev/null | grep -m1 "$MATCH")"
[ -z "$TOPIC" ] && pf_fail "watchdog health topic matching '$MATCH' MISSING (node absent, or pre-hardening build)"

STATE="$(timeout 8 ros2 topic echo "$TOPIC" \
    --qos-reliability reliable --qos-durability transient_local \
    --field data --once 2>/dev/null | head -1 | tr -d "'\"" | tr -d '[:space:]')"

case "$STATE" in
    armed|disarmed-ok) pf_pass "watchdog EFFECTIVE (health: $STATE)" ;;
    BLIND*)   pf_fail "watchdog BLIND - it is NOT protecting this flight ($STATE)" ;;
    tripped)  pf_fail "watchdog TRIPPED latch still set - restart the stack" ;;
    starting) pf_fail "watchdog still starting (rigid body not yet sighted) - re-run the gate" ;;
    *)        pf_fail "watchdog health UNREADABLE (got '${STATE:-nothing}')" ;;
esac
