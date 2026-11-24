"""The built-in backend without any middleware: a shell command starts the stack, and a
small recorder stores what the system writes to its log files during the trial.

It exists for two reasons. A system that is a set of processes and log files can use it as
it is. And it is the second implementation of the two interfaces of
:mod:`robofacts.backend`, so the trial loop is exercised without ROS.

``runner.yaml`` of an experiment on this backend::

    launch:
      command: "python3 experiments/plant/scripts/plant.py --rate {rate}"   # optional
    record_sources: [experiments/plant/runs/plant.log]    # files the recorder follows

The launch command is templated like the action: ``{factor}`` and ``{arg_<name>}``. An
experiment without ``launch:`` has no stack, and the action is the whole trial.

A recording is a directory with ``data/<file name>`` for every source (the bytes written
while the recorder ran) and ``recording.json``, written last, which marks it finalized.
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..backend import Backend, BackendError

#: Written last by the recorder: its presence means the recording is finalized.
MANIFEST = "recording.json"
DATA_DIR = "data"


class ProcessLauncher:
    def parse(self, raw: Dict[str, Any], path: Path) -> Dict[str, Any]:
        launch = raw.get("launch")
        if launch is None:
            return {}
        if not isinstance(launch, dict):
            raise BackendError(f"{path}: launch must be a mapping with a command")
        if launch.get("package") or launch.get("file"):
            raise BackendError(
                f"{path}: launch.package and launch.file belong to the ROS 2 backend, and this "
                f"project runs on the process backend. Add `backend: ros2` to trials.yaml, or "
                f"declare `launch: {{command: ...}}`")
        command = str(launch.get("command") or "").strip()
        if not command:
            raise BackendError(f"{path}: launch.command is required (or remove launch: for an "
                               f"experiment without a stack)")
        return {"command": command}

    def command(self, stack: Dict[str, Any], args: Dict[str, Any],
                values: Optional[Dict[str, Any]] = None) -> Optional[List[str]]:
        template = (stack or {}).get("command")
        if not template:
            return None
        placeholders = {**(values or {}), **{f"arg_{k}": v for k, v in (args or {}).items()}}
        try:
            return shlex.split(str(template).format(**placeholders))
        except KeyError as exc:
            raise BackendError(f"launch command references unknown factor {exc}") from exc

    def stop_patterns(self) -> Dict[str, str]:
        # The runner stops the process group it started, which is the whole stack here.
        return {}


class ProcessRecorder:
    noun = "recording"

    def command(self, directory: Path, sources: List[str],
                options: Optional[Dict[str, Any]] = None) -> List[str]:
        return ["python3", "-m", "robofacts.backends.process_recorder",
                "--out", str(directory), *[str(s) for s in sources]]

    def holds_data(self, directory: Path) -> bool:
        data = Path(directory) / DATA_DIR
        return data.is_dir() and any(p.is_file() and p.stat().st_size > 0 for p in data.iterdir())

    def unsettled_reason(self, directory: Path) -> Optional[str]:
        if not (Path(directory) / MANIFEST).is_file():
            return f"no {MANIFEST} (recording never finalized)"
        return None

    def has_data(self, target: Any, directory: Path) -> Tuple[bool, str]:
        directory = Path(directory)
        if target is None:
            if not directory.is_dir() or self.unsettled_reason(directory):
                return False, f"no {MANIFEST}: the recorder never finalized a recording"
            if not self.holds_data(directory):
                return False, "the recording holds no data: no source was written during the trial"
            return True, ""
        from ..remote_exec_utils import remote_sh
        script = (f'd={shlex.quote(str(directory))}; '
                  f'if [ -f "$d/{MANIFEST}" ]; then echo meta=1; else echo meta=0; fi; '
                  f'echo data=$(find "$d/{DATA_DIR}" -type f -size +0 2>/dev/null | wc -l)')
        result = remote_sh(target, script)
        if result.returncode != 0:
            return False, f"could not inspect the recording on {target.host}: {result.stderr.strip()}"
        out = result.stdout
        if "meta=1" not in out:
            return False, f"no {MANIFEST} on {target.host}: the recorder never finalized a recording"
        if "data=0" in out.replace(" ", ""):
            return False, f"the recording on {target.host} holds no data"
        return True, ""

    def after_trial(self, directory: Path, target: Any, hooks: Any, project_root: Path, *,
                    reduced: bool = True) -> None:
        return None


BACKEND = Backend(name="process", launcher=ProcessLauncher(), recorder=ProcessRecorder())
