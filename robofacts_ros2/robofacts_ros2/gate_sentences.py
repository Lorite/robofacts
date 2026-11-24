"""The spoken sentences of the ROS 2 checks (see robofacts.gate_speech)."""
from __future__ import annotations

import re

from robofacts.gate_speech import _NUM, _num


def _topic_words(topic: str) -> str:
    """``/drone1/flow_log`` -> ``flow log``: the last name, spoken."""
    last = [p for p in topic.split("/") if p]
    return (last[-1] if last else topic).replace("_", " ")


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


def _rigidbody_continuity(detail: str) -> str:
    """Name the body: the check guards a robot's body in the walks and the drone's in the flights. On
    2026-11-09 'Mocap does not track the drone' sent the operator looking for a drone that the campaign
    did not use, while the motion-capture system was not solving the walking robot's rigid body."""
    m = re.search(r"rigid body '([^']+)'", detail)
    if m:
        return f"Mocap does not track the rigid body {m.group(1).replace('_', ' ')}."
    return "Mocap does not track the rigid body."


GATE_SENTENCES = {
    "topic_rate": _topic_rate,
    "topic_value": _topic_value,
    "mocap_stream": lambda d: "Mocap stream missing." if "not advertised" in d else "Mocap stream too slow.",
    "rigidbody_continuity": _rigidbody_continuity,
    "rigidbody_near": lambda d: "The drone is not on its takeoff point.",
    "node_present": lambda d: "A required node is missing.",
    "estop_reachable": lambda d: "The emergency stop is not reachable.",
    "camera_frames": lambda d: "Camera frames missing.",
    "ros_clean_slate": lambda d: "Old ROS processes are still running.",
}
