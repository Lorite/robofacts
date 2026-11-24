#!/usr/bin/env bash
# A required file is present (and optionally non-trivial in size).
#
# Built for the artifacts that take 10+ minutes to regenerate and whose absence
# only shows up as an opaque vendor-node failure minutes into a session:
# TensorRT .plan engines, prepared meshes, calibration maps, driver configs.
#
#   file_exists.sh path=/ssd/engines/refine.plan [min_mb=1] [label=refine engine]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="file_exists.sh path=<path> [min_mb=0] [label=<human name>] [required=true]"
pf_parse_args "$@"
pf_require_args path

TARGET="$(pf_arg path)"
MIN_MB="$(pf_arg min_mb 0)"
LABEL="$(pf_arg label "$TARGET")"
REQUIRED="$(pf_arg required true)"

if [ ! -e "$TARGET" ]; then
    [ "$REQUIRED" = "false" ] && pf_skip "$LABEL not present (optional for this trial)"
    pf_fail "$LABEL MISSING at $TARGET"
fi

if [ -d "$TARGET" ]; then
    pf_pass "$LABEL present (directory $TARGET)"
fi

SIZE_MB="$(awk -v b="$(stat -c %s "$TARGET" 2>/dev/null || echo 0)" 'BEGIN{printf "%.1f", b/1048576}')"
if pf_ge "$SIZE_MB" "$MIN_MB"; then
    pf_pass "$LABEL present (${SIZE_MB} MB)"
fi
pf_fail "$LABEL is only ${SIZE_MB} MB (expected >= ${MIN_MB} MB) - truncated or a failed build?"
