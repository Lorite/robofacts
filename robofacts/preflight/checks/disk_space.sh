#!/usr/bin/env bash
# Enough free disk for the bag. Flight bags here run 6-10 GB; filling the disk
# mid-trial corrupts the recording and wastes the whole flight.
#
#   disk_space.sh [path=.] [min_gb=20]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="disk_space.sh [path=.] [min_gb=20]"
pf_parse_args "$@"

TARGET="$(pf_arg path .)"
MIN_GB="$(pf_arg min_gb 20)"

AVAIL_GB="$(df -BG --output=avail "$TARGET" 2>/dev/null | tail -1 | tr -dc '0-9')"
[ -z "${AVAIL_GB:-}" ] && pf_fail "could not read free space for '$TARGET'"

pf_ge "$AVAIL_GB" "$MIN_GB" && pf_pass "${AVAIL_GB} GB free on '$TARGET' (>= ${MIN_GB} GB)"
pf_fail "only ${AVAIL_GB} GB free on '$TARGET' (need >= ${MIN_GB} GB) - free space or the bag will be truncated"
