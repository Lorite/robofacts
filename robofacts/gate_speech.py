"""Spoken reasons for a gate NO-GO (2026-11-04).

The operator in the flight volume cannot read the screen, and the voice said only "The gate says
no go", so every block sent them to the laptop. On a NO-GO (pre-launch or T-0) the runner now
speaks at most three failed checks in short plain sentences:

1. the checklist entry's own ``"speak"`` text, when it has one (``{detail}`` is filled in);
2. else a sentence built from the check's FAIL line for the common checks (battery, topic rates,
   mocap, a sensor value, nodes, hosts), with its numbers read aloud;
3. else a generic "<check> failed."

The sentences are plain text for ``operator_voice.speak``, which respects ``TRIALS_VOICE=off``
(``--no-voice``). Pure functions, tested in ``test_gate_speech.py``.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

_NUM = r"(-?\d+(?:\.\d+)?)"


def _num(text: str) -> str:
    """A number as it should be spoken: at most two decimals, no trailing zeros."""
    try:
        value = float(text)
    except ValueError:
        return text
    out = f"{value:.2f}".rstrip("0").rstrip(".")
    return out if out not in ("-0", "") else "0"


def _topic_words(topic: str) -> str:
    """``/cf1/flow_log`` -> ``flow log``: the last name, spoken."""
    last = [p for p in topic.split("/") if p]
    return (last[-1] if last else topic).replace("_", " ")


def _battery(detail: str) -> str:
    m = re.search(_NUM + r"\s*V.*?" + _NUM + r"\s*V", detail)
    if m:
        return f"Battery {_num(m.group(1))} volts, below {_num(m.group(2))}. Swap the battery."
    if "UNREADABLE" in detail.upper() or "unreadable" in detail:
        return "Battery voltage unreadable. Check that the drone is on."
    return "Battery check failed. Swap the battery."


def _topic_rate(detail: str) -> str:
    found = re.search(r"(/[\w/]+)", detail)
    words = _topic_words(found.group(1)) if found else "A topic"
    if "not advertised" in detail:
        return f"{words.capitalize()} missing."
    m = re.search(r"~\s*" + _NUM + r"\s*Hz.*?need\s*>=\s*" + _NUM, detail)
    if m:
        return f"{words.capitalize()} at {_num(m.group(1))} hertz, needs {_num(m.group(2))}."
    m = re.search(r"~\s*" + _NUM + r"\s*Hz.*?max_hz=\s*" + _NUM, detail)
    if m:
        return f"{words.capitalize()} at {_num(m.group(1))} hertz, above {_num(m.group(2))}."
    return f"{words.capitalize()} rate too low."


def _topic_value(detail: str) -> str:
    topic = re.search(r"(/[\w/]+)", detail)
    words = _topic_words(topic.group(1)) if topic else "A sensor"
    if "constant" in detail:
        return f"{words.capitalize()} is frozen."
    m = re.search(r"median\s*" + _NUM + r"\s*<\s*min\s*" + _NUM, detail)
    if m:
        return f"{words.capitalize()} at {_num(m.group(1))}, needs {_num(m.group(2))}."
    return f"{words.capitalize()} out of range."


BUILT_IN = {
    "battery": _battery,
    "crazyflie_radio_battery": _battery,
    "topic_rate": _topic_rate,
    "topic_value": _topic_value,
    "mocap_stream": lambda d: "Mocap stream missing." if "not advertised" in d else "Mocap stream too slow.",
    "rigidbody_continuity": lambda d: "Mocap does not track the drone.",
    "rigidbody_near": lambda d: "The drone is not on its takeoff point.",
    "node_present": lambda d: "A required node is missing.",
    "host_reachable": lambda d: "A host is not reachable.",
    "estop_reachable": lambda d: "The emergency stop is not reachable.",
    "drone_armable": lambda d: "The drone cannot arm.",
    "camera_frames": lambda d: "Camera frames missing.",
    "disk_space": lambda d: "Disk almost full.",
    "stop_latch": lambda d: "The stop latch is set. Clear it first.",
    "ros_clean_slate": lambda d: "Old ROS processes are still running.",
}


def sentence(item: Dict[str, Any]) -> str:
    """One spoken sentence for one failed check."""
    detail = str(item.get("detail") or "")
    custom = str(item.get("speak") or "").strip()
    if custom:
        try:
            return custom.format(detail=detail)
        except (KeyError, IndexError, ValueError):
            return custom
    check = str(item.get("check") or "a check")
    fn = BUILT_IN.get(check)
    if fn is not None:
        try:
            return fn(detail)
        except Exception:  # noqa: BLE001 - a sentence must never break a NO-GO
            pass
    return f"{check.replace('_', ' ').capitalize()} failed."


def sentences(items: Sequence[Dict[str, Any]], limit: int = 3) -> List[str]:
    """At most ``limit`` sentences, in checklist order, without repeating the same sentence."""
    out: List[str] = []
    for item in items:
        text = sentence(item)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    if out and len(items) > limit:
        out[-1] = out[-1] + f" And {len(items) - limit} more."
    return out
