#!/usr/bin/env bash
# No operator stop latch younger than 120 s (2026-10-12, SAC 2027 framework paper).
#
# Every robots.yaml stop command writes the stop latch FIRST. On the lab
# day of 2026-10-07 the T-0 gate passed 62 s after a latch was written, because
# only the setup step and the flight action's start read it. This check puts the
# same rule in the gate, so a fresh latch refuses before the operator is asked to
# pose the rig. The 120 s limit is NOT an argument: it is STOP_LATCH_MAX_AGE_S in
# experiments/common/scripts/stop_latch_clear.py, the one constant the setup step
# and the flight actions use too. The detail ends with age_s=<seconds> (or
# age_s=none), the measured value the gate log and the trial metadata keep.
#
#   stop_latch.sh [path=<latch file>]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="stop_latch.sh [path=<latch file, default \$TRIALS_STOP_LATCH, else trials.yaml stop_latch, else /tmp/trial_stop_latch>]"
pf_parse_args "$@"

LATCH="$(pf_arg path "${TRIALS_STOP_LATCH:-}")"   # empty: stop_latch_clear.py resolves it (trials.yaml, then the default)
SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/stop_latch_clear.py"
[ -f "$SCRIPT" ] || pf_fail "stop latch rule UNREADABLE: $SCRIPT missing"

if [ -n "$LATCH" ]; then
    LINE="$(python3 "$SCRIPT" --gate --latch "$LATCH" 2>&1)"
else
    LINE="$(python3 "$SCRIPT" --gate 2>&1)"
fi
RC=$?
LINE="$(printf '%s\n' "$LINE" | tail -n 1)"
case "$RC:$LINE" in
    0:PASS:\ *) pf_pass "${LINE#PASS: }" ;;
    1:FAIL:\ *) pf_fail "${LINE#FAIL: }" ;;
    *) pf_fail "stop latch UNREADABLE (rc=$RC): $LINE" ;;
esac
