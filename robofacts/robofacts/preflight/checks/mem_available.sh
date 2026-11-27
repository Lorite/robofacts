#!/usr/bin/env bash
# The recording host has enough free memory at the time of the check.
#
# Born 2026-10-01: the laptop reached 29 of 30 GiB used with a load of 21 while it
# streamed a camera, and that corrupted a latency run. A host that is swapping drops frames
# and stamps late, and nothing else in the gate notices, because every stream still "flows".
# Reads MemAvailable from /proc/meminfo. Inside a dev container that is the HOST's memory (the
# kernel is shared), which is the number that matters here.
#
#   mem_available.sh [min_gb=4]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="mem_available.sh [min_gb=4]"
pf_parse_args "$@"

MIN_GB="$(pf_arg min_gb 4)"
AVAIL_KB="$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo 2>/dev/null)"
[ -z "${AVAIL_KB:-}" ] && pf_fail "could not read MemAvailable from /proc/meminfo"
AVAIL_GB="$(awk -v k="$AVAIL_KB" 'BEGIN{printf "%.1f", k / 1048576}')"

pf_ge "$AVAIL_GB" "$MIN_GB" && pf_pass "${AVAIL_GB} GiB of memory available (>= ${MIN_GB} GiB)"
pf_fail "only ${AVAIL_GB} GiB of memory available (need >= ${MIN_GB} GiB) - close browsers, notebooks and stale nodes before recording"
