#!/usr/bin/env python3
"""The action of the cflib example: one Crazyflie hovers on its Flow deck, with cflib and no ROS.

    cf_hover.py --log experiments/cflib_hover/runs/flight.jsonl --hover-s 10

In order:

1. REFUSES BEFORE ARMING (exit 2) on an operator stop latch younger than 120 s (exit 5), when
   the radio link does not come up, no Flow deck is detected, the battery is below
   ``min_vbat``, the estimator does not settle after its reset, no state sample has arrived,
   or the estimated ground position is more than ``ground_guard_m`` from the origin.
2. The lab's two-stage takeoff (2026-11-04, after Flow-only takeoffs that tilted and one that
   flew away): flow off on the floor (``motion.disable`` 1), ``stage1_m`` in ``stage1_s``,
   flow back on once the estimate passes ``flow_on_m`` (or at the end of the stage), then
   the climb to ``height_m``.
3. Holds for ``--hover-s`` and LANDS (``land_m`` in ``land_s``) on SIGINT or SIGTERM or a
   fresh stop latch (exit 5), when the estimate leaves the geofence (exit 6), when no state
   sample has arrived for ``stale_s`` (exit 7, the radio link is probably lost: the land
   command is still sent, and the operator is told to use the physical stop if the drone keeps
   hovering), at the end of the hover, and on any error (a ``finally``).
4. A latch whose last line starts with ``estop`` cuts the motors instead, at any time: in the
   air, after a signal, and during the landing.

The flight log (``--log``, JSON lines) holds the estimate, the range, the battery and the flow
quality at 20 Hz, and the events (takeoff, land with its reason). The runner records it.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cflib_drone import CflibDrone, JsonlLog, LinkError, load_config  # noqa: E402

from robofacts.stop_latch_clear import STOP_LATCH_MAX_AGE_S, fresh_latch, latch_path  # noqa: E402

OK_RC, ERROR_RC, REFUSED_RC, STOP_RC, GEOFENCE_RC, STALE_RC = 0, 1, 2, 5, 6, 7
POLL_S = 0.02


def say(text: str) -> None:
    """Print, but never fail: a Ctrl+C stops the runner too, which closes this pipe mid-landing."""
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass


def latch_since(path: Path, since: float) -> str:
    """The latch's last line when it was written after ``since`` (this run), else ''."""
    try:
        if path.stat().st_mtime < since - 1.0:
            return ""
        lines = path.read_text(errors="replace").strip().splitlines()
    except OSError:
        return ""
    return (lines[-1] if lines else "") or f"stop latch at {path}"


class Stop:
    """What asks the drone to come down: the latch or a signal. An estop line wins over both."""

    def __init__(self, latch: Path, since: float):
        self.latch, self.since, self.signal = latch, since, ""

    def on_signal(self, signum, _frame) -> None:
        self.signal = signal.Signals(signum).name

    def estop(self) -> str:
        line = latch_since(self.latch, self.since)
        return f"operator stop latch: {line}" if line.startswith("estop") else ""

    def reason(self) -> str:
        line = latch_since(self.latch, self.since)
        if line.startswith("estop"):
            return f"operator stop latch: {line}"
        if self.signal:
            return f"signal {self.signal}"
        return f"operator stop latch: {line}" if line else ""


def stale(drone, cfg: Dict) -> str:
    """'' while state samples arrive, else why the state cannot be trusted."""
    age = drone.age_s()
    if age is None:
        return "no state sample from the drone yet"
    if age > cfg["stale_s"]:
        return (f"no state sample for {age:.2f} s (limit {cfg['stale_s']} s): the radio link is "
                f"probably lost")
    return ""


def preflight(drone, cfg: Dict) -> str:
    """'' when the drone may arm, else why not."""
    if not drone.has_flow_deck():
        return "no Flow deck detected (deck.bcFlow2 is not 1)"
    vbat = drone.battery_v()
    if vbat is None:
        return "battery voltage unreadable"
    if vbat < cfg["min_vbat"]:
        return f"battery {vbat:.2f} V below {cfg['min_vbat']} V"
    if not drone.reset_estimator():
        return "the estimator did not settle within 10 s after its reset"
    time.sleep(0.3)   # one or two log samples after the reset
    here, old = drone.latest(), stale(drone, cfg)
    if old or not here:
        return f"the state is not streaming ({old or 'empty sample'})"
    off = max(abs(here["x"]), abs(here["y"]))
    if off > cfg["ground_guard_m"]:
        return f"estimated ground position {off:.3f} m from the origin (> {cfg['ground_guard_m']} m)"
    return ""


def outside_fence(state: Dict, cfg: Dict) -> str:
    if not state:
        return "no state sample"
    xy = max(abs(state["x"]), abs(state["y"]))
    if xy > cfg["fence_xy_m"]:
        return f"estimate {xy:.2f} m from the takeoff point (fence {cfg['fence_xy_m']} m)"
    if state["z"] > cfg["fence_z_m"]:
        return f"estimate {state['z']:.2f} m high (fence {cfg['fence_z_m']} m)"
    return ""


def attempt(log: JsonlLog, what: str, call, *args) -> None:
    """Send one command on the way down. A dead link must not skip the commands after it."""
    try:
        call(*args)
    except Exception as exc:  # noqa: BLE001
        log.write("event", event="command_failed", command=what, error=f"{type(exc).__name__}: {exc}")
        say(f"{what} failed: {exc}")


def come_down(drone, cfg: Dict, stop: Stop, log: JsonlLog, reason: str) -> None:
    """Land, reading the latch during the whole landing, or cut the motors on an estop line."""
    def cut(why: str) -> None:
        log.write("event", event="emergency_stop", reason=why)
        say(f"EMERGENCY STOP: {why}")
        attempt(log, "emergency stop", drone.emergency_stop)

    why = stop.estop()
    if why:
        cut(why)
        return
    log.write("event", event="land", reason=reason)
    say(f"landing: {reason}")
    if reason.startswith("no state sample"):
        say("LINK LOST? The land command is sent, but the drone may not receive it. If it keeps "
            "hovering, use the physical stop.")
    attempt(log, "land", drone.land, cfg["land_m"], cfg["land_s"])
    end = time.time() + cfg["land_s"] + 1.0
    while time.time() < end:
        time.sleep(POLL_S)
        why = stop.estop()
        if why:
            cut(why)
            return
    attempt(log, "stop the commander", drone.stop_commander)


def fly(drone, cfg: Dict, hover_s: float, stop: Stop, log: JsonlLog) -> tuple:
    """Arm, take off, hold, come down. Returns (exit code, reason). Comes down on every path."""
    rc, reason, armed = OK_RC, "", False
    try:
        drone.set_param("motion.disable", 1)
        drone.arm(True)
        armed = True
        time.sleep(1.0)
        log.write("event", event="takeoff", height_m=cfg["height_m"])
        say(f"takeoff to {cfg['height_m']} m")
        drone.takeoff(cfg["stage1_m"], cfg["stage1_s"])
        t0, flow_on = time.time(), False
        while time.time() - t0 < cfg["stage1_s"]:
            time.sleep(POLL_S)
            if not flow_on and drone.latest().get("z", 0.0) >= cfg["flow_on_m"]:
                drone.set_param("motion.disable", 0)
                flow_on = True
            if stop.reason() or stale(drone, cfg):
                break
        if not flow_on:
            drone.set_param("motion.disable", 0)
        if not stop.reason() and not stale(drone, cfg):
            drone.climb(cfg["height_m"] - cfg["stage1_m"], cfg["stage2_s"])
        t_up = time.time()
        while not reason:
            reason = stop.reason()
            if reason:
                rc = STOP_RC   # a stopped hover is not a finished trial
                break
            old = stale(drone, cfg)
            if old:
                reason, rc = old, STALE_RC
                say(f"STALE STATE: {old}")
                break
            fence = outside_fence(drone.latest(), cfg)
            if fence and time.time() - t_up > 0.5:
                reason, rc = fence, GEOFENCE_RC
            elif time.time() - t_up >= cfg["stage2_s"] + hover_s:
                reason = f"hover of {hover_s:.0f} s done"
            else:
                time.sleep(POLL_S)
    except Exception as exc:  # noqa: BLE001 - any error lands the drone
        reason, rc = f"error: {type(exc).__name__}: {exc}", ERROR_RC
    finally:
        if armed:
            come_down(drone, cfg, stop, log, reason or "error")
            attempt(log, "disarm", drone.arm, False)
    return rc, reason


def main(argv=None, drone_factory=CflibDrone) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", type=Path, required=True, help="the flight log, JSON lines")
    ap.add_argument("--hover-s", type=float, default=10.0)
    ap.add_argument("--config", type=Path, default=None, help="default: config/drone.yaml")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    since = time.time()
    latch = latch_path()
    stop = Stop(latch, since)
    signal.signal(signal.SIGINT, stop.on_signal)
    signal.signal(signal.SIGTERM, stop.on_signal)
    log = JsonlLog(args.log)
    log.write("event", event="start", uri=cfg["uri"], hover_s=args.hover_s)

    def refuse(why: str, rc: int = REFUSED_RC) -> int:
        log.write("event", event="refused", reason=why)
        say(f"did NOT take off: {why}")
        log.close()
        return rc

    why = fresh_latch(latch, STOP_LATCH_MAX_AGE_S, since)
    if why:
        return refuse(f"operator stop latch is fresh ({why}), rm {latch} when safe", STOP_RC)
    if not 0.2 <= cfg["height_m"] <= 1.0 or not 0 < args.hover_s <= cfg["max_hover_s"]:
        return refuse(f"height in [0.2, 1.0] m and hover in (0, {cfg['max_hover_s']}] s")
    try:
        with drone_factory(cfg["uri"]) as drone:
            drone.start_log(lambda sample: log.write("state", **sample))
            problem = preflight(drone, cfg)
            if not problem and stop.reason():
                problem = f"stopped before takeoff ({stop.reason()})"
            if problem:
                return refuse(problem, STOP_RC if "latch" in problem else REFUSED_RC)
            rc, reason = fly(drone, cfg, args.hover_s, stop, log)
    except LinkError as exc:
        return refuse(str(exc))
    log.write("event", event="end", rc=rc, reason=reason)
    say(f"done: {reason} (exit {rc})")
    log.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
