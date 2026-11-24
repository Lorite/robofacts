#!/usr/bin/env python3
"""The pictures of a trial card: what an experiment declares, and what is already made.

An experiment that wants its trial cards filled declares a camera once, in
``experiments/<exp>/config/trial_media.yaml`` (``camera:`` with ``topic``, ``label``,
``stills``, ``video``, ``after_trial``), and may put its result plots first
(``card: {first: plots}``). This module reads that declaration, lists the pictures that
exist in the cache ``runs/camera/<trial>/`` and finds a trial's result plots. It reads no
recording. Making the pictures from a recording is the backend's part (the feature
``trial_media`` of robofacts.backend.Backend), because only the backend can read its
recordings.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

#: Where an experiment declares its camera.
MEDIA_CONFIG = Path("config") / "trial_media.yaml"

#: Where the pictures live, inside the experiment, one folder per trial.
MEDIA_DIR = Path("runs") / "camera"

INFO_NAME = "camera.json"

VIDEO_NAME = "camera.mp4"

STILL_WIDTH = 480

MAX_STILLS = 24

IMAGE_TYPES = ("sensor_msgs/msg/Image", "sensor_msgs/msg/CompressedImage")

CAMERA_KEYS = ("topic", "label", "stills", "video", "after_trial")

class MediaConfigError(ValueError):
    """``config/trial_media.yaml`` exists but does not say what it must."""

@dataclass(frozen=True)
class CameraSpec:
    """The one camera an experiment shows on its trial cards.

    ``topic`` and ``label`` are the first alternative, ``alternatives`` all of them
    as (topic, label) in the declared order (just the first when there is one).
    """
    topic: str
    label: str = "camera"
    stills: int = 8
    video: bool = True
    after_trial: bool = True
    alternatives: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self):
        if not self.alternatives:
            object.__setattr__(self, "alternatives", ((self.topic, self.label),))

def load_camera(exp_dir: Path) -> Optional[CameraSpec]:
    """The experiment's declared camera, None without ``config/trial_media.yaml``."""
    path = Path(exp_dir) / MEDIA_CONFIG
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise MediaConfigError(f"{path}: does not load ({exc})") from exc
    camera = raw.get("camera") if isinstance(raw, dict) else None
    if not isinstance(camera, dict) or not camera.get("topic"):
        raise MediaConfigError(f"{path}: needs camera.topic, an absolute topic name")
    unknown = sorted(set(camera) - set(CAMERA_KEYS))
    if unknown:
        raise MediaConfigError(f"{path}: camera has unknown key(s) {', '.join(unknown)}; "
                               f"the known ones are {', '.join(CAMERA_KEYS)}")
    stills = camera.get("stills", 8)
    if not isinstance(stills, int) or isinstance(stills, bool) or not 0 <= stills <= MAX_STILLS:
        raise MediaConfigError(f"{path}: camera.stills must be a whole number from 0 to {MAX_STILLS}")
    label = str(camera.get("label") or "camera")
    alternatives = []
    for item in camera["topic"] if isinstance(camera["topic"], list) else [camera["topic"]]:
        if isinstance(item, dict):
            extra = sorted(set(item) - {"topic", "label"})
            if extra:
                raise MediaConfigError(f"{path}: a camera.topic alternative has unknown key(s) "
                                       f"{', '.join(extra)}; the known ones are topic, label")
            topic, alt_label = item.get("topic"), str(item.get("label") or label)
        else:
            topic, alt_label = item, label
        if not str(topic or "").startswith("/"):
            raise MediaConfigError(f"{path}: needs camera.topic, an absolute topic name "
                                   f"(or a list of them), got {topic!r}")
        alternatives.append((str(topic), alt_label))
    return CameraSpec(topic=alternatives[0][0], label=alternatives[0][1],
                      stills=stills, video=bool(camera.get("video", True)),
                      after_trial=bool(camera.get("after_trial", True)),
                      alternatives=tuple(alternatives))

CARD_FIRST = ("camera", "plots")

def load_card_first(exp_dir: Path) -> str:
    """What a trial card shows first: ``camera`` (the trial video, then the
    camera, then the plots) or ``plots``. A missing or broken file means camera."""
    path = Path(exp_dir) / MEDIA_CONFIG
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return "camera"
    card = raw.get("card") if isinstance(raw, dict) else None
    first = str((card or {}).get("first", "camera")) if isinstance(card, dict) else "camera"
    return first if first in CARD_FIRST else "camera"

def load_camera_or_none(exp_dir: Path) -> Optional[CameraSpec]:
    """:func:`load_camera` for readers that must not fail: a broken file means no camera."""
    try:
        return load_camera(exp_dir)
    except MediaConfigError:
        return None

# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #
def media_dir(exp_dir: Path, trial: str) -> Path:
    return Path(exp_dir) / MEDIA_DIR / trial

def cached(exp_dir: Path, trial: str) -> Tuple[List[Path], Optional[Path], Dict[str, Any]]:
    """(stills, camera video, what ``camera.json`` says) of one trial, from disk only."""
    folder = media_dir(exp_dir, trial)
    info_path = folder / INFO_NAME
    if not info_path.is_file():
        return [], None, {}
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None, {}
    stills = sorted(folder.glob("still_*.jpg"))
    video = folder / VIDEO_NAME
    return stills, (video if video.is_file() else None), info if isinstance(info, dict) else {}

def still_indices(count: int, n: int) -> List[int]:
    """``n`` evenly spaced frame indices of ``count``, the middle of each of ``n`` equal parts."""
    if count <= 0 or n <= 0:
        return []
    n = min(n, count)
    return sorted({min(count - 1, int((i + 0.5) * count / n)) for i in range(n)})

#: At most this many plots per trial: a card has a tag per plot, and a row of tags is short.
MAX_PLOTS = 4

def plots_of(analyses: Iterable[Dict[str, Any]], trial: str, limit: int = MAX_PLOTS
             ) -> List[Dict[str, Any]]:
    """The trial's result plots for its card: [{path, name, analysis, cohort}], own plots first.

    Newest analysis first. A per-trial folder gives its ``*.png``, and a cohort
    folder's plot named after the trial (``<trial>_trajectory.png``) is the trial's
    own too. Then the first plot of each cohort that includes the trial, with
    ``cohort`` set to its number of trials, so the card can say the plot is about
    many trials. ``name`` is the file's stem without the trial's name
    (``trajectory``). An older run of the same analysis that made a plot of the
    same name is left out: the newest one supersedes it.
    """
    analyses = list(analyses)
    own: List[Dict[str, Any]] = []
    cohort: List[Dict[str, Any]] = []
    seen = set()

    def add(into, png, a, n):
        name = png.stem[len(trial):].lstrip("_-") if png.stem.startswith(trial) else png.stem
        key = (a.get("analysis"), name or png.stem)
        if png not in seen and key not in seen:
            seen.update((png, key))
            into.append({"path": png, "name": name or png.stem, "analysis": a.get("analysis"),
                         "cohort": n})
    for a in analyses:
        path = Path(a["path"])
        if not path.is_dir():
            continue
        if a.get("per") == "cohort":
            named = sorted(path.glob(f"{trial}*.png"))
            for png in named:
                add(own, png, a, None)
            rest = [p for p in sorted(path.glob("*.png")) if p not in named]
            if rest:
                add(cohort, rest[0], a, int(a.get("n_bags") or 0) or None)
        else:
            for png in sorted(path.glob("*.png")) or sorted(path.glob(f"*/{trial}*.png")):
                add(own, png, a, None)
    return (own + cohort)[:limit]
