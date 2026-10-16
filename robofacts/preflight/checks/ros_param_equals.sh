#!/usr/bin/env bash
# A live node's parameter holds the value the trial declares, right now.
#
# Why this exists (2026-10-14, SAC 2027 framework paper, realized conditions).
# Some levels are node parameters that no launch argument of the experiment
# sets: the RealSense depth preset lives in the camera launch's own default
# and the operator changes it at the rig. Without a check, a trial declared
# `visual_preset=3` is a claim nobody verified. This reads the parameter from
# the running node with `ros2 param get`, so a factor can declare it with a
# per-level `expected` value (`check:` in factors.yaml) and the runner adds it
# to the trial's gate with the trial's level.
#
#   ros_param_equals.sh node=/camera/d435 param=depth_module.visual_preset expected=3 [timeout_s=10]
#
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
