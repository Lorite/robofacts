#!/usr/bin/env bash
# The drone is in a state where it CAN fly — not locked, not tumbled, armable.
#
# Born 2026-07-29, from a trial that recorded 48 s of a drone sitting on the
# ground while the runner reported "flew and recorded". The E-STOP button had
# been pressed earlier in the session; `/all/emergency` sets the firmware
# supervisor's LOCKED state, which persists until the drone is POWER-CYCLED,
# so every takeoff after it is silently ignored. Nothing in ROS complained:
# the trajectory script ran to completion and exited 0.
#
# crazyflie_interfaces/msg/Status carries this as a bitfield, so the state that
# was invisible is in fact published once per status message:
#   1 CAN_BE_ARMED   2 IS_ARMED   4 AUTO_ARM   8 CAN_FLY
#   16 IS_FLYING     32 IS_TUMBLED             64 IS_LOCKED
#
#   drone_armable.sh cf=cf1 [topic=/<cf>/status]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="drone_armable.sh cf=<name> [topic=/<cf>/status]"
pf_parse_args "$@"
pf_require_args cf
CF="$(pf_arg cf)"
pf_require_ros

TOPIC="$(pf_arg topic "/$CF/status")"

INFO="$(timeout 8 ros2 topic echo "$TOPIC" --once 2>/dev/null \
    | grep -oE 'supervisor_info: [0-9]+' | grep -oE '[0-9]+$' | head -1)"

[ -z "${INFO:-}" ] && pf_fail \
    "supervisor state UNREADABLE on $TOPIC (drone off, radio down, or wrong name '$CF')"

# Bit tests. IS_LOCKED first: it is the one that cannot be cleared from here.
if [ $(( INFO & 64 )) -ne 0 ]; then
    pf_fail "drone is LOCKED (latched e-stop, supervisor_info=$INFO) — POWER-CYCLE the drone (battery out/in) and restart the rig; /all/emergency does not clear by itself"
fi
if [ $(( INFO & 32 )) -ne 0 ]; then
    pf_fail "drone is TUMBLED (supervisor_info=$INFO) — set it upright on the pad, then re-check"
fi
if [ $(( INFO & 1 )) -eq 0 ] && [ $(( INFO & 2 )) -eq 0 ]; then
    pf_fail "drone reports it CANNOT be armed (supervisor_info=$INFO) — check the propellers are free, the battery is seated and the drone is level"
fi

pf_pass "drone armable (supervisor_info=$INFO: not locked, not tumbled)"
