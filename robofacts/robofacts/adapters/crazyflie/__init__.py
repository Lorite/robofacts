"""The Crazyflie adapter: crazyswarm2 and the Crazyflie firmware.

Checks: ``battery`` (the voltage on ``/<cf>/status``), ``crazyflie_radio_battery`` (the
voltage over the Crazyradio with cflib, before the stack starts), ``drone_armable`` (the
supervisor state), ``ekf_convergence`` (the onboard estimate against motion capture).
Helpers: ``verify_flight_happened`` and ``verify_ext_pose_age`` (verify steps that read a
recorded bag), ``rosout_trim_utils`` (analysis windows from crazyflie_server's /rosout events).
"""
from ...gate_speech import _battery

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
