#!/usr/bin/env bash
# The motion-capture stream publishes at least at its minimum rate at the time of the
# check. Named entry point for the
# human checklist item "MOCAP server running and streaming".
#
#   mocap_stream.sh [topic=/poses] [min_hz=85] [window_s=10]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="mocap_stream.sh [topic=/poses] [min_hz=85] [window_s=10]"
pf_parse_args "$@"
exec "$(dirname "${BASH_SOURCE[0]}")/topic_rate.sh" \
    "topic=$(pf_arg topic /poses)" \
    "min_hz=$(pf_arg min_hz 85)" \
    "window_s=$(pf_arg window_s 10)"
