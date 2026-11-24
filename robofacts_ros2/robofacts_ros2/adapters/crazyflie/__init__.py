"""The Crazyflie adapter: crazyswarm2 and the Crazyflie firmware.

Checks: ``battery`` (the voltage on ``/<cf>/status``), ``crazyflie_radio_battery`` (the
voltage over the Crazyradio with cflib, before the stack starts), ``drone_armable`` (the
supervisor state), ``ekf_convergence`` (the onboard estimate against motion capture).
Helpers: ``verify_flight_happened`` and ``verify_ext_pose_age`` (verify steps that read a
recorded bag), ``rosout_trim_utils`` (analysis windows from crazyflie_server's /rosout events).
"""
import re

from robofacts.gate_speech import _NUM, _num


def _battery(detail: str) -> str:
    m = re.search(_NUM + r"\s*V.*?" + _NUM + r"\s*V", detail)
    if m:
        return f"Battery {_num(m.group(1))} volts, below {_num(m.group(2))}. Swap the battery."
    if "UNREADABLE" in detail.upper() or "unreadable" in detail:
        return "Battery voltage unreadable. Check that the drone is on."
    return "Battery check failed. Swap the battery."


#: The default name of the drone the record profiles and the checks build topics from.
NAMES = {"cf_name": "cf1"}

#: Read-only probes (see run_preflight.PARALLEL_SAFE_CHECKS).
PARALLEL_SAFE_CHECKS = frozenset({"battery", "drone_armable", "ekf_convergence"})

#: Checks that must run alone, and why.
SEQUENTIAL_CHECKS = {
    "crazyflie_radio_battery": (
        "opens its own link on the Crazyradio (cflib, 2026-11-04), which allows one user at a "
        "time, so it must not race another radio check; it runs in the pre-launch stage, before "
        "crazyflie_server takes the radio"),
}

#: The spoken sentence of a failed check (see gate_speech.sentence).
GATE_SENTENCES = {
    "battery": _battery,
    "crazyflie_radio_battery": _battery,
    "drone_armable": lambda d: "The drone cannot arm.",
}
