#!/usr/bin/env bash
# The Crazyflie's battery voltage read over the radio with cflib, BEFORE the stack starts (a
# pre-launch check, "stage": "pre_launch"). Born 2026-11-04: trial 20261104T132935 launched the
# whole stack and failed the T-0 gate after 32 s on "battery 3.758 V below 3.85", after a 60 s
# hover on the same battery. The T-0 `battery` check reads the stack's ROS topic; this one opens
# its own link, so it runs only while no crazyflie_server holds the Crazyradio.
#
#   crazyflie_radio_battery.sh uri=radio://0/80/2M/E7E7E7E7E7 min_v=3.85 [samples=5] [timeout_s=20]
#
# Read-only: one log block of pm.vbat at 10 Hz, the median of `samples` values. No setpoint, never
# arms. A radio that cannot be opened, a link that does not come up, or cflib missing is a FAIL.
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../../../preflight/lib/common.sh}"
PF_HELP="crazyflie_radio_battery.sh uri=<radio uri> min_v=<V> [samples=5] [timeout_s=20]"
pf_parse_args "$@"
pf_require_args uri min_v
URI="$(pf_arg uri)"
MIN_V="$(pf_arg min_v)"
SAMPLES="$(pf_arg samples 5)"
TIMEOUT="$(pf_arg timeout_s 20)"

RESULT="$(timeout "$TIMEOUT" python3 - "$URI" "$SAMPLES" 2>&1 <<'PYEOF' | tail -n 1
import statistics
import sys
import time

try:
    import cflib.crtp
    from cflib.crazyflie import Crazyflie
    from cflib.crazyflie.log import LogConfig
    from cflib.crazyflie.syncCrazyflie import SyncCrazyflie
except ImportError:
    print("ERR|cflib is not installed in this shell")
    sys.exit(0)

uri, n = sys.argv[1], int(sys.argv[2])
cflib.crtp.init_drivers()
values = []
try:
    with SyncCrazyflie(uri, cf=Crazyflie(rw_cache="/tmp/cf_toc_cache")) as scf:
        conf = LogConfig(name="pf_vbat", period_in_ms=100)
        conf.add_variable("pm.vbat", "float")
        conf.data_received_cb.add_callback(lambda ts, data, c: values.append(data["pm.vbat"]))
        scf.cf.log.add_config(conf)
        conf.start()
        t_end = time.time() + 3.0
        while len(values) < n and time.time() < t_end:
            time.sleep(0.05)
        conf.stop()
except Exception as exc:  # noqa: BLE001 - every failure is a FAIL with its reason
    print(f"ERR|cannot read the battery over {uri}: {str(exc)[:120]} (is the radio held by a running server?)")
    sys.exit(0)
if not values:
    print(f"ERR|no pm.vbat sample from {uri} within 3 s")
    sys.exit(0)
print(f"V|{statistics.median(values):.3f}")
PYEOF
)"

case "$RESULT" in
    V\|*)
        V="${RESULT#V|}"
        pf_ge "$V" "$MIN_V" && pf_pass "battery ${V} V (>= ${MIN_V}) over the radio, before the stack"
        pf_fail "battery ${V} V is below the ${MIN_V} V launch threshold (read over the radio before the stack)"
        ;;
    ERR\|*) pf_fail "battery voltage UNREADABLE: ${RESULT#ERR|}" ;;
    *) pf_fail "battery voltage UNREADABLE over $URI (no answer within ${TIMEOUT}s: drone off, out of range, or the radio is busy)" ;;
esac
