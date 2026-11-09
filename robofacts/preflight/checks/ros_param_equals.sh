#!/usr/bin/env bash
# A live node's parameter holds the value the trial declares, right now.
#
# Why this exists (2026-10-14, realized conditions).
# Some levels are node parameters that no launch argument of the experiment
# sets: a depth camera's preset lives in the camera launch's own default
# and the operator changes it at the rig. Without a check, a trial declared
# `visual_preset=3` is a claim nobody verified. This reads the parameter from
# the running node with `ros2 param get`, so a factor can declare it with a
# per-level `expected` value (`check:` in factors.yaml) and the runner adds it
# to the trial's gate with the trial's level.
#
#   ros_param_equals.sh node=/camera param=depth_module.visual_preset expected=3 [timeout_s=10]
#
# Arrays (2026-11-09): Humble prints `Double values are:
# array('d', [-0.6, 0.7])` (or a plain list for strings); declare `expected=[-0.6, 0.7]` and the
# lists compare element by element, numbers numerically, the rest as text.
# Numbers compare numerically (4 equals 4.0), everything else as text, case
# insensitive (True equals true). An empty `expected` FAILS ("not declared"),
# so an undeclared level can never pass. A node that is absent, a parameter
# that is not set, or a reply that cannot be read is a FAIL, never a SKIP.
# The value a node REPORTS is what it was asked for: a driver that failed to
# apply it to the device still reports the requested value.
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="ros_param_equals.sh node=<node> param=<name> expected=<value> [timeout_s=10]"
pf_parse_args "$@"
pf_require_args node param expected
NODE="$(pf_arg node)"
PARAM="$(pf_arg param)"
EXPECTED="$(pf_arg expected)"
TIMEOUT="$(pf_arg timeout_s 10)"
pf_require_ros

[ -n "$EXPECTED" ] || pf_fail "no expected value declared for $NODE $PARAM (null = not declared yet)"

OUT="$(timeout "$TIMEOUT" ros2 param get "$NODE" "$PARAM" 2>&1)"
RC=$?
[ "$RC" -eq 124 ] && pf_fail "ros2 param get $NODE $PARAM did not answer within ${TIMEOUT}s (node absent or its parameter service blocked)"
# `ros2 param get` prints "<Type> value is: <value>" (Humble): Integer, Double,
# String, Boolean, and the array types.
VALUE="$(printf '%s\n' "$OUT" | sed -n 's/^[A-Za-z ]* value is: //p' | head -n 1)"
ARRAY="$(printf '%s\n' "$OUT" | sed -n 's/^[A-Za-z ]* values are: //p' | head -n 1)"
if [ -n "$ARRAY" ]; then
    VALUE="$(printf '%s' "$ARRAY" | sed -E "s/^array\('[a-zA-Z]', (\[.*\])\)$/\1/")"
    if python3 -c '
import ast, sys
try:
    a, b = ast.literal_eval(sys.argv[1]), ast.literal_eval(sys.argv[2])
except (ValueError, SyntaxError):
    sys.exit(1)
def eq(x, y):
    try:
        return float(x) == float(y)
    except (TypeError, ValueError):
        return str(x).lower() == str(y).lower()
sys.exit(0 if isinstance(b, (list, tuple)) and len(a) == len(b) and all(eq(x, y) for x, y in zip(a, b)) else 1)
' "$VALUE" "$EXPECTED" 2>/dev/null; then
        pf_pass "$NODE $PARAM = $VALUE (expected $EXPECTED)"
    fi
    pf_fail "$NODE $PARAM = $VALUE, but the trial declares $EXPECTED"
fi
if [ -z "$VALUE" ] && ! printf '%s\n' "$OUT" | grep -q ' value is: '; then
    pf_fail "cannot read $NODE $PARAM: $(printf '%s' "$OUT" | tr '\n' ' ' | cut -c1-200)"
fi

NUMBER='^[-+]?[0-9]+([.][0-9]*)?([eE][-+]?[0-9]+)?$'
if [[ "$VALUE" =~ $NUMBER ]] && [[ "$EXPECTED" =~ $NUMBER ]]; then
    if awk -v a="$VALUE" -v b="$EXPECTED" 'BEGIN{exit !(a+0 == b+0)}'; then
        pf_pass "$NODE $PARAM = $VALUE (expected $EXPECTED)"
    fi
elif [ "${VALUE,,}" = "${EXPECTED,,}" ]; then
    pf_pass "$NODE $PARAM = $VALUE (expected $EXPECTED)"
fi
pf_fail "$NODE $PARAM = $VALUE, but the trial declares $EXPECTED"
