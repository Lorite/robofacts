#!/usr/bin/env python3
"""Pictures of a trial from its own bag: evenly spaced stills and a camera video (#138).

Most trials have no spectator video (``trial_video.mp4`` exists only where an
experiment rendered one), but many record an onboard camera. An experiment that
wants its trial cards filled declares that camera once, in
``experiments/<exp>/config/trial_media.yaml``::

    camera:
      topic: /crazyflie/aideck/image   # sensor_msgs/Image or sensor_msgs/CompressedImage
      label: drone camera              # what the UI calls it, so nobody mistakes it for the trial video
      stills: 8                        # evenly spaced frames per trial
      video: true                      # also an H.264 mp4 of the whole topic
      after_trial: true                # the runner makes them right after the slim copy

No topic lives in the code: an experiment without the file has no stills, and the
UI falls back to the spectator video's poster or to a plot of the trial.

The pictures are a cache, never data: ``runs/camera/<trial>/`` (``runs/`` is
git-ignored and already served read-only by the UI) holds ``still_01.jpg`` and
up, ``camera.mp4`` and ``camera.json``, which records what was read (the bag, the
topic, the frame count, the rate). The slim copy is read when it carries the
topic (it does in every experiment so far, and it is six times smaller), else the
full bag. ``trials records <exp> --stills`` makes them ahead of the UI.

Reading a bag needs ROS (``rosbag2_py``) and ``cv2``; both are imported only
when pictures are made, so listing what exists works without them.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
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
    """The one camera topic an experiment shows on its trial cards."""
    topic: str
    label: str = "camera"
    stills: int = 8
    video: bool = True
    after_trial: bool = True


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
    if not isinstance(camera, dict) or not str(camera.get("topic") or "").startswith("/"):
        raise MediaConfigError(f"{path}: needs camera.topic, an absolute topic name")
    unknown = sorted(set(camera) - set(CAMERA_KEYS))
    if unknown:
        raise MediaConfigError(f"{path}: camera has unknown key(s) {', '.join(unknown)}; "
                               f"the known ones are {', '.join(CAMERA_KEYS)}")
    stills = camera.get("stills", 8)
    if not isinstance(stills, int) or isinstance(stills, bool) or not 0 <= stills <= MAX_STILLS:
        raise MediaConfigError(f"{path}: camera.stills must be a whole number from 0 to {MAX_STILLS}")
    return CameraSpec(topic=str(camera["topic"]), label=str(camera.get("label") or "camera"),
                      stills=stills, video=bool(camera.get("video", True)),
                      after_trial=bool(camera.get("after_trial", True)))


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


def topic_info(bag_dir: Optional[Path], topic: str) -> Optional[Dict[str, Any]]:
    """{type, count, duration_s} of ``topic`` in a bag, from its ``metadata.yaml``; None if absent."""
    if bag_dir is None:
        return None
    meta = Path(bag_dir) / "metadata.yaml"
    try:
        info = (yaml.safe_load(meta.read_text(encoding="utf-8")) or {}).get(
            "rosbag2_bagfile_information") or {}
    except (OSError, yaml.YAMLError, AttributeError):
        return None
    duration = ((info.get("duration") or {}).get("nanoseconds") or 0) / 1e9
    for entry in info.get("topics_with_message_count") or []:
        meta_t = (entry or {}).get("topic_metadata") or {}
        if meta_t.get("name") == topic and meta_t.get("type") in IMAGE_TYPES:
            count = int(entry.get("message_count") or 0)
            if count > 0:
                return {"type": meta_t["type"], "count": count, "duration_s": duration}
    return None


def source_bag(candidates: Iterable[Optional[Path]], topic: str
               ) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
    """The first bag (slim copy first, by the caller's order) that records ``topic``."""
    for bag in candidates:
        info = topic_info(bag, topic)
        if info is not None:
            return Path(bag), info
    return None, None


def is_current(exp_dir: Path, trial: str, spec: CameraSpec, source: Path) -> bool:
    """Whether the cache was made from this source with this spec and is not older than the bag."""
    _stills, video, info = cached(exp_dir, trial)
    if not info or info.get("topic") != spec.topic or info.get("stills_wanted") != spec.stills:
        return False
    if spec.video and video is None and not info.get("video_error"):
        return False
    made = (media_dir(exp_dir, trial) / INFO_NAME).stat().st_mtime
    newest = max((p.stat().st_mtime for p in Path(source).glob("*.mcap")), default=0.0)
    return made >= newest


def still_indices(count: int, n: int) -> List[int]:
    """``n`` evenly spaced frame indices of ``count``, the middle of each of ``n`` equal parts."""
    if count <= 0 or n <= 0:
        return []
    n = min(n, count)
    return sorted({min(count - 1, int((i + 0.5) * count / n)) for i in range(n)})


# --------------------------------------------------------------------------- #
# Decoding and making
# --------------------------------------------------------------------------- #
#: sensor_msgs/Image encodings and how many channels and bytes per channel they have.
_ENCODINGS = {"mono8": (1, 1), "8uc1": (1, 1), "rgb8": (3, 1), "bgr8": (3, 1),
              "rgba8": (4, 1), "bgra8": (4, 1), "mono16": (1, 2), "16uc1": (1, 2),
              "bayer_rggb8": (1, 1), "bayer_bggr8": (1, 1), "bayer_gbrg8": (1, 1),
              "bayer_grbg8": (1, 1)}


def to_bgr(msg: Any, msg_type: str):
    """A message as an 8-bit BGR array (``cv2``'s order), or None for an encoding it cannot read."""
    import cv2
    import numpy as np
    if msg_type.endswith("CompressedImage"):
        frame = cv2.imdecode(np.frombuffer(bytes(msg.data), np.uint8), cv2.IMREAD_COLOR)
        return frame
    enc = str(msg.encoding).lower()
    if enc not in _ENCODINGS:
        return None
    channels, depth = _ENCODINGS[enc]
    h, w, step = int(msg.height), int(msg.width), int(msg.step)
    if h <= 0 or w <= 0:
        return None
    buf = np.frombuffer(bytes(msg.data), np.uint8)
    if buf.size < h * step:
        return None
    rows = buf[:h * step].reshape(h, step)[:, :w * channels * depth]
    if depth == 2:
        img = rows.copy().view("<u2" if not msg.is_bigendian else ">u2").reshape(h, w)
        img = (img / max(1, int(img.max())) * 255).astype(np.uint8)
    else:
        img = rows.reshape(h, w, channels) if channels > 1 else rows.reshape(h, w)
    if enc.startswith("bayer_"):
        code = {"bayer_rggb8": cv2.COLOR_BayerRG2BGR, "bayer_bggr8": cv2.COLOR_BayerBG2BGR,
                "bayer_gbrg8": cv2.COLOR_BayerGB2BGR, "bayer_grbg8": cv2.COLOR_BayerGR2BGR}[enc]
        return cv2.cvtColor(np.ascontiguousarray(img), code)
    if channels == 1:
        return cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_GRAY2BGR)
    if enc == "rgb8":
        return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    if enc == "rgba8":
        return cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    if enc == "bgra8":
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return np.ascontiguousarray(img)


def _read_frames(bag: Path, topic: str):
    """Yield the deserialized messages of ``topic``, through the package's bag reader."""
    from .bag_io_utils import read_messages_with_time
    for _topic, msg, _t in read_messages_with_time(bag, topics=[topic]):
        yield msg


class _Encoder:
    """ffmpeg fed raw BGR frames on stdin, writing a browser-playable H.264 mp4."""

    def __init__(self, out: Path, width: int, height: int, fps: float, ffmpeg: str):
        self.size = (width, height)
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}",
               "-r", f"{fps:g}", "-i", "-",
               # yuv420p needs even sides; faststart puts the index first for streaming.
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264",
               "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(out)]
        # stderr goes to a file: a pipe nobody reads can fill and stall the encoder.
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=self.log)

    def write(self, frame) -> bool:
        if (frame.shape[1], frame.shape[0]) != self.size:
            return False                 # a frame of another size: leave it out
        try:
            self.proc.stdin.write(frame.tobytes())
            return True
        except (BrokenPipeError, OSError):
            return False

    def close(self, timeout: float) -> Optional[str]:
        """None when the mp4 is good, else what ffmpeg said."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return "ffmpeg timed out"
        finally:
            self.log.seek(0)
            err = self.log.read()
            self.log.close()
        if self.proc.returncode != 0:
            return err.decode("utf-8", "replace").strip()[-300:] or "ffmpeg failed"
        return None


def make(exp_dir: Path, trial: str, candidates: Iterable[Optional[Path]], spec: CameraSpec, *,
         force: bool = False, ffmpeg: str = "ffmpeg", timeout: float = 600.0
         ) -> Optional[Dict[str, Any]]:
    """Make the stills and the camera video of one trial; returns ``camera.json``'s content.

    None when no candidate bag records the topic. A cache made from the same bag with
    the same spec is kept unless ``force``. Everything is written to a temporary
    folder that replaces the trial's folder at the end, so the UI never shows half
    a set. A frame that cannot be decoded is skipped, an encoding it cannot read at
    all gives zero stills, and a failed video is recorded in ``video_error``.
    """
    import cv2
    exp_dir = Path(exp_dir)
    source, info = source_bag(candidates, spec.topic)
    if source is None:
        return None
    if not force and is_current(exp_dir, trial, spec, source):
        return cached(exp_dir, trial)[2]
    folder = media_dir(exp_dir, trial)
    folder.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{trial}.", dir=folder.parent))
    t0 = time.monotonic()
    count, duration = info["count"], info["duration_s"]
    fps = round(count / duration, 1) if duration > 0 else 10.0
    fps = min(60.0, max(1.0, fps))
    wanted = set(still_indices(count, spec.stills))
    encoder: Optional[_Encoder] = None
    want_video = spec.video and shutil.which(ffmpeg) is not None
    video_error = None if want_video or not spec.video else "no ffmpeg on this machine"
    stills: List[str] = []
    frames = decoded = 0
    size = None
    try:
        for index, msg in enumerate(_read_frames(source, spec.topic)):
            frames += 1
            if index not in wanted and not want_video:
                continue
            frame = to_bgr(msg, info["type"])
            if frame is None:
                continue
            decoded += 1
            size = size or (frame.shape[1], frame.shape[0])
            if want_video:
                if encoder is None:
                    encoder = _Encoder(tmp / VIDEO_NAME, size[0], size[1], fps, ffmpeg)
                encoder.write(frame)
            if index in wanted:
                still = frame
                if still.shape[1] > STILL_WIDTH:
                    h = int(round(still.shape[0] * STILL_WIDTH / still.shape[1]))
                    still = cv2.resize(still, (STILL_WIDTH, h), interpolation=cv2.INTER_AREA)
                name = f"still_{len(stills) + 1:02d}.jpg"
                if cv2.imwrite(str(tmp / name), still, [cv2.IMWRITE_JPEG_QUALITY, 80]):
                    stills.append(name)
        if encoder is not None:
            video_error = encoder.close(timeout)
            encoder = None
            if video_error:
                (tmp / VIDEO_NAME).unlink(missing_ok=True)
        elif want_video:
            video_error = "no frame could be decoded"
        result = {
            "trial": trial, "topic": spec.topic, "label": spec.label, "type": info["type"],
            "source": str(source.relative_to(exp_dir)) if source.is_relative_to(exp_dir)
            else str(source),
            "frames": frames, "decoded": decoded, "fps": fps,
            "width": size[0] if size else None, "height": size[1] if size else None,
            "stills_wanted": spec.stills, "stills": stills,
            "video": VIDEO_NAME if (tmp / VIDEO_NAME).is_file() else None,
            "video_error": video_error,
            "made_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.monotonic() - t0, 1),
        }
        (tmp / INFO_NAME).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        tmp.chmod(0o755)                 # mkdtemp makes it 0700
        if folder.exists():
            shutil.rmtree(folder)
        os.replace(tmp, folder)
        return result
    finally:
        if encoder is not None:
            encoder.proc.kill()
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def make_after_trial(slim_dir: Optional[Path], bag_dir: Optional[Path] = None) -> None:
    """The runner's hook after the slim copy: make the pictures if the experiment declares a camera.

    The experiment is the slim copy's grandparent (``<exp>/bags_slim/<trial>_slim``).
    Never raises: the trial has flown, and pictures are a convenience.
    """
    try:
        if slim_dir is None:
            return
        slim_dir = Path(slim_dir)
        exp_dir = slim_dir.parent.parent
        spec = load_camera(exp_dir)
        if spec is None or not spec.after_trial:
            return
        trial = slim_dir.name[:-len("_slim")] if slim_dir.name.endswith("_slim") else slim_dir.name
        result = make(exp_dir, trial, [slim_dir, bag_dir], spec)
        if result is None:
            print(f"[media] {spec.topic} is not in the bag: no stills")
        else:
            print(f"[media] {len(result['stills'])} still(s)"
                  f"{' and a ' + spec.label + ' video' if result.get('video') else ''}"
                  f" in {result['seconds']} s under {MEDIA_DIR / trial}")
    except Exception as exc:  # noqa: BLE001 - never lose a finished trial
        print(f"[media] skipped ({exc})")


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
