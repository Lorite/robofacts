#!/usr/bin/env python3
"""Read-only questions to the drone for the gate, and the reset before a trial. Never arms.

    cf_link.py battery [--config drone.yaml]     # PASS or FAIL: the battery over the radio
    cf_link.py flow-deck [--config drone.yaml]   # PASS or FAIL: a Flow deck that sees the floor
    cf_link.py power-cycle [--config drone.yaml] # the reset step of robots.yaml

One line, starting with PASS or FAIL, and exit 0 or 1. Each opens its own link, so it runs only
while no other program holds the Crazyradio.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cflib_drone import CflibDrone, LinkError, load_config, power_cycle  # noqa: E402


def battery(drone, cfg) -> tuple:
    vbat = drone.battery_v()
    if vbat is None:
        return 1, f"FAIL: no battery sample from {drone.uri} within 3 s"
    if vbat < cfg["min_vbat"]:
        return 1, f"FAIL: battery {vbat:.2f} V is below {cfg['min_vbat']} V (read over the radio)"
    return 0, f"PASS: battery {vbat:.2f} V (>= {cfg['min_vbat']} V) over the radio"


def flow_deck(drone, cfg) -> tuple:
    """The deck is detected, and its sensor sees texture: median motion.squal over 2 s. A stuck
    sensor passes the boot self-test (the lab, 2026-11-04), so the deck flag alone is not enough."""
    if not drone.has_flow_deck():
        return 1, "FAIL: no Flow deck detected (deck.bcFlow2 is not 1)"
    squal = []
    drone.start_log(lambda sample: squal.append(sample["squal"]))
    time.sleep(2.0)
    if not squal:
        return 1, "FAIL: the Flow deck sent no sample within 2 s"
    median = statistics.median(squal)
    if median < cfg["min_squal"]:
        return 1, f"FAIL: Flow deck quality {median:.0f} below {cfg['min_squal']} (floor texture, light, lens?)"
    return 0, f"PASS: Flow deck detected, quality {median:.0f} (>= {cfg['min_squal']}) over 2 s"


def main(argv=None, drone_factory=CflibDrone) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("question", choices=["battery", "flow-deck", "power-cycle"])
    ap.add_argument("--config", type=Path, default=None)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    try:
        if args.question == "power-cycle":
            power_cycle(cfg["uri"])
            rc, line = 0, f"PASS: {cfg['uri']} power-cycled"
        else:
            with drone_factory(cfg["uri"]) as drone:
                rc, line = (battery if args.question == "battery" else flow_deck)(drone, cfg)
    except LinkError as exc:
        rc, line = 1, f"FAIL: {exc}"
    except ImportError:
        rc, line = 1, "FAIL: cflib is not installed in this shell (pip install cflib)"
    except Exception as exc:  # noqa: BLE001 - every failure is a FAIL with its reason
        rc, line = 1, f"FAIL: {type(exc).__name__}: {str(exc)[:150]}"
    print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
