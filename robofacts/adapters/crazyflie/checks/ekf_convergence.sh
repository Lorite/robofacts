#!/usr/bin/env bash
# Onboard EKF agrees with MOCAP to within a few cm, sampled NOW. This is the
# check that cannot be front-loaded: it needs a live drone sample, so it must
# run seconds before arming (skipping it before the 2026-07-23 shadow flight is
# what let a blind estimator take off).
#
#   ekf_convergence.sh cf=cf1 [max_err_m=0.05] [gt_topic=/poses]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../../../preflight/lib/common.sh}"
PF_HELP="ekf_convergence.sh cf=<name> [max_err_m=0.05] [gt_topic=/poses] [ekf_topic=/<cf>/pose]"
pf_parse_args "$@"
pf_require_args cf
CF="$(pf_arg cf)"
pf_require_ros

MAX_ERR="$(pf_arg max_err_m 0.05)"
GT_TOPIC="$(pf_arg gt_topic /poses)"
EKF_TOPIC="$(pf_arg ekf_topic "/$CF/pose")"

RESULT="$(python3 - "$EKF_TOPIC" "$GT_TOPIC" "$MAX_ERR" <<'PYEOF'
import re
import subprocess
import sys

ekf_topic, gt_topic, max_err = sys.argv[1], sys.argv[2], float(sys.argv[3])


def xyz(topic, timeout_s):
    cmd = f"timeout {timeout_s} ros2 topic echo {topic} --once 2>/dev/null | grep -A3 'position:'"
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout
    vals = re.findall(r"[xyz]: (-?\d+\.?\d*(?:e-?\d+)?)", out)
    return [float(v) for v in vals[:3]] if len(vals) >= 3 else None


ekf = xyz(ekf_topic, 8)
gt = xyz(gt_topic, 6)
if ekf is None:
    print(f"FAIL|onboard EKF pose not publishing on {ekf_topic}")
elif gt is None:
    print(f"FAIL|no mocap sample on {gt_topic}")
else:
    err = sum((a - b) ** 2 for a, b in zip(ekf, gt)) ** 0.5
    verdict = "PASS" if err < max_err else "FAIL"
    detail = f"EKF-vs-MOCAP {err * 1000:.0f} mm"
    if verdict == "FAIL":
        detail += f" (> {max_err * 1000:.0f} mm) - estimator stale or diverged"
    print(f"{verdict}|{detail}")
PYEOF
)"

case "$RESULT" in
    PASS\|*) pf_pass "${RESULT#PASS|}" ;;
    FAIL\|*) pf_fail "${RESULT#FAIL|}" ;;
    *) pf_fail "ekf_convergence check produced no verdict (python error?)" ;;
esac
