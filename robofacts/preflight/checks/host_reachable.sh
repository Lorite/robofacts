#!/usr/bin/env bash
# Another machine answers, and the link is not pathologically slow.
#
# Covers "laptop is on the same network as the MOCAP host" and "WiFi/ethernet
# link to the robot is stable" - both of which otherwise fail deep inside a driver.
#
# NOTE a passing ping does NOT mean DDS discovery works: some WiFi networks drop every
# IP-fragmented UDP datagram while small ICMP passes fine. Use this to
# catch a dead link, not to certify the ROS transport.
#
#   host_reachable.sh host=192.168.50.3 [max_rtt_ms=0] [count=3]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="host_reachable.sh host=<hostname|ip> [max_rtt_ms=0 (0=any)] [count=3]"
pf_parse_args "$@"
pf_require_args host

HOST="$(pf_arg host)"
MAX_RTT="$(pf_arg max_rtt_ms 0)"
COUNT="$(pf_arg count 3)"

OUT="$(ping -c "$COUNT" -W 2 "$HOST" 2>/dev/null)" \
    || pf_fail "$HOST did not answer $COUNT pings (link down, wrong network, or host off)"

AVG="$(echo "$OUT" | awk -F'/' '/rtt|round-trip/ {print $5}')"
[ -z "${AVG:-}" ] && pf_pass "$HOST reachable (rtt unavailable)"

if [ "$MAX_RTT" = "0" ] || pf_ge "$MAX_RTT" "$AVG"; then
    pf_pass "$HOST reachable (avg ${AVG} ms)"
fi
pf_fail "$HOST reachable but slow: avg ${AVG} ms > ${MAX_RTT} ms - link is degraded"
