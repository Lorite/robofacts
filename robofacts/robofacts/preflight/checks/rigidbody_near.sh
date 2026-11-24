#!/usr/bin/env bash
# The rigid body stands within max_m of a point, sampled NOW, seconds before
# arming. Born 2026-10-29 for the gamepad teleoperation of a drone on an
# external pose: a teleop node (cmd_vel_world) integrates the
# stick velocities from its `initial_position` parameter, with no feedback of
# where the drone really is, so the first setpoint after takeoff drags the drone
# from wherever it took off to that point. A pad 1.2 m from the configured point
# is a 1.2 m dash at 0.5 m above the floor, over whatever stands there (the first
# scripted hover of 2026-10-29 did exactly this toward the origin, over a robot).
#
# The point comes either from the checklist (x=, y=) or, better, from the live
# node that will use it (node=/teleop reads <param_prefix>.x and .y with
# `ros2 param get`), so the gate and the node can never disagree.
#
#   rigidbody_near.sh name=robot1 x=0.0 y=0.0 [max_m=0.25] [topic=/poses] [timeout_s=6]
#   rigidbody_near.sh name=robot1 node=/teleop [param_prefix=initial_position] [max_m=0.25]
#
# The topic is a motion_capture_tracking_interfaces NamedPoseArray (one echo,
# the body picked by name). No sample, a missing body or a non-finite position
# is a FAIL, never a SKIP: a teleop trial without a mocap fix must not arm.
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="rigidbody_near.sh name=<rigid body> (x=<m> y=<m> | node=<node> [param_prefix=initial_position]) [max_m=0.25] [topic=/poses] [timeout_s=6]"
pf_parse_args "$@"
pf_require_args name
NAME="$(pf_arg name)"
X="$(pf_arg x "")"
Y="$(pf_arg y "")"
NODE="$(pf_arg node "")"
PREFIX="$(pf_arg param_prefix initial_position)"
MAX_M="$(pf_arg max_m 0.25)"
TOPIC="$(pf_arg topic /poses)"
TIMEOUT="$(pf_arg timeout_s 6)"
if [ -z "$NODE" ] && { [ -z "$X" ] || [ -z "$Y" ]; }; then
    pf_usage_error "rigidbody_near.sh: give x= and y=, or node= to read them from a live node ($PF_HELP)"
fi
pf_require_ros

SOURCE="x=$X y=$Y from the checklist"
if [ -n "$NODE" ]; then
    read_param() {
        timeout 10 ros2 param get "$NODE" "$1" 2>&1 | sed -n 's/^[A-Za-z ]* value is: //p' | head -n 1
    }
    X="$(read_param "$PREFIX.x")"
    Y="$(read_param "$PREFIX.y")"
    if [ -z "$X" ] || [ -z "$Y" ]; then
        pf_fail "cannot read $PREFIX.x and $PREFIX.y from $NODE (node absent, or the parameters are not set): the setpoint origin of the teleop flight is unknown"
    fi
    SOURCE="x=$X y=$Y from $NODE $PREFIX"
fi

# The program goes in with -c, not as `python3 - <<EOF`: a heredoc REPLACES stdin, so the echo
# piped into it never arrived and the check said "no sample" for every pose (found on the
# Orin 2026-11-10, the drone 1 cm from the origin).
PY_NEAR="$(cat <<'PYEOF'
import math
import sys

import yaml

name, x0, y0, max_m = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
text = sys.stdin.read()
doc = None
for d in yaml.safe_load_all(text):
    if isinstance(d, dict):
        doc = d
        break
if doc is None:
    print("FAIL|no sample (the topic did not answer within the timeout)")
    sys.exit(0)
poses = doc.get("poses") or []
match = [p for p in poses if isinstance(p, dict) and p.get("name") == name]
if not match:
    names = ", ".join(str(p.get("name")) for p in poses if isinstance(p, dict)) or "none"
    print(f"FAIL|rigid body '{name}' not in the sample (bodies: {names})")
    sys.exit(0)
pos = ((match[0].get("pose") or {}).get("position") or {})
try:
    x, y = float(pos.get("x")), float(pos.get("y"))
except (TypeError, ValueError):
    print(f"FAIL|rigid body '{name}' has no readable position in the sample")
    sys.exit(0)
if not (math.isfinite(x) and math.isfinite(y)):
    print(f"FAIL|rigid body '{name}' position is not finite (untracked)")
    sys.exit(0)
d = math.hypot(x - x0, y - y0)
verdict = "PASS" if d <= max_m else "FAIL"
detail = f"'{name}' at x={x:.3f} y={y:.3f}, {d * 100:.0f} cm from the setpoint origin ({x0:.2f}, {y0:.2f})"
if verdict == "FAIL":
    detail += f" (> {max_m * 100:.0f} cm): move the drone onto the pad, or the first setpoint drags it there"
print(f"{verdict}|{detail}")
PYEOF
)"
RESULT="$(timeout "$TIMEOUT" ros2 topic echo "$TOPIC" --once 2>/dev/null | python3 -c "$PY_NEAR" "$NAME" "$X" "$Y" "$MAX_M")"

case "$RESULT" in
    PASS\|*) pf_pass "${RESULT#PASS|} [$SOURCE]" ;;
    FAIL\|*) pf_fail "${RESULT#FAIL|} [$SOURCE]" ;;
    *) pf_fail "rigidbody_near produced no verdict on $TOPIC (no sample within ${TIMEOUT}s, or a python error)" ;;
esac
