"""Spoken reasons for a gate NO-GO (2026-11-04).

The operator in the flight volume cannot read the screen, and the voice said only "The gate says
no go", so every block sent them to the laptop. On a NO-GO (pre-launch or T-0) the runner now
speaks at most three failed checks in short plain sentences:

1. the checklist entry's own ``"speak"`` text, when it has one (``{detail}`` is filled in);
2. else a sentence built from the check's FAIL line for the common checks (hosts, disk, the
   stop latch here, and the ones the backend and the robot adapters add), with its numbers
   read aloud;
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


BUILT_IN = {
    "host_reachable": lambda d: "A host is not reachable.",
    "disk_space": lambda d: "Disk almost full.",
    "stop_latch": lambda d: "The stop latch is set. Clear it first.",
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
    if fn is None:
        try:
            from . import adapters, backend
            fn = (backend.active().gate_sentences.get(check)
                  or adapters.merged_dict("GATE_SENTENCES").get(check))
        except Exception:  # noqa: BLE001 - a sentence must never break a NO-GO
            fn = None
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
