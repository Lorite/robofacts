"""The ROS 2 recorder: ``ros2 bag record`` into a rosbag2 directory, a bag."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from robofacts.remote_exec_utils import package_module_command, remote_sh

#: The data files of a bag, by storage plugin (mcap, sqlite3).
BAG_DATA_SUFFIXES = ('.mcap', '.db3')
#: rosbag2 writes this file last, so its presence means the bag is finalized.
BAG_METADATA = "metadata.yaml"


def bag_has_data(target, bag_dir: Path) -> Tuple[bool, str]:
    """Did the recorder actually leave data in `bag_dir`?

    `ros2 bag record` can fail *after* the runner has started it and still let
    the run look orderly: an unwritable mount, a bad path, a recorder that
    never subscribed. The process exits, the stack comes down, and the trial
    reports success over an empty directory. Asking the host that did the
    recording is the only reliable way to tell a finished trial from a hole.

    Returns (ok, reason-if-not).
    """
    if target is None:
        if not (bag_dir / 'metadata.yaml').is_file():
            return False, 'no metadata.yaml — the recorder never finalized a bag'
        if not any(p.suffix in BAG_DATA_SUFFIXES for p in bag_dir.iterdir()):
            return False, 'bag directory holds no .mcap/.db3 data file'
        return True, ''

    script = (f'd={shlex.quote(str(bag_dir))}; '
              'if [ -f "$d/metadata.yaml" ]; then echo meta=1; else echo meta=0; fi; '
              'echo data=$(ls "$d" 2>/dev/null | grep -cE "[.](mcap|db3)$")')
    result = remote_sh(target, script)
    if result.returncode != 0:
        return False, f'could not inspect the bag on {target.host}: {result.stderr.strip()}'
    out = result.stdout
    if 'meta=1' not in out:
        return False, (f'no metadata.yaml on {target.host} — '
                       'the recorder never finalized a bag')
    if 'data=0' in out:
        return False, f'bag directory on {target.host} holds no .mcap/.db3 data file'
    return True, ''


def export_rosout(bag_dir: Path, target, hooks, project_root: Path) -> None:
    """Write ``<bag>/rosout.txt`` from the bag's /rosout. Never raises: the trial has flown."""
    try:
        if target is None:
            from robofacts_ros2.export_rosout_text import export_rosout_text
            count = export_rosout_text(Path(bag_dir), Path(bag_dir) / "rosout.txt")
            print(f"[runner] rosout.txt: {count} /rosout messages")
            return
        root = Path(project_root or target.workspace)
        completed = hooks.run_foreground(
            package_module_command(root, "export_rosout_text", [str(bag_dir)], package="robofacts_ros2"),
            target, cwd=root, check=False)
        rc = getattr(completed, "returncode", 0)
        print("[runner] rosout.txt written on the rig" if rc == 0
              else f"[runner] rosout export failed on the rig (rc={rc}) - continuing")
    except Exception as exc:  # noqa: BLE001 - a finished trial outranks a text export
        print(f"[runner] rosout export skipped ({exc})")


class Ros2Recorder:
    noun = "bag"
    #: What the data files of a bag are called in a message.
    data_name = ".mcap or .db3"
    #: The suffixes of those files (and of a file-compressed bag), which an operating web
    #: interface does not serve as files.
    data_suffixes = (".mcap", ".db3", ".zstd")
    #: What the recorder leaves beside the metadata of a finished trial.
    sidecar_note = " and `rosout.txt`"

    def command(self, directory: Path, sources: List[str],
                options: Optional[Dict[str, Any]] = None) -> List[str]:
        storage = str((options or {}).get("storage") or "mcap")
        return ["ros2", "bag", "record", "-s", storage, "-o", str(directory)] + list(sources)

    def has_data(self, target: Any, directory: Path) -> Tuple[bool, str]:
        return bag_has_data(target, Path(directory))

    def holds_data(self, directory: Path) -> bool:
        return any(p.suffix in BAG_DATA_SUFFIXES for p in Path(directory).iterdir())

    def unsettled_reason(self, directory: Path) -> Optional[str]:
        if not (Path(directory) / BAG_METADATA).is_file():
            return "no metadata.yaml (bag never finalized)"
        return None

    def reader_problem(self) -> Optional[str]:
        """Why bags cannot be read on this machine, or None (``trials reverify`` asks)."""
        try:
            import rosbag2_py  # noqa: F401
        except ImportError:
            return ("rosbag2_py does not import here: run trials reverify inside the "
                    "dev container of the host that recorded the trial")
        return None

    def after_trial(self, directory: Path, target: Any, hooks: Any, project_root: Path, *,
                    reduced: bool = True) -> None:
        """``rosout.txt`` beside the bag, on the host that holds it and before the slim
        copy, which carries it (2026-10-14). Then the slim copy and the trial's stills."""
        export_rosout(directory, target, hooks, project_root)
        # Leave a slim, transferable copy. Never fatal: the trial has flown.
        if reduced:
            try:
                from robofacts_ros2.slim_bag_archive import archive_and_sync
                local_slim = archive_and_sync(directory, target, project_root=project_root)
            except Exception as exc:  # noqa: BLE001
                local_slim = None
                print(f"[runner] slim archiving skipped ({exc})")
            # The trial card's stills and camera video, from the local slim copy,
            # when the experiment declares a camera (config/trial_media.yaml). Never fatal.
            from robofacts_ros2.trial_media import make_after_trial
            make_after_trial(local_slim, directory if target is None else None)
