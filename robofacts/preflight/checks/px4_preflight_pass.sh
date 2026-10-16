#!/usr/bin/env bash
# PX4 itself reports that it would arm now: pre_flight_checks_pass is true.
#
# Why this exists (SAC 2027 framework paper, E1 phase B, 2026-10-01). With
# the airframe's NAV_DLL_ACT 2 and no ground station, PX4 refuses to arm with
# "Preflight Fail: No connection to the GCS" (the arming blocker found in the
# PX4 SITL feasibility check). Every other check of the PX4 gate passes on such
# a vehicle: the estimator streams, the ground truth flows, the processes are
# all there. offboard_node then waits for pre_flight_checks_pass until its
# mission timeout, so the trial is lost after the recorder has started. This
# check reads the flag PX4 publishes, before anything is recorded.
#
#   px4_preflight_pass.sh [topic=/fmu/out/vehicle_status_v1] [timeout_s=8]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="px4_preflight_pass.sh [topic=/fmu/out/vehicle_status_v1] [timeout_s=8]"
pf_parse_args "$@"
pf_require_ros

TOPIC="$(pf_arg topic /fmu/out/vehicle_status_v1)"
TIMEOUT="$(pf_arg timeout_s 8)"

pf_topic_exists "$TOPIC" || pf_fail "$TOPIC is not advertised, so PX4's arming readiness is unknown"

# PX4 publishes /fmu/out/* best effort, and a best-effort subscription matches both.
VALUE="$(timeout "$TIMEOUT" ros2 topic echo --once --field pre_flight_checks_pass \
    --qos-reliability best_effort "$TOPIC" 2>/dev/null | head -1 | tr -d '[:space:]')"

case "$VALUE" in
    True|true) pf_pass "PX4 reports pre_flight_checks_pass=true on $TOPIC" ;;
    False|false) pf_fail "PX4 reports pre_flight_checks_pass=false on $TOPIC - it will refuse to arm (look for 'Preflight Fail' in the PX4 log, for example 'No connection to the GCS' with NAV_DLL_ACT 2)" ;;
    *) pf_fail "no vehicle_status sample on $TOPIC within ${TIMEOUT}s - readiness unknown" ;;
esac
