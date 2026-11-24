"""The PX4 adapter: PX4 Autopilot through the uXRCE-DDS bridge.

Checks: ``px4_preflight_pass`` (PX4 reports that it would arm now, ``pre_flight_checks_pass``
on its vehicle status topic).
"""

NAMES: dict = {}
PARALLEL_SAFE_CHECKS = frozenset({"px4_preflight_pass"})
SEQUENTIAL_CHECKS: dict = {}
GATE_SENTENCES: dict = {}
