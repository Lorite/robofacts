#!/usr/bin/env bash
# Camera FRAMES are flowing, not merely a camera node being up.
# Optional by default: mocap-only flights have no camera.
#
#   camera_frames.sh [topic=/camera/color/image_raw] [min_hz=20] [required=false]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="camera_frames.sh [topic=/camera/color/image_raw] [min_hz=20] [window_s=6] [required=false]"
pf_parse_args "$@"
exec "$(dirname "${BASH_SOURCE[0]}")/topic_rate.sh" \
    "topic=$(pf_arg topic /camera/color/image_raw)" \
    "min_hz=$(pf_arg min_hz 20)" \
    "window_s=$(pf_arg window_s 6)" \
    "required=$(pf_arg required false)"
