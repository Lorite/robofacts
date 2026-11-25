#!/usr/bin/env bash
# The battery of the Crazyflie, read over the radio (pm.vbat, the median of 5 samples).
# A check of the cflib example. It opens its own radio link and never arms, so it runs only
# while no other program holds the Crazyradio. Its settings come from config/drone.yaml.
#
#   cf_battery.sh [timeout_s=20]
source "${PF_LIB:-$(python3 -c 'import os, robofacts.preflight as p; print(os.path.join(os.path.dirname(p.__file__), "lib", "common.sh"))')}"
PF_HELP="cf_battery.sh [timeout_s=20]"
pf_parse_args "$@"
LINE="$(timeout "$(pf_arg timeout_s 20)" python3 "$(dirname "${BASH_SOURCE[0]}")/../../../cflib_hover/scripts/cf_link.py" battery 2>&1 | tail -n 1)"
case "$LINE" in
    PASS:*) pf_pass "${LINE#PASS: }" ;;
    FAIL:*) pf_fail "${LINE#FAIL: }" ;;
    *) pf_fail "no PASS or FAIL line from the drone check (timed out after $(pf_arg timeout_s 20) s, or crashed): ${LINE:0:120}" ;;
esac
