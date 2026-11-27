#!/usr/bin/env bash
# The GPU has enough free memory for the processes that start next.
#
# GPU inference nodes fail late and opaquely when VRAM is already
# taken (a leftover node from the previous run is the usual culprit - see
# scripts/kill_ros.sh). Checking first turns that into a clear no-go.
#
#   gpu_memory.sh min_free_mb=4096 [index=0]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="gpu_memory.sh min_free_mb=<mb> [index=0] [required=true]"
pf_parse_args "$@"
pf_require_args min_free_mb

MIN_FREE="$(pf_arg min_free_mb)"
INDEX="$(pf_arg index 0)"
REQUIRED="$(pf_arg required true)"

if ! command -v nvidia-smi >/dev/null 2>&1; then
    [ "$REQUIRED" = "false" ] && pf_skip "nvidia-smi not available (no discrete GPU on this host)"
    pf_fail "nvidia-smi not available - cannot verify VRAM, and this experiment needs a GPU"
fi

FREE="$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits -i "$INDEX" 2>/dev/null | tr -dc '0-9')"
[ -z "${FREE:-}" ] && pf_fail "could not read free VRAM for GPU $INDEX"

pf_ge "$FREE" "$MIN_FREE" && pf_pass "GPU $INDEX has ${FREE} MB free (>= ${MIN_FREE} MB)"
pf_fail "GPU $INDEX has only ${FREE} MB free (need >= ${MIN_FREE} MB) - leftover node holding VRAM? run scripts/kill_ros.sh"
