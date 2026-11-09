#!/usr/bin/env python3
"""Run the trials a campaign still needs (#96 Phase 4).

Composes the pieces that already exist, in the order the lab incidents taught:

    pre-flight gate (#87) -> launch (local or remote) -> record profile (#94)
      -> stop -> trial_metadata.json -> slim bag -> back into the index

Everything experiment-specific is declared, not coded: ``experiments/<name>/
config/runner.yaml`` says which launch file to bring up, how to execute one
trial, and how long to settle. The runner turns a Phase 3 plan into commands and
runs them, so the same code drives every experiment and — importantly — the
future web UI, which must be a client of this, never a second implementation.

Two properties that are not negotiable
--------------------------------------
* **The gate is not optional.** A trial that cannot pass pre-flight is not run,
  unless the operator ticked the override, in which case the reason and the
  failing checks land in the bag's metadata (#87).
* **Factor values are written into the trial metadata under their
  ``metadata_key``.** This is what closes the loop: without it, the bag this
  runner just produced would not be findable by the index or countable by the
  expander, and the campaign would ask for it again forever.

``--dry-run`` prints every command it would issue and touches nothing, which is
also how this is verified without hardware.

``--scratch`` flies ONE ad-hoc diagnostic flight through the campaign's own
apparatus (stack, T-0 gate, operator steps, recorder) but records it to
``bags_scratch/``, which the coverage index never reads. It exists because the
alternative is hand-built tmux panes, and 2026-08-12 showed what those cost:
a drone that silently never armed (no gate), a recorder pre-typed with an
already-existing bag name, and an operator juggling five panes mid-flight.

Every executed attempt, whatever its outcome, appends one JSON line to
``experiments/<name>/runs/attempts.jsonl`` (the attempt ledger, 2026-10-01):
a NO-GO leaves no bag and no metadata, so without the ledger a blocked or
failed attempt would exist only in a run log. The ledger is never read by the
coverage index or the planner.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import hashlib
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import yaml


from . import bag_storage as bstore
from . import campaign_plan as cp
from . import campaign_spec as cs
from . import operator_voice as voice
from . import trial_index as ti
from .preflight_gate import PreflightGate  # noqa: E402

from . import roots
from . import run_marker


def __getattr__(name):
    """``EXPERIMENTS_ROOT`` and ``REPO_ROOT`` for callers of the old constants, resolved on access."""
    return roots.module_root_attribute(name)
RUNNER_CONFIG_NAME = "runner.yaml"

# ANSI colour for the runner's own narrative (2026-08-11, the operator's ask).
# Enabled only when stdout is a real terminal, captured HERE because run_trial
# replaces sys.stdout with the run-log tee (whose isatty answer would be
# wrong). The tee strips the codes for the log sink, so run logs stay clean.
import re as _re
_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*m")
_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    """Wrap ``text`` in an ANSI colour when the terminal supports it.
    Codes: 32=green, 31=red, 33=yellow, 36=cyan; ';1' = bold."""
    return f"\x1b[{code}m{text}\x1b[0m" if _COLOR else text


class RunnerError(Exception):
    """The runner configuration is unusable."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class RunnerConfig:
    experiment: str
    launch_package: str
    launch_file: str
    #: "launch" -> `ros2 launch pkg file`, "run" -> `ros2 run pkg executable`.
    #: Demos and single-node stacks are `ros2 run`; real experiments are launch
    #: files. Making it explicit beats guessing from the file extension.
    launch_mode: str = "launch"
    #: Explicit topic list, when the experiment does not use a named record
    #: profile of the project's common/config/record_profiles.yaml.
    record_topics: List[str] = field(default_factory=list)
    action: str = ""
    #: Foreground commands run after the gate and before anything is launched:
    #: the "do this once, in order, or the trial is invalid" steps the run
    #: notebooks kept as their own cells (Spot arm-camera mode, spot setup/
    #: stand, EKF priming). Each is {name, command, optional required: false}.
    setup_steps: List[Dict[str, Any]] = field(default_factory=list)
    #: The robots this experiment uses (runner.yaml ``robots:``, 2026-11-04) and the reset steps
    #: their robots.yaml entries declare (robot_resets.py), run after ``setup_steps`` and the
    #: pre-launch checks, before the campaign's setup steps and the launch.
    robots: List[Dict[str, Any]] = field(default_factory=list)
    robot_resets: List[Dict[str, Any]] = field(default_factory=list)
    #: Extra long-running launches started alongside the main stack and torn
    #: down with it (e.g. Spot arm-tracking). Each is {name, command}.
    aux_launches: List[Dict[str, Any]] = field(default_factory=list)
    #: Post-trial checks against the RECORDED BAG ({bag} placeholder), run once
    #: the recorder has stopped. A non-zero exit marks the trial failed and
    #: aborted. This exists because an action can exit 0 without the trial
    #: having happened: on 2026-07-29 a latched firmware e-stop meant the drone
    #: never left the ground, the trajectory script returned 0, and the runner
    #: reported "flew and recorded". An exit code describes a process; only the
    #: bag describes the experiment.
    verify_steps: List[Dict[str, Any]] = field(default_factory=list)
    #: Provenance probed on the host that flew, AFTER the trial, into the
    #: metadata under ``key`` (first line of stdout). Each is {key, command}.
    #: Added 2026-10-01 for the PX4 SITL demo, whose stack version (the PX4
    #: checkout under lib/, the Gazebo release) is a fact about THAT
    #: experiment's apparatus, not about this repo's HEAD, which code_version
    #: already records. Never fatal, never a factor: a finished trial outranks
    #: a version string, and a firmware bump must not reopen every campaign cell.
    metadata_probes: List[Dict[str, Any]] = field(default_factory=list)
    settle_s: float = 3.0
    stop_grace_s: float = 2.0
    record_storage: str = "mcap"
    #: Optional live-view side launch (e.g. foxglove_bridge), off by default —
    #: started only when the trial's steps enable it.
    livestream: str = ""
    #: The drone the record profiles name their topics after. None means the
    #: project's (trials.yaml record_profiles.cf_name), else the package default.
    cf_name: Optional[str] = None
    hosts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: The working directory of every command this file declares. Resolved by
    #: roots.project_root() (flag, environment, trials.yaml, else the parent of
    #: the experiments root) unless runner.yaml says `project_root:`.
    project_root: Path = field(default_factory=lambda: roots.project_root())
    #: The experiments root this file was loaded from. The record profiles are read from it
    #: (``common/config/record_profiles.yaml``), so a runner given an explicit root records
    #: that project's topic sets, not the ones of the working directory's project.
    experiments_root: Optional[Path] = None
    source: Optional[Path] = None
    #: WHERE THE HEAVY BAG IS WRITTEN (2026-08-04). None means the repo's
    #: experiments/<name>/bags, which is what every trial recorded before that
    #: date, so leaving it unset changes nothing. "external" resolves through
    #: experiments/common/config/storage.yaml for the host that records, and an
    #: absolute path is used as a root directly. Declared per host under
    #: `hosts:` when it differs by machine, because the SSD is plugged into one
    #: of them and not the others.
    #: WHY IT IS A RUNNER SETTING AND NOT A FACTOR: a factor is an axis of the
    #: experiment and lands in the coverage grid. Which disk the bytes went to
    #: does not change what the trial measures, and an `internal` factor that no
    #: command consumes is refused by the config lint (L3) anyway. The value
    #: that WAS used is still recorded per trial, in `bag_storage` metadata.
    bags_root: Optional[str] = None
    #: --bags-root on the command line, which wins over both of the above.
    bags_root_override: Optional[str] = None
    #: True only for an experiment whose robots are all simulated. Required by
    #: a `gate` factor (2026-10-01): its shadow and disabled levels let a trial
    #: run past a NO-GO, which is a measurement in simulation and a hazard on
    #: hardware. Lint L12 enforces the pairing statically.
    simulation: bool = False
    #: Whether runner.yaml SAYS `simulation:` at all (2026-10-06). Every trial
    #: records its domain, so lint L14 requires the key: a missing one would
    #: make a simulated experiment's trials count as hardware.
    simulation_declared: bool = False
    #: Optional deadline on the ACTION, seconds (2026-10-02). None (the default)
    #: changes nothing. When set, an action still running after this long gets
    #: SIGTERM through the same path as the UI's stop (the flight actions treat
    #: a signal as "land every drone now"), and the trial is aborted with the
    #: timeout as its reason. Found by the lab-day sim test: an action blocked on
    #: a dead crazyswarm2 server kept the trial, and the recorder, open forever.
    action_timeout_s: Optional[float] = None
    #: The EXPLORE block (2026-10-16, explore.py): which parameters the explore
    #: bridge may change from Foxglove, and where it listens. Empty means the
    #: defaults, which explore.settings() decides from `simulation`: open in
    #: simulation, refused on hardware until this block is declared.
    explore: Dict[str, Any] = field(default_factory=dict)
    #: Whether runner.yaml SAYS `explore:` at all. On hardware the declaration
    #: is the opt-in, so an absent block and an empty one are not the same.
    explore_declared: bool = False

    def bags_root_for(self, host_level: Optional[Any]) -> Optional[str]:
        """The bags_root that applies to a trial running on ``host_level``."""
        if self.bags_root_override:
            return self.bags_root_override
        spec = self.hosts.get(str(host_level)) if host_level is not None else None
        if spec and spec.get("bags_root"):
            return str(spec["bags_root"])
        return self.bags_root

    def remote_target(self, host_level: Optional[Any]):
        """A RemoteTarget for this host level, or None to run locally.

        Built here rather than in runner.yaml so every experiment inherits the
        same container/setup conventions and only states what differs.

        ``local: true`` means "the runner is already inside the right dev
        container", which on the laptop is simply how the framework is used. A
        rig host's spec can additionally declare ``container_hostname`` (the
        hostname INSIDE its dev container): when the runner finds itself
        running there, the ssh+docker wrapper would be a loop back to the very
        place it already is, so it runs locally instead. Found live 2026-08-10:
        the on-rig shadow session ssh'd rig->rig through a VPN name
        whose daemon was down, and hung — from inside the container there is no
        ssh key anyway, and from the host the alias resolution is a network
        dependency a trial should not have.
        """
        spec = self.hosts.get(str(host_level)) if host_level is not None else None
        if not spec or spec.get("local", False):
            return None
        container_hostname = str(spec.get("container_hostname", "") or "")
        if container_hostname and socket.gethostname() == container_hostname:
            return None
        from .remote_exec_utils import RemoteTarget

        if not spec.get("host"):
            raise RunnerError(f"host spec {spec!r} has neither 'local: true' nor 'host'")
        workspace = Path(spec.get("workspace", self.project_root))
        return RemoteTarget(
            host=str(spec["host"]),
            container=str(spec.get("container", "")),
            workspace=workspace,
            ros_setup=str(spec.get("ros_setup", "/opt/ros/humble/setup.zsh")),
            ws_setup=str(spec.get("ws_setup", workspace / "ros2_ws/install/setup.zsh")),
            shell=str(spec.get("shell", "zsh")),
            env_setup=tuple(str(f) for f in spec.get("env_setup") or ()),
        )

    def local_setup(self, host_level: Optional[Any]) -> Optional[Tuple[str, List[str]]]:
        """(shell, files to source) for a trial that runs on THIS machine, or None.

        A runner inside the rig's container (``container_hostname``) or on a
        ``local: true`` host runs every command directly, so the host's setup
        files were never sourced: the runner's own environment applied, which
        held ROS and ros2_ws but not an overlay such as OpenVINS in ~/ov_ws
        (2026-10-14). The files a host spec NAMES (``ros_setup``, ``ws_setup``,
        ``env_setup``) are now sourced locally too. Defaults are not: the
        runner's environment already holds them, and a host spec that names no
        file keeps its commands exactly as before.
        """
        spec = self.hosts.get(str(host_level)) if host_level is not None else None
        if not spec or self.remote_target(host_level) is not None:
            return None
        files = [str(spec[k]) for k in ("ros_setup", "ws_setup") if spec.get(k)]
        files += [str(f) for f in spec.get("env_setup") or ()]
        if not files:
            return None
        return str(spec.get("shell", "zsh")), files

    @classmethod
    def load(cls, experiment: str, experiments_root: Optional[Path] = None) -> "RunnerConfig":
        experiments_root = roots.experiments_root(experiments_root)
        path = experiments_root / experiment / "config" / RUNNER_CONFIG_NAME
        if not path.is_file():
            raise RunnerError(
                f"{experiment} has no config/{RUNNER_CONFIG_NAME}; the runner needs to be told "
                f"which launch file to bring up and how to execute one trial")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        from . import robot_resets as rr
        try:
            robots_decl = rr.parse_declaration(raw.get("robots"), str(path))
            robot_reset_steps = rr.reset_steps(experiments_root, robots_decl, str(path))
        except rr.RobotResetError as exc:
            raise RunnerError(str(exc)) from exc
        launch = raw.get("launch") or {}
        for required in ("package", "file"):
            if not launch.get(required):
                raise RunnerError(f"{path}: launch.{required} is required")
        for level, spec in (raw.get("hosts") or {}).items():
            env_setup = (spec or {}).get("env_setup") if isinstance(spec, dict) else None
            if env_setup is not None and (not isinstance(env_setup, list)
                                          or not all(isinstance(f, str) and f for f in env_setup)):
                raise RunnerError(f"{path}: hosts.{level}.env_setup must be a list of file paths "
                                  f"to source, got {env_setup!r}")
        launch_mode = str(raw.get("launch_mode", "launch"))
        if launch_mode not in ("launch", "run"):
            raise RunnerError(f"{path}: launch_mode must be 'launch' or 'run', got {launch_mode!r}")
        return cls(
            experiment=experiment,
            launch_package=str(launch["package"]),
            launch_file=str(launch["file"]),
            launch_mode=launch_mode,
            record_topics=list(raw.get("record_topics") or []),
            action=str((raw.get("action") or {}).get("command", "")).strip(),
            setup_steps=_named_commands(raw.get("setup_steps"), "setup_steps", path),
            robots=robots_decl,
            robot_resets=robot_reset_steps,
            aux_launches=_named_commands(raw.get("aux_launches"), "aux_launches", path),
            verify_steps=_named_commands(raw.get("verify_steps"), "verify_steps", path),
            metadata_probes=_metadata_probes(raw.get("metadata_probes"), path),
            settle_s=float(raw.get("settle_s", 3.0)),
            stop_grace_s=float(raw.get("stop_grace_s", 2.0)),
            record_storage=str(raw.get("record_storage", "mcap")),
            livestream=str(((raw.get("livestream") or {}).get("command", "")
                            if isinstance(raw.get("livestream"), dict)
                            else raw.get("livestream") or "")).strip(),
            cf_name=(str(raw["cf_name"]) if raw.get("cf_name") else None),
            hosts=raw.get("hosts") or {},
            project_root=(Path(raw["project_root"]).expanduser() if raw.get("project_root")
                          else roots.project_root(experiments=experiments_root)),
            source=path,
            bags_root=(str(raw["bags_root"]) if raw.get("bags_root") else None),
            simulation=_strict_bool(raw.get("simulation", False), "simulation", path),
            simulation_declared="simulation" in raw,
            action_timeout_s=_positive_or_none(raw.get("action_timeout_s"), "action_timeout_s", path),
            explore=_explore_block(raw.get("explore"), path),
            explore_declared="explore" in raw,
            experiments_root=experiments_root,
        )


def _positive_or_none(value: Any, name: str, path: Path) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise RunnerError(f"{path}: {name} must be a positive number of seconds, got {value!r}")
    return float(value)


def _strict_bool(value: Any, name: str, path: Path) -> bool:
    """A safety-relevant flag must be a real YAML boolean, never a string."""
    if not isinstance(value, bool):
        raise RunnerError(f"{path}: {name} must be true or false, got {value!r}")
    return value


#: The keys an `explore:` block may carry, and what each must be.
EXPLORE_KEYS = {"param_whitelist": "a list of regular expressions",
                "topic_whitelist": "a list of regular expressions",
                "port": "a port number", "address": "an address to bind",
                "viewers": "a list of {name, command}",
                "actions": "a list of {name, command}",
                "jog": "a mapping with topic, max_speed_mps and optionally step_s, max_yaw_rate_rps, note"}


def _explore_block(raw: Any, path: Path) -> Dict[str, Any]:
    """runner.yaml's `explore:` block, checked for shape (explore.py gives it meaning).

    Refused here, at load time, rather than when the explore bridge starts: a
    misspelled key (`param_whitlist`) would otherwise fall back to the default
    silently, and on hardware the default is "no parameter may change".
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RunnerError(f"{path}: explore must be a mapping with "
                          f"{', '.join(EXPLORE_KEYS)}, got {raw!r}")
    unknown = sorted(set(raw) - set(EXPLORE_KEYS))
    if unknown:
        raise RunnerError(f"{path}: explore has unknown key(s) {', '.join(unknown)}; "
                          f"the known ones are {', '.join(EXPLORE_KEYS)}")
    out: Dict[str, Any] = {}
    for key in ("param_whitelist", "topic_whitelist"):
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
            raise RunnerError(f"{path}: explore.{key} must be {EXPLORE_KEYS[key]}, got {value!r}")
        for pattern in value:
            try:
                _re.compile(pattern)
            except _re.error as exc:
                raise RunnerError(f"{path}: explore.{key} entry {pattern!r} is not a "
                                  f"regular expression ({exc})") from None
        out[key] = list(value)
    if "port" in raw:
        port = raw["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise RunnerError(f"{path}: explore.port must be a port number, got {port!r}")
        out["port"] = port
    if "address" in raw:
        if not isinstance(raw["address"], str) or not raw["address"]:
            raise RunnerError(f"{path}: explore.address must be an address, got {raw['address']!r}")
        out["address"] = raw["address"]
    if "viewers" in raw:
        # Windows for a human (a simulator GUI, RViz), started by Explore only.
        out["viewers"] = _named_commands(raw["viewers"], "explore.viewers", path)
    if "actions" in raw:
        # Buttons on the Explore run page (#133): take off, fly a route, land.
        # Run by Explore only, one at a time, never by a trial.
        out["actions"] = _named_commands(raw["actions"], "explore.actions", path)
        names = [a["name"] for a in out["actions"]]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise RunnerError(f"{path}: explore.actions names {', '.join(twice)} more than once; "
                              f"the run page starts an action by its name")
    if "jog" in raw:
        out["jog"] = _explore_jog(raw["jog"], path)
    return out


#: The keys of explore.jog, and the hard ceilings the runner never exceeds.
JOG_KEYS = ("topic", "max_speed_mps", "step_s", "max_yaw_rate_rps", "note")
JOG_MAX_STEP_S = 5.0


def _explore_jog(raw: Any, path: Path) -> Dict[str, Any]:
    """explore.jog (#133): the velocity topic of the run page's jog pad and its limits.

    The limits are required, not defaulted: a jog pad publishes velocities, and
    how fast is a reviewed decision of the experiment, like a safety limit.
    """
    if not isinstance(raw, dict):
        raise RunnerError(f"{path}: explore.jog must be a mapping with {', '.join(JOG_KEYS)}")
    unknown = sorted(set(raw) - set(JOG_KEYS))
    if unknown:
        raise RunnerError(f"{path}: explore.jog has unknown key(s) {', '.join(unknown)}; "
                          f"the known ones are {', '.join(JOG_KEYS)}")
    topic = raw.get("topic")
    if not isinstance(topic, str) or not topic.startswith("/"):
        raise RunnerError(f"{path}: explore.jog.topic must be an absolute topic name, got {topic!r}")

    def positive(key, default=None, ceiling=None):
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 \
                or (value == 0 and key != "max_yaw_rate_rps"):
            raise RunnerError(f"{path}: explore.jog.{key} must be a positive number, got {value!r}")
        if ceiling is not None and value > ceiling:
            raise RunnerError(f"{path}: explore.jog.{key} may be at most {ceiling}, got {value!r}")
        return float(value)

    if "max_speed_mps" not in raw:
        raise RunnerError(f"{path}: explore.jog needs max_speed_mps (the fastest a press may move)")
    note = raw.get("note", "")
    if not isinstance(note, str):
        raise RunnerError(f"{path}: explore.jog.note must be text, got {note!r}")
    return {"topic": topic, "max_speed_mps": positive("max_speed_mps"),
            "step_s": positive("step_s", 1.0, JOG_MAX_STEP_S),
            "max_yaw_rate_rps": positive("max_yaw_rate_rps", 0.0),
            # What this robot does with a jog that the page cannot know (shown above the pad).
            "note": note.strip()}


def _named_commands(raw: Any, field_name: str, path: Path) -> List[Dict[str, Any]]:
    """Validate a list of {name, command} entries.

    Named, because a trial that fails half way through its setup must be able to
    say *which* step failed — "setup failed" over three Spot commands is not a
    diagnosis.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RunnerError(f"{path}: {field_name} must be a list of {{name, command}}")
    steps = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or not str(entry.get("command", "")).strip():
            raise RunnerError(f"{path}: {field_name}[{i}] needs a non-empty 'command'")
        when = str(entry.get("when") or "always")
        if when not in cs.STEP_WHEN:
            raise RunnerError(f"{path}: {field_name}[{i}] when must be one of "
                              f"{', '.join(cs.STEP_WHEN)}, got {entry.get('when')!r}")
        steps.append({"name": str(entry.get("name", f"{field_name}_{i}")),
                      "command": str(entry["command"]).strip(),
                      "required": bool(entry.get("required", True)),
                      "when": when})
    return steps


#: What a repeated trial shows when every operator step of the campaign is ``when: first``
#: (2026-11-04). The runner still waits once: the reset steps and the launch come next,
#: and the operator may still be standing at the drone that just landed.
REPEAT_READY_STEP = {"text": "Repeat trial: the previous one of this run has ended. Press Enter "
                             "when the area is clear and you are ready. The setup and reset "
                             "steps and the stack launch come next, then the T-0 gate.",
                     "run": "", "when": "repeat"}


def operator_steps_for(operator_steps: Optional[List[Dict[str, Any]]],
                       repeat: bool) -> List[Dict[str, Any]]:
    """The operator steps of one trial: the campaign's, filtered by ``when``. A repeat
    trial whose steps all drop out still gets the one ready step, so it pauses."""
    steps = cs.steps_for(list(operator_steps or []), repeat)
    if repeat and operator_steps and not steps:
        return [dict(REPEAT_READY_STEP)]
    return steps


def setup_steps_for(config: "RunnerConfig", campaign_setup: Optional[List[Dict[str, Any]]],
                    repeat: bool, robot_reset: bool = True) -> List[Dict[str, Any]]:
    """runner.yaml's setup steps, the robots' resets, then the campaign's, filtered by ``when``."""
    before, after = setup_groups(config, campaign_setup, repeat, robot_reset)
    return before + after


def setup_groups(config: "RunnerConfig", campaign_setup: Optional[List[Dict[str, Any]]],
                 repeat: bool, robot_reset: bool = True):
    """(runner.yaml's setup steps, then robot resets + the campaign's), filtered by ``when``.
    The pre-launch checks run between the two groups (2026-11-04): after the runner's own steps,
    which clear a stale stack off the robot's link, and before the resets, which may take a
    minute (a radio power cycle and an access point coming back), so a low battery stops the
    trial in seconds."""
    resets = list(config.robot_resets) if robot_reset else []
    return (cs.steps_for(list(config.setup_steps), repeat),
            cs.steps_for(resets + list(campaign_setup or []), repeat))


def _metadata_probes(raw: Any, path: Path) -> List[Dict[str, Any]]:
    """Validate the {key, command} list of post-trial provenance probes."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RunnerError(f"{path}: metadata_probes must be a list of {{key, command}}")
    probes = []
    for i, entry in enumerate(raw):
        key = str((entry or {}).get("key", "")).strip() if isinstance(entry, dict) else ""
        command = str((entry or {}).get("command", "")).strip() if isinstance(entry, dict) else ""
        if not key or not command:
            raise RunnerError(f"{path}: metadata_probes[{i}] needs a non-empty 'key' and 'command'")
        if key.startswith("_"):
            raise RunnerError(f"{path}: metadata_probes[{i}] key {key!r} may not start with '_' "
                              f"(underscore keys are ignored by the contract validator)")
        as_json = entry.get("json", False)
        if not isinstance(as_json, bool):
            raise RunnerError(f"{path}: metadata_probes[{i}] json must be true or false")
        probes.append({"key": key, "command": command, "json": as_json})
    return probes


def run_metadata_probes(probes: List[Dict[str, Any]], hooks: "ProcessHooks", target,
                        cwd: Path, placeholders: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Any]:
    """Run each probe through ``sh -c`` on the trial's host; first stdout line per key.

    A probe that fails or prints nothing leaves its key ABSENT rather than
    writing an empty string: an honest hole is cheaper than a confident blank,
    and the contract marks these fields `required: false` for exactly that.

    ``json: true`` on a probe parses its first line as JSON (2026-10-02).

    ``placeholders`` (2026-10-01) are the trial's factor values plus ``{bag}``
    and ``{arg_<name>}``, the same set the verify steps get, so a probe can ask
    about THIS trial (which namespace, which fault was realized). Only known
    names are replaced (``campaign_spec.substitute_known``), so a probe with
    literal braces of its own (``awk '{print $1}'``) runs exactly as before.
    """
    out: Dict[str, Any] = {}
    for probe in probes:
        key, command = probe["key"], probe["command"]
        if placeholders:
            command = cs.substitute_known(command, placeholders)
        try:
            completed = hooks.run_foreground(["sh", "-c", command], target, cwd=cwd, check=False)
        except Exception as exc:  # noqa: BLE001 - a finished trial outranks a probe
            print(f"[runner] metadata probe {key!r} failed ({exc}) - left absent")
            continue
        rc = getattr(completed, "returncode", 0)
        lines = [l.strip() for l in (getattr(completed, "stdout", "") or "").splitlines()
                 if l.strip()]
        if rc != 0 or not lines:
            print(f"[runner] metadata probe {key!r} rc={rc}, no value - left absent")
            continue
        if probe.get("json"):
            # Opt-in (2026-10-02, E2's `agents` list): the first line is parsed
            # as JSON, so a list or a mapping lands as one. Unparseable output
            # leaves the key absent, as a failed probe does.
            try:
                out[key] = json.loads(lines[0])
            except ValueError:
                print(f"[runner] metadata probe {key!r} printed no valid JSON - left absent")
            continue
        out[key] = lines[0]
    return out


def templated_command(template: str, trial: cs.TrialSpec, what: str,
                      **extra: Any) -> List[str]:
    """Fill {factor} placeholders from the trial's values, as the action does.

    ``extra`` adds non-factor placeholders — ``{bag}`` for the verify steps,
    which need to inspect what was just recorded.
    """
    try:
        return shlex.split(template.format(**trial.values, **extra))
    except KeyError as exc:
        raise RunnerError(f"{what} references unknown factor {exc}") from exc


# --------------------------------------------------------------------------- #
# Command construction (pure — this is what --dry-run shows and tests assert)
# --------------------------------------------------------------------------- #
def launch_command(trial: cs.TrialSpec, factors: Dict[str, cs.Factor],
                   config: RunnerConfig) -> List[str]:
    if config.launch_mode == "run":
        # `ros2 run` takes no name:=value arguments; a single-node stack that
        # needs parameters should use a launch file instead of growing a
        # half-supported argument path here.
        return ["ros2", "run", config.launch_package, config.launch_file]
    cmd = ["ros2", "launch", config.launch_package, config.launch_file]
    for name, value in sorted(trial.launch_args(factors).items()):
        # An EMPTY value is dropped, not passed as a bare `name:=`. `ros2 launch`
        # rejects that outright ("malformed launch argument ..., expected format
        # '<name>:=<value>'") and refuses to start the whole stack, so a single
        # empty factor value takes down the rig rather than the one node it
        # describes. There is no CLI spelling for "pass an empty string" either
        # — the shell strips the quotes in `name:=""` and launch sees the same
        # malformed token — so omitting the argument is the only thing an empty
        # value CAN mean, and it lands on the launch file's own default.
        #
        # Found 2026-08-14, lab day 5: `pose_source: mocap` sets
        # `observer_pose_topic_override: ""` (added with the Exp 3 Part A wiring
        # in 6e06c10db9 and first RUN today), which blocked the T-0 gate with
        # three failures that all read like dead hardware — no Spot driver node,
        # no /mocap/drone_pose, no mocap frames — because the stack never
        # started at all. The declared default for that argument is likewise "",
        # so dropping it is behaviour-preserving.
        if value == "":
            continue
        cmd.append(f"{name}:={value}")
    return cmd


def record_command(trial: cs.TrialSpec, factors: Dict[str, cs.Factor],
                   config: RunnerConfig, bag_dir: Path) -> List[str]:
    if config.record_topics:
        # An explicit list beats a named profile for experiments whose topics are
        # not one of the project's named sets.
        return (["ros2", "bag", "record", "-s", config.record_storage, "-o", str(bag_dir)]
                + list(config.record_topics))

    from .record_profiles import build_record_command, default_record_profile

    profile = None
    for name, value in trial.values.items():
        if name in factors and factors[name].maps_to.get("kind") == "record_profile":
            profile = str(value)
    if profile is None:
        profile = default_record_profile()
    if profile is None:
        # The package used to fall back to a flight set of one lab here. A trial without a
        # declared topic set must not record a guess: it would look finished and hold nothing.
        raise RunnerError(
            f"experiment {trial.experiment!r} declares no topics to record: add a factor with "
            f"maps_to kind record_profile, record_topics in runner.yaml, or "
            f"default_record_profile in trials.yaml")
    # The TRIAL's drone name wins over runner.yaml's default: a factor that
    # renames the drone also renames every one of its topics, and recording the
    # configured name instead would produce a bag full of topics that do not
    # exist — an empty bag that still looks like a finished trial.
    from .record_profiles import default_cf_name

    cf_name = trial.launch_args(factors).get("cf_name") or config.cf_name or default_cf_name()
    return build_record_command(profile, str(bag_dir),
                                cf_name=(str(cf_name) if cf_name else None),
                                storage=config.record_storage,
                                experiments_root=config.experiments_root)


def action_command(trial: cs.TrialSpec, config: RunnerConfig,
                   extra: Optional[Dict[str, Any]] = None) -> List[str]:
    """The experiment-specific 'do the trial' step, templated on factor values.

    Declared in runner.yaml as e.g.
        action: {command: "python3 scripts/fly.py --shape {trajectory}"}
    so adding a factor to the command is a config edit, not a code change.

    ``extra`` adds the non-factor placeholders the runner owns, the same way
    the verify steps get them: ``{bag}`` (the trial's bag directory, so a
    flight script can write its flight-time record, e.g.
    ``commanded_waypoints.json``, next to the bag it belongs to) and the
    resolved ``{arg_<name>}`` launch arguments. Added 2026-08-13 so the
    commanded reference of a goTo flight is recorded by the flight itself,
    not only by crazyflie_server's /rosout log.
    """
    if not config.action:
        return []
    try:
        rendered = config.action.format(**trial.values, **(extra or {}))
    except KeyError as exc:
        raise RunnerError(f"action command references unknown factor {exc}") from exc
    return shlex.split(rendered)


#: Longest trial id we will emit. ext4 allows 255 BYTES per path component, and
#: the id is a DIRECTORY whose contents are named after it - the recorder writes
#: ``<id>_0.mcap`` inside, and the slim tier appends ``_slim`` - so the id itself
#: must leave room. 200 is that, with headroom.
#:
#: This is not hypothetical tidiness. On 2026-08-19 three new factors
#: (arm_aim, max_pose_age_s, ext_pos_std_dev) pushed a markerless trial id to 260
#: bytes and the trial died mid-run with
#:   OSError: [Errno 36] File name too long
#: after the stack was up and Spot had been readied. Every factor added to an
#: experiment lengthens every id that varies it, so the ceiling has to be enforced
#: here rather than remembered by whoever adds the next axis.
MAX_TRIAL_ID_BYTES = 200


def bag_name(trial: cs.TrialSpec, factors: Dict[str, cs.Factor], stamp: str) -> str:
    """Timestamped, and carrying the non-default values so a directory listing
    is already readable without opening any metadata.

    Over ``MAX_TRIAL_ID_BYTES`` the factor section is truncated and an 8-hex
    digest of the FULL name is appended. Readability degrades; uniqueness does
    not, which is the property that matters - two trials differing only in a
    truncated-away factor still get different directories. The complete factor
    set is in trial_metadata.json either way, which is what the index reads.
    """
    parts = [stamp]
    for name in sorted(trial.non_defaults):
        parts.append(f"{name}-{trial.values[name]}")
    parts.append(f"rep{trial.repetition:02d}")
    full = "_".join(str(p).replace("/", "-").replace(" ", "") for p in parts)
    if len(full.encode("utf-8")) <= MAX_TRIAL_ID_BYTES:
        return full

    digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]
    head = f"{stamp}_"
    tail = f"_rep{trial.repetition:02d}_h{digest}"
    budget = MAX_TRIAL_ID_BYTES - len(head.encode()) - len(tail.encode())
    middle = full[len(head):full.rindex("_rep")][:max(0, budget)].rstrip("_-")
    return f"{head}{middle}{tail}"


def trial_metadata(trial: cs.TrialSpec, factors: Dict[str, cs.Factor],
                   config: RunnerConfig, extra: Optional[Dict[str, Any]] = None,
                   trial_id: Optional[str] = None) -> Dict[str, Any]:
    """Metadata for the bag, with every factor under its declared metadata_key.

    Without this the bag would be invisible to the index and uncountable by the
    expander, and the campaign would keep asking for a trial that already flew.

    `trial_id` must be the bag directory's own name. It used to be re-derived
    from the clock at write time, which put a *different* timestamp inside the
    bag than on it — every trial drifted by the length of the run, and the id
    written into rep01 could equal the directory name of rep02.
    """
    metadata: Dict[str, Any] = {
        "trial_id": trial_id or bag_name(
            trial, factors, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")),
        "experiment": config.experiment,
        "aborted": False,
        "repetition": trial.repetition,
        "non_default_factors": list(trial.non_defaults),
        # The resolved launch arguments, verbatim. Factor levels are semantic
        # ("airframe: patched"); this records what the rig was actually told —
        # e.g. WHICH mocap asset provided ground truth — so a bag can be
        # audited without re-reading the factor definitions of the day.
        "launch_args": trial.launch_args(factors),
    }
    for name, value in trial.values.items():
        factor = factors.get(name)
        if factor and factor.metadata_key:
            # An operator-realized factor the campaign does not name was never
            # staged or checked: its default is not written as a realized
            # condition (2026-10-06). The key stays absent, as on old bags.
            if (factor.maps_to.get("kind") == "operator" and trial.named is not None
                    and name not in trial.named):
                continue
            metadata[factor.metadata_key] = value
    # The DOMAIN (2026-10-06): simulation or hardware, so one condition can be
    # counted per domain across experiments (trial_index --condition-space).
    domain, basis = cs.trial_domain(trial.values, factors, config.simulation)
    metadata["domain"] = domain
    metadata["domain_basis"] = basis
    if trial.order:
        # The randomized position of this trial (campaign `order`): seed, block
        # and position, so the order is recoverable from every bag.
        metadata["order"] = dict(trial.order)
    metadata.update(extra or {})
    return metadata


# --------------------------------------------------------------------------- #
# Spoken operator cues (2026-10-13)
# --------------------------------------------------------------------------- #
#: What the runner says on its own on a HARDWARE experiment when the campaign
#: declares no ``voice_announce``. A simulated experiment says nothing unless
#: its campaign asks: an agent running a sim campaign at night must not talk
#: through the laptop speaker. ``before_launch`` has no default because only
#: the campaign knows what the operator must do before the stack comes up.
DEFAULT_VOICE_ANNOUNCE: Dict[str, Optional[str]] = {
    "before_launch": None,
    "record_start": "Recording.",
    "record_end": "Recording stopped.",
    "gate_blocked": "The gate says no go. Nothing was recorded.",
    # Spoken on a GO (2026-11-04, Alejandro: the operator stands at the drone and only heard
    # the NO-GO). On hardware the runner then waits for "fly"; without that pause (--no-pause,
    # or a simulation's plain Enter) the default is shortened to its first sentence.
    "gate_passed": "The gate passed. Type fly when you are ready.",
}


def voice_plan(voice_cues: Optional[List[Any]], voice_announce: Any, *, simulation: bool,
               values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What the runner will say during one trial (pure, so tests and dry runs share it).

    Returns ``{"cues": [operator_voice.Cue, ...] sorted by time,
    "announce": {moment: text or None}}``. ``{factor}`` placeholders in any
    text are filled from the trial's values, as in the operator steps.

    A campaign cue at 0 s replaces the DEFAULT ``record_start`` line, which
    would otherwise say "Recording." right before the campaign's own
    "Recording. Keep still.". An explicit ``record_start`` is always kept.
    """
    values = values or {}
    keys = cs.VOICE_ANNOUNCE_KEYS
    explicit: Dict[str, Any] = dict(voice_announce) if isinstance(voice_announce, dict) else {}
    if voice_announce is False:
        announce: Dict[str, Optional[str]] = {k: None for k in keys}
    else:
        announce = ({k: DEFAULT_VOICE_ANNOUNCE.get(k) for k in keys} if not simulation
                    else {k: None for k in keys})
        for key, text in explicit.items():
            if key in announce:
                announce[key] = str(text) if text not in (None, False, "") else None
    cues = voice.as_cues(voice_cues)
    if "record_start" not in explicit and any(c.at_s == 0 for c in cues):
        announce["record_start"] = None
    fill = lambda text: cs.substitute_known(text, values).strip()  # noqa: E731
    return {"cues": [voice.Cue(c.at_s, fill(c.text)) for c in cues],
            "announce": {k: (fill(v) if v else None) for k, v in announce.items()}}


def voice_plan_lines(plan: Dict[str, Any]) -> List[str]:
    """The plan as printed lines, in the order the trial would speak them."""
    announce = plan.get("announce") or {}
    lines: List[str] = []
    only = {"gate_blocked": "  (only on a NO-GO)", "gate_passed": "  (only on a GO)"}
    for moment in ("before_launch", "gate_blocked", "gate_passed", "record_start"):
        if announce.get(moment):
            lines.append(f"{moment}: {announce[moment]}" + only.get(moment, ""))
    for cue in plan.get("cues") or []:
        lines.append(f"+{cue.at_s:.1f} s: {cue.text}")
    if announce.get("record_end"):
        lines.append(f"record_end: {announce['record_end']}")
    return lines


def voice_target_line() -> str:
    """Where the cues go, or why they do not, for the runner's narrative."""
    if not voice.enabled():
        return f"voice OFF ({voice.ENV_SWITCH}): cues are printed only"
    host, port = voice.target()
    return f"voice cues go to {host}:{port} (UDP, {voice.ENV_HOST}/{voice.ENV_PORT})"


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
@dataclass
class ProcessHooks:
    """The three process primitives, injectable so tests need no ROS.

    Defaults come from remote_exec_utils, which already encodes the hard-won
    behaviour: process groups via os.setsid, SIGINT -> SIGTERM -> SIGKILL
    escalation, and a remote pkill scoped by `kind` so stopping a recorder does
    not also kill the long-running launch on the same host.
    """
    start_background: Callable[..., Any] = None
    run_foreground: Callable[..., Any] = None
    stop_process: Callable[..., Any] = None

    @classmethod
    def default(cls) -> "ProcessHooks":
        from .remote_exec_utils import run_foreground, start_background, stop_remote_process
        return cls(start_background=start_background, run_foreground=run_foreground,
                   stop_process=stop_remote_process)

    def with_local_setup(self, shell: str, files: List[str]) -> "ProcessHooks":
        """These hooks, with every LOCAL command run after sourcing ``files``
        (RunnerConfig.local_setup). Remote commands are wrapped by their target."""
        from .remote_exec_utils import local_setup_command
        start, run = self.start_background, self.run_foreground

        def start_background(cmd, target, **kwargs):
            return start(cmd if target is not None else local_setup_command(cmd, files, shell),
                         target, **kwargs)

        def run_foreground(cmd, target, **kwargs):
            return run(cmd if target is not None else local_setup_command(cmd, files, shell),
                       target, **kwargs)

        return dataclasses.replace(self, start_background=start_background,
                                   run_foreground=run_foreground)


@dataclass
class TrialResult:
    trial: cs.TrialSpec
    bag_dir: Optional[Path]
    status: str                 # ran | blocked | failed | dry-run
    detail: str = ""
    commands: List[List[str]] = field(default_factory=list)
    #: The attempt ledger line this result produced (None for a dry run).
    ledger: Optional[Dict[str, Any]] = None


#: Trial phases an operator may toggle per run. All on by default except the
#: livestream (foxglove_bridge costs bandwidth on the lab Wi-Fi). Turning a
#: phase off changes what the trial MEANS — a record-off trial can never count
#: as coverage — so the toggles are explicit here, not hidden in the config.
DEFAULT_STEPS = {"setup": True, "launch": True, "record": True,
                 "action": True, "livestream": False}


def tally(statuses: Iterable[str]) -> Dict[str, Any]:
    """The closing count every surface shows, computed in ONE place.

    Leads with the successes on purpose (the operator, 2026-08-12: "1 blocked,
    0 failed" after two clean flights read as "it crashed" at the rig). The CLI
    prints ``line`` and exits on ``bad``, and the web UI shows the same
    ``line`` on its run page, so the two can never disagree about how a batch
    went.
    """
    statuses = list(statuses)
    ok = sum(1 for s in statuses if s == "ran")
    blocked = sum(1 for s in statuses if s == "blocked")
    failed = sum(1 for s in statuses if s == "failed")
    return {"total": len(statuses), "ok": ok, "blocked": blocked, "failed": failed,
            "bad": bool(blocked or failed),
            "line": f"{len(statuses)} trial(s): {ok} OK, {blocked} blocked, {failed} failed"}


def speak_gate_reasons(result: Any, limit: int = 3) -> List[str]:
    """Speak up to ``limit`` failed checks of a NO-GO in plain sentences (2026-11-04): the
    operator in the flight volume cannot read the screen, and "The gate says no go" alone
    sent them to the laptop for every block. Silent when the voice is off."""
    from . import gate_speech
    items = list(getattr(result, "failed_items", None) or [])
    lines = gate_speech.sentences(items, limit=limit)
    for line in lines:
        voice.speak(line, prefix="[voice] gate reason: ")
    return lines


def confirm_operator_steps_tty(trial: cs.TrialSpec,
                               operator_steps: List[Dict[str, Any]]) -> bool:
    """The CLI's confirmation surface: print the steps, wait for Enter.

    Only at a real terminal. A non-tty caller passes straight through, which
    keeps headless and CI behaviour exactly what it was before 8f1213cc6e.
    Callers with their OWN confirmation surface (the web UI) do not use this:
    they hand ``run_trial``/``run_plan`` a ``confirm_steps`` callable instead,
    because blocking on this process's stdin from a browser-driven worker
    thread waits on a terminal nobody is watching.
    """
    if not sys.stdin.isatty():
        return True
    print(_c("36;1", f"\n[runner] OPERATOR STEPS for {trial.describe()}"))

    class _KeepUnknown(dict):
        # A step may name a factor ("SET THE LIGHTS TO `{lighting}`") so the
        # operator reads the trial's own level; a brace that is not a factor
        # stays as written instead of raising (2026-10-07, lab day: the
        # placeholder was printed literally).
        def __missing__(self, key):
            return "{" + key + "}"

    values = _KeepUnknown(trial.values)
    for i, step in enumerate(operator_steps, 1):
        text = str(step.get("text", "")).strip()
        try:
            text = text.format_map(values)
        except (ValueError, IndexError):
            pass  # literal braces or positional fields: show the text as written
        first, *rest = text.splitlines() or [""]
        print(f"  {_c('36;1', f'{i}.')} {_c('1', first)}")
        for line in rest:
            print(f"     {line}")
    print(_c("33;1", "[runner] Press Enter when the steps above are DONE "
                     "— the T-0 gate checks them next."))
    try:
        input()
    except EOFError:
        pass
    return True


def stamp_code_version(metadata: Dict[str, Any], target) -> None:
    """Record which code ran the trial in ``metadata["code_version"]`` and print it. An
    unknown version is printed as a WARNING (2026-10-15): the adoption test found a
    ``commit: null`` only when it read the bag, because the line looked like any other."""
    try:
        from . import code_version as cv
        metadata["code_version"] = cv.cached(target)
        print(f"[runner] code version: {cv.summary(metadata['code_version'])}")
        if metadata["code_version"].get("warning"):
            print(f"[runner] WARNING: {metadata['code_version']['warning']}")
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] WARNING: code version unavailable ({exc}) - continuing")


def _run_trial(trial: cs.TrialSpec, factors: Dict[str, cs.Factor], config: RunnerConfig,
              bags_dir: Path, *, _ctx: Dict[str, Any], gate=None, dry_run: bool = True,
              hooks: Optional[ProcessHooks] = None, slim: bool = True,
              steps: Optional[Dict[str, bool]] = None, hold_s: float = 60.0,
              session: Optional[Dict[str, Any]] = None,
              extra_verify: Optional[List[Dict[str, Any]]] = None,
              quiet_children: bool = False,
              pause_before_action: bool = True,
              operator_steps: Optional[List[Dict[str, Any]]] = None,
              scratch_label: Optional[str] = None,
              confirm_steps: Optional[Callable[[cs.TrialSpec, List[Dict[str, Any]]], bool]] = None,
              confirm_pose: Optional[Callable[[cs.TrialSpec], bool]] = None,
              clock: Callable[[], str] = lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"),
              voice_cues: Optional[List[Any]] = None,
              voice_announce: Any = None,
              repeat: bool = False,
              campaign_setup_steps: Optional[List[Dict[str, Any]]] = None,
              robot_reset: bool = True,
              ) -> TrialResult:
    """One trial, end to end.

    The ORDER is the whole point, and it is the order the notebooks learned:

      gate -> launch (background) -> settle -> record (background)
           -> action (foreground) -> STOP RECORDER FIRST -> stop stack -> slim

    Stopping the recorder before the stack is not cosmetic: rosbag2 needs to
    finalize, and tearing the stack down first has produced truncated bags.
    Teardown lives in a ``finally`` so a crashing action still closes the bag
    rather than leaving an orphaned recorder holding the file.
    """
    marks: Dict[str, Any] = _ctx.setdefault("marks", {})
    _t_start = time.monotonic()

    def mark(stage: str) -> None:
        """Seconds since the attempt began, per stage, for the attempt ledger."""
        marks[stage] = round(time.monotonic() - _t_start, 2)
        _ctx.setdefault("wall", {})[stage] = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds")

    trace: List[Dict[str, Any]] = _ctx.setdefault("trace", [])

    def trace_step(name: str, kind: str, *, ok: bool, since: Optional[float] = None,
             rc: Optional[int] = None, output: str = "", note: str = "",
             blocking: bool = True) -> None:
        """One record of the step trace, for the ledger and the failure report.

        The idea is ASPIRE's (Lu et al. 2026, arXiv 2607.00272), whose agent finds
        a failing program step from a per-call trace: what ran, when, how long, how
        it ended, and the evidence of a failure. Only a failing step keeps its
        output tail, so a passing trial costs a few short lines in the ledger.
        """
        now = time.monotonic()
        record: Dict[str, Any] = {"step": name, "kind": kind, "ok": ok,
                                  "t_s": round((since if since is not None else now)
                                               - _t_start, 2)}
        if since is not None:
            record["duration_s"] = round(now - since, 2)
        if rc is not None:
            record["rc"] = rc
        if note:
            record["note"] = note[:500]
        if not ok and not blocking:
            record["blocking"] = False   # recorded, but the trial went on
        lines = (output or "").strip().splitlines()
        if not ok and lines:
            record["output_tail"] = lines[-TRACE_TAIL_LINES:]
        trace.append(record)

    mark("start")
    target = config.remote_target(trial.host(factors))
    hooks = hooks or ProcessHooks.default()
    local_env = config.local_setup(trial.host(factors))
    if local_env is not None:
        hooks = hooks.with_local_setup(*local_env)
    steps = {**DEFAULT_STEPS, **(steps or {})}

    # SCRATCH FLIGHT (2026-08-12): scratch_label is not None means this is an
    # ad-hoc diagnostic flight, not a campaign trial. Recording is NOT optional
    # for one: a scratch flight exists to answer a question, and an unrecorded
    # answer is an anecdote. The bag goes to bags_scratch/ (the caller's
    # bags_dir), which the coverage index never reads, so forcing the recorder
    # on can never pollute a campaign.
    if scratch_label is not None:
        steps = {**steps, "record": True}

    # OPERATOR STEPS, shown and WAITED ON before anything runs (2026-08-12,
    # the operator): the CLI runner never displayed the campaign's operator_steps
    # at all — the web UI shows them, the CLI silently assumed them done — so
    # the T-0 gate raced the operator's physical prep (the Motive body switch
    # for the patched arm lost that race today and blocked a healthy trial).
    # The confirmation SURFACE is a seam: the default is the CLI's tty prompt
    # (which passes straight through when stdin is not a tty), and the web UI
    # supplies a callable that waits on a browser button instead — without the
    # seam, a UI-triggered run would block on input() at a terminal nobody is
    # watching. Dry runs skip. A False from the surface means the steps were
    # NOT confirmed (the operator aborted), so nothing launches.
    # FIRST OR REPEAT (2026-11-04): a step's `when` picks the steps of this trial. A
    # repeat is a trial after another one of the same runner invocation that was not
    # blocked (run_plan decides), so a blocked trial falls back to the full procedure.
    operator_steps = operator_steps_for(operator_steps, repeat)
    _ctx["step_set"] = "repeat" if repeat else "first"
    if operator_steps:
        print(f"[runner] {'repeat' if repeat else 'first'} trial of this run: "
              f"{len(operator_steps)} operator step(s)")
    if operator_steps and not dry_run:
        confirm = confirm_steps if confirm_steps is not None else confirm_operator_steps_tty
        if not confirm(trial, operator_steps):
            return TrialResult(trial, None, "blocked",
                               "operator steps not confirmed — nothing was launched", [])

    # Continuous-session mode: when the caller carries a session dict and a
    # stack is already running in it, REUSE that stack instead of killing and
    # relaunching it. The expensive part of a trial is bringing the rig up
    # (~8 s settle plus camera/mocap/radio re-init); flying it is the cheap
    # part. Only the recorder cycles per trial, which is exactly the boundary
    # a bag needs anyway.
    reuse_stack = bool(session is not None and session.get("procs", {}).get("launch_stack"))
    if reuse_stack:
        steps = {**steps, "setup": False, "launch": False}
        print("[runner] reusing the running stack (continuous session)")

    stamp = clock()
    # WHERE THE BAG IS WRITTEN. Unset (the default) means the repo's bags/, byte
    # for byte what every trial did before 2026-08-04. Set, it points at
    # external storage so a replay-grade flight does not have to fit on the
    # rig's internal disk. Resolved per trial because the host is a factor, and
    # the disk is plugged into one machine.
    host_level = trial.host(factors)
    try:
        record_dir, external_record = bstore.resolve_bags_dir(
            config.bags_root_for(host_level), experiment=config.experiment,
            repo_bags_dir=bags_dir,
            level=str(host_level) if host_level is not None else None,
            # A scratch flight mirrors its own directory name externally too,
            # so an offloaded scratch bag can never land among real trials.
            record_dir_name=bags_dir.name,
            # <root>/<experiment>/bags, so storage.yaml is read from the same
            # tree the trial belongs to rather than from wherever this file sits.
            experiments_root=bags_dir.parents[1])
    except bstore.StorageError as exc:
        # Blocked rather than failed: a destination that cannot be resolved will
        # not resolve for the next trial either, and grinding through a campaign
        # writing bags to the wrong disk is the outcome this refuses to have.
        print(f"[runner] RECORD DESTINATION UNUSABLE - not recording, the action does not run")
        return TrialResult(trial, None, "blocked", str(exc), [])
    if scratch_label is None:
        bag_dir = record_dir / bag_name(trial, factors, stamp)
        _ctx["label"] = bag_dir.name
    else:
        # The name is generated NOW, at run time, and de-collided against what
        # is already on disk. 2026-08-12: a hand-built recorder pane pre-typed
        # with a bag name from earlier in the session captured 53 s of a landed
        # drone instead of the flight. A name nobody typed cannot be stale.
        base = f"{stamp}_scratch" + (f"_{scratch_label}" if scratch_label else "")
        bag_dir = record_dir / base
        suffix = 2
        while bag_dir.exists():
            bag_dir = record_dir / f"{base}-{suffix}"
            suffix += 1
        _ctx["label"] = bag_dir.name
    _ctx["bag_dir"] = bag_dir
    if external_record:
        print(f"[runner] recording to EXTERNAL storage: {record_dir}")
        print(f"[runner]   a symlink will be left at {bags_dir / bag_dir.name} so the "
              f"index, the slim step and the analyses keep working unchanged")
    # Resolved launch arguments are available to any command as {arg_<name>}:
    # a node often needs the VALUE a factor resolved to (the mocap asset name)
    # rather than the semantic level ("bare"), and re-deriving it in the
    # template would duplicate the mapping that factors.yaml already owns.
    arg_ph = {f"arg_{k}": v for k, v in trial.launch_args(factors).items()}
    launch_cmd = launch_command(trial, factors, config) if steps["launch"] else None
    record_cmd = record_command(trial, factors, config, bag_dir) if steps["record"] else None
    # The action gets {bag} (and {arg_*}) like the verify steps do, so a flight
    # script can be told where its trial's outputs live and leave its own
    # record of the commanded reference beside the bag (2026-08-13).
    action_cmd = (action_command(trial, config,
                                 extra={"bag": str(bag_dir), **arg_ph})
                  if steps["action"] else None)
    livestream_cmd = (shlex.split(config.livestream)
                      if steps["livestream"] and config.livestream else None)
    def _display(cmd: List[str]) -> List[str]:
        """What would actually run, ssh/docker wrapper included, so --dry-run is
        an honest preview rather than the un-wrapped local form."""
        if target is None:
            if local_env is None:
                return cmd
            from .remote_exec_utils import local_setup_command
            return local_setup_command(cmd, local_env[1], local_env[0])
        from .remote_exec_utils import wrap_command
        return wrap_command(cmd, target)

    def _setup(group):
        return [(s["name"], templated_command(s["command"], trial, f"setup step {s['name']}",
                                              **arg_ph), s["required"]) for s in group]
    _before, _after = (setup_groups(config, campaign_setup_steps, repeat, robot_reset)
                       if steps["setup"] else ([], []))
    setup_before_cmds, setup_after_cmds = _setup(_before), _setup(_after)
    setup_cmds = setup_before_cmds + setup_after_cmds
    # The pre-launch checks (2026-11-04): checklist entries with "stage": "pre_launch", run
    # between the two setup groups, before the stack, so a low battery stops the trial in seconds.
    pre_launch_names = (gate.stage_checks("pre_launch")
                        if gate is not None and hasattr(gate, "stage_checks") else [])
    # Aux launches get {bag} too (2026-08-18), for the same reason the action
    # does: a side process that records something ABOUT the trial has to be able
    # to put it beside the trial. Added for sample_process_resources.py, whose
    # whole value is that its output is filed with the bag it describes rather
    # than in some scratch path nobody correlates later.
    aux_cmds = ([(a["name"], templated_command(a["command"], trial, f"aux launch {a['name']}",
                                               **{"bag": str(bag_dir), **arg_ph}))
                 for a in config.aux_launches] if steps["launch"] else [])
    if livestream_cmd:
        aux_cmds.append(("livestream", livestream_cmd))

    commands = [_display(c) for c in
                ([c for _, c, _ in setup_cmds] + [launch_cmd]
                 + [c for _, c in aux_cmds] + [record_cmd, action_cmd]) if c]
    skipped = [k for k in DEFAULT_STEPS if not steps[k] and k != "livestream"]
    if skipped:
        print(f"[runner] steps DISABLED for this trial: {', '.join(skipped)}")

    gate_result = None
    # A GATE FACTOR (2026-10-01): the trial itself says whether the gate binds,
    # shadows or is disabled. Refused before anything runs unless the
    # experiment is declared simulated, and refused when the gate in hand
    # cannot change mode (the web UI's shell gate): running a `gate=off` trial
    # through a binding gate would silently turn the arm into `gate=on`.
    gate_spec = trial.gate_mode(factors)
    if gate_spec is not None:
        g_name, g_level, g_mode = gate_spec
        refusal = ""
        if not config.simulation:
            refusal = (f"factor {g_name}={g_level} selects the gate mode {g_mode!r}, which "
                       f"runner.yaml allows only with `simulation: true`")
        elif not dry_run and gate is None:
            refusal = f"factor {g_name}={g_level} needs a T-0 gate, and none was built"
        elif not dry_run and not hasattr(gate, "with_mode"):
            refusal = (f"factor {g_name}={g_level} needs a gate that can change mode, "
                       f"and this one ({type(gate).__name__}) cannot. Run it from the CLI")
        if refusal:
            print(_c("31;1", f"[runner] REFUSED - {refusal}"))
            return TrialResult(trial, None, "blocked", refusal, commands)
    # FACTOR CHECKS (2026-10-06): a level the rig cannot be told (the room's
    # lighting, set by the operator) is confirmed by a measurement in the T-0
    # gate, with the level's own arguments. A trial that declares such a level
    # is refused when the gate in hand cannot add the check: recording it
    # unchecked would make the declared level a claim again.
    factor_checks = cs.factor_check_entries(trial.values, factors, trial.named)
    calibrations: List[Dict[str, str]] = []
    calibration_results: Dict[str, Any] = {}
    if trial.factor_check_mode == "calibration":
        # A CALIBRATION run measures the factor's condition instead of checking
        # it (its bands do not exist yet). Scratch only: its bag must never be
        # mistaken for a trial whose level was confirmed.
        if scratch_label is None:
            refusal = ("this campaign declares `factor_checks: calibration`: it MEASURES the "
                       "factor checks' conditions instead of checking them, so it runs only "
                       "with --scratch and can never count as coverage")
            print(_c("31;1", f"[runner] REFUSED - {refusal}"))
            return TrialResult(trial, None, "blocked", refusal, commands)
        calibrations = cs.factor_calibration_commands(trial.values, factors, trial.named)
        for entry in factor_checks:
            print(f"[runner] CALIBRATION run: the check {entry['check']} of "
                  f"{entry['factor']}={entry['level']} is measured, not enforced")
        factor_checks = []
    if factor_checks:
        for entry in factor_checks:
            rendered = " ".join(f"{k}={v if v != '' else '(EMPTY)'}"
                                for k, v in entry["args"].items())
            print(f"[runner] factor check {entry['factor']}={entry['level']}: "
                  f"{entry['check']} {rendered}")
        refusal = ""
        if not dry_run and gate is None:
            refusal = (f"factor check(s) for {', '.join(e['factor'] for e in factor_checks)} "
                       f"need a T-0 gate, and none was built")
        elif not dry_run and not hasattr(gate, "with_extra"):
            refusal = (f"factor check(s) need a gate that can add checks, and this one "
                       f"({type(gate).__name__}) cannot. Run it from the CLI")
        if refusal:
            print(_c("31;1", f"[runner] REFUSED - {refusal}"))
            return TrialResult(trial, None, "blocked", refusal, commands)
    # SPOKEN CUES (2026-10-13): what the laptop will say during this trial.
    # Sending is one non-blocking UDP datagram per line, so a voice daemon that
    # is down costs nothing and changes nothing about the trial.
    vplan = voice_plan(voice_cues, voice_announce, simulation=config.simulation,
                       values=trial.values)
    if dry_run:
        # A dry run previews commands; there is no stack to check and the gate
        # bounces the ros2 daemon, so running it here would be a side effect.
        # The voice plan is printed, never sent.
        if operator_steps:
            print(f"[dry-run] operator steps of this {'repeat' if repeat else 'first'} trial "
                  f"(shown and waited on before anything runs):")
            for i, step in enumerate(operator_steps, 1):
                first_line = (str(step.get("text", "")).strip().splitlines() or [""])[0]
                print(f"[dry-run]   {i}. {first_line[:150]}")
        if setup_before_cmds:
            print("[dry-run] setup steps (runner.yaml): "
                  + ", ".join(name for name, _, _ in setup_before_cmds))
        if pre_launch_names:
            print("[dry-run] pre-launch checks, before the stack: " + ", ".join(pre_launch_names))
        if setup_after_cmds:
            print("[dry-run] setup steps, before the launch: "
                  + ", ".join(name for name, _, _ in setup_after_cmds))
        lines = voice_plan_lines(vplan)
        if lines:
            print(f"[runner] voice plan (dry run, nothing is sent, {voice_target_line()}):")
            for line in lines:
                print(f"[runner]   {line}")
        return TrialResult(trial, bag_dir, "dry-run", "nothing executed", commands)

    def announce(moment: str) -> None:
        """Speak one of the runner's fixed lines. Never raises, never blocks."""
        text = vplan["announce"].get(moment)
        if not text:
            return
        sent = voice.speak(text, prefix=f"[voice] {moment}: ")
        _ctx.setdefault("voice", {}).setdefault("announced", []).append(
            {"moment": moment, "text": text, "sent": sent})

    cwd = config.project_root
    procs: Dict[str, Any] = {}
    detail = ""
    pre_launch_meta = None
    # Everything this trial prints, on disk next to the experiment. The UI's
    # log is in memory and dies with the process; the terminal's scrollback
    # dies with the window. A trial that failed at 23:00 has to be explainable
    # at 09:00, so the run log is written where the trial is, not where the
    # operator happened to be standing.
    log_path = bags_dir.parent / "runs" / f"{bag_dir.name}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w", encoding="utf-8")
    except OSError:
        log_file = None

    class _LogTee:
        def __init__(self, stream, sink):
            self._stream, self._sink = stream, sink

        def write(self, text):
            try:
                self._sink.write(_ANSI_RE.sub("", text))
                self._sink.flush()
            except (OSError, ValueError):
                pass
            return self._stream.write(text)

        def flush(self):
            self._stream.flush()

    _saved_stdout = sys.stdout
    if log_file is not None:
        sys.stdout = _LogTee(_saved_stdout, log_file)
    try:
        announce("before_launch")
        # Setup steps run before the stack: they are the preconditions, so failing
        # one must abort the trial rather than record a flight whose rig was never
        # actually configured. Two groups since 2026-11-04, with the pre-launch
        # checks between them (setup_groups says why).
        def _gate_for(stage):
            g = gate.with_stage(stage) if hasattr(gate, "with_stage") else gate
            if hasattr(g, "with_target"):
                g = g.with_target(target, local_env, config.project_root)
            if gate_spec is not None and hasattr(g, "with_mode"):
                g = g.with_mode(gate_spec[2], reason=f"shadow: {gate_spec[0]}={gate_spec[1]} "
                                                     f"(the gate runs and records, a NO-GO does not block)")
            return g

        def _gate_no_go(stage_label, g, exc, since):
            mark("gate_done")
            last = getattr(g, "last_result", None)
            if last is not None and hasattr(last, "as_metadata"):
                _ctx["gate"] = last.as_metadata()
                if stage_label != "gate":
                    _ctx["gate"]["stage"] = "pre_launch"
            trace_step(stage_label, "gate", ok=False, since=since, output=str(exc),
                       note=_gate_note(_ctx.get("gate")))
            where = "PRE-LAUNCH CHECKS" if stage_label != "gate" else "GATE"
            print(_c("31;1", f"[runner] {where} NO-GO - not recording, the action does not run"))
            announce("gate_blocked")
            speak_gate_reasons(last)
            _note_gate_refusal(bag_dir, _ctx.get("gate"), str(exc))
            return TrialResult(trial, None, "blocked", str(exc), commands)

        def _run_setup(group):
            nonlocal detail
            for name, cmd, required in group:
                print(f"[setup] {name}")
                _t_step = time.monotonic()
                completed = hooks.run_foreground(cmd, target, cwd=cwd, check=False)
                rc = getattr(completed, "returncode", 0)
                mark(f"setup:{name}")
                trace_step(f"setup:{name}", "setup", ok=rc == 0, since=_t_step, rc=rc,
                     output=getattr(completed, "stdout", "") or "", blocking=required,
                     note="" if rc == 0 or required else "optional, the trial continued")
                if rc != 0 and required:
                    raise RunnerError(f"setup step {name!r} failed (rc={rc})")
                if rc != 0:
                    detail = f"{detail} | optional setup {name} rc={rc}".strip(" |")

        _run_setup(setup_before_cmds)
        # THE PRE-LAUNCH CHECKS, between the two setup groups.
        if pre_launch_names:
            pre_gate = _gate_for("pre_launch")
            mark("pre_launch_start")
            _t_pre = time.monotonic()
            try:
                try:
                    pre_result = pre_gate.check(bag_dir.name, {**trial.values, **arg_ph})
                except TypeError:
                    pre_result = pre_gate.check(bag_dir.name)
            except Exception as exc:  # PreflightBlocked, or anything else
                return _gate_no_go("gate:pre_launch", pre_gate, exc, _t_pre)
            mark("pre_launch_done")
            if hasattr(pre_result, "as_metadata"):
                pre_launch_meta = pre_result.as_metadata()
            trace_step("gate:pre_launch", "gate",
                       ok=(pre_launch_meta or {}).get("verdict") != "NO-GO",
                       since=_t_pre, note=_gate_note(pre_launch_meta), blocking=False)
        _run_setup(setup_after_cmds)

        if launch_cmd:
            procs["launch_stack"] = hooks.start_background(
                launch_cmd, target, cwd=cwd, label="stack",
                quiet_sink=log_file if quiet_children else None)
            mark("launch")
            trace_step("launch", "launch", ok=True, note="started in the background")
            time.sleep(config.settle_s)
            mark("settled")
        for name, cmd in aux_cmds:
            print(f"[aux] {name}")
            procs[f"aux:{name}"] = hooks.start_background(
                cmd, target, cwd=cwd, label=name,
                quiet_sink=log_file if quiet_children else None)
            mark(f"aux:{name}")
            trace_step(f"aux:{name}", "aux", ok=True, note="started in the background")
        # T-0 GATE, here and not earlier: most of what it checks — camera
        # frames, mocap continuity, EKF convergence, battery, e-stop
        # reachability — only exists once the stack is UP. Running it before
        # the launch (as this did until 2026-07-29) meant the first trial of
        # every session was gated against a rig that was not running yet, so
        # it either blocked on a healthy setup or, worse, passed against the
        # STALE rig that the setup steps (scripts/kill_ros.sh) were about to
        # tear down.
        #
        # Before the recorder and before the action: a no-go must not produce
        # a bag, and nothing has flown yet. Teardown is in the finally below.
        if gate is not None:
            # Without its pre-launch entries when they ran before the stack (2026-11-04).
            trial_gate = (gate.with_stage("t0") if pre_launch_names and hasattr(gate, "with_stage")
                          else gate)
            # The gate runs where the trial runs (2026-10-14): a laptop-driven
            # trial on the Orin used to check the LAPTOP's processes, disk,
            # network and stop latch. A gate that cannot move (a test double,
            # the UI's shell gate) keeps running here.
            if hasattr(trial_gate, "with_target"):
                trial_gate = trial_gate.with_target(target, local_env, config.project_root)
            if gate_spec is not None:
                trial_gate = trial_gate.with_mode(
                    gate_spec[2], reason=f"shadow: {gate_spec[0]}={gate_spec[1]} "
                                         f"(the gate runs and records, a NO-GO does not block)")
            if factor_checks:
                trial_gate = trial_gate.with_extra(factor_checks)
            mark("gate_start")
            _t_gate = time.monotonic()
            try:
                # The trial's factor values go with it: a gate that cannot see what
                # kind of trial this is has to be either too strict or too lax.
                # The resolved launch arguments go too, as arg_<name> (2026-10-02,
                # E2): a fleet check needs the namespace layout the launch was
                # told (arg_symmetric_ns), which the semantic level cannot give.
                try:
                    gate_result = trial_gate.check(bag_dir.name, {**trial.values, **arg_ph})
                except TypeError:
                    gate_result = trial_gate.check(bag_dir.name)   # older gate signature
            except Exception as exc:  # PreflightBlocked, or anything else
                mark("gate_done")
                last = getattr(trial_gate, "last_result", None)
                if last is not None and hasattr(last, "as_metadata"):
                    _ctx["gate"] = last.as_metadata()
                trace_step("gate", "gate", ok=False, since=_t_gate, output=str(exc),
                     note=_gate_note(_ctx.get("gate")))
                print(_c("31;1", "[runner] GATE NO-GO - not recording, the action does not run"))
                announce("gate_blocked")
                speak_gate_reasons(last)
                _note_gate_refusal(bag_dir, _ctx.get("gate"), str(exc))
                return TrialResult(trial, None, "blocked", str(exc), commands)
            mark("gate_done")
            if hasattr(gate_result, "as_metadata"):
                _ctx["gate"] = gate_result.as_metadata()
            # A shadow gate (a gate factor's "off" level) records a NO-GO and lets the
            # trial run: a failing, non-blocking step in the trace.
            trace_step("gate", "gate", ok=(_ctx.get("gate") or {}).get("verdict") != "NO-GO",
                 since=_t_gate, note=_gate_note(_ctx.get("gate")), blocking=False)
            # A real GO is spoken, a shadow gate's recorded NO-GO is not ("passed" would lie).
            if (_ctx.get("gate") or {}).get("verdict") != "NO-GO":
                text = vplan["announce"].get("gate_passed")
                if (text and text == DEFAULT_VOICE_ANNOUNCE["gate_passed"]
                        and (not pause_before_action or config.simulation)):
                    vplan["announce"]["gate_passed"] = "The gate passed."
                announce("gate_passed")

        # CALIBRATION MEASUREMENTS (factor_checks: calibration, scratch only):
        # the stack is up and the gate passed, so the condition a factor check
        # would confirm is measured now, with the trial's level. The proposed
        # band is the last JSON line of the command; it goes into the run log
        # and the scratch bag's metadata, and into factors.yaml only through a
        # reviewed commit.
        for entry in calibrations:
            print(f"[calibrate] {entry['factor']}={entry['level']}: {entry['command']}")
            completed = hooks.run_foreground(["sh", "-c", entry["command"]], target,
                                             cwd=cwd, check=False)
            out_lines = [l.strip() for l in (getattr(completed, "stdout", "") or "").splitlines()
                         if l.strip()]
            parsed: Any = None
            for line in reversed(out_lines):
                try:
                    parsed = json.loads(line)
                    break
                except ValueError:
                    continue
            calibration_results[entry["factor"]] = {
                "level": entry["level"], "rc": getattr(completed, "returncode", 0),
                "measurement": parsed}
            print(f"[calibrate] {entry['factor']}={entry['level']} -> {json.dumps(parsed)}")

        # OPERATOR PAUSE (2026-08-11): the runner used to roll straight from a
        # gate PASS into recording, so every operator act (stand Spot, set the
        # gripper) had to race the dwell — the operator missed the window twice.
        # Same seam as the operator steps above: the default is the tty Enter
        # wait, and a caller with its own surface (the web UI) passes
        # ``confirm_pose`` instead. Without the seam a UI-triggered run on a
        # server started from a terminal parked HERE, mid-trial, stack up, on
        # an input() nobody could see. A False (the operator aborted while
        # posing) stops the trial before anything is recorded or flown; the
        # finally below tears the stack down.
        if pause_before_action and not dry_run:
            nd = ", ".join(f"{k}={v}" for k, v in sorted(trial.values.items())
                           if k in (trial.non_defaults or [])) or "all defaults"
            if confirm_pose is not None:
                if not confirm_pose(trial):
                    return TrialResult(trial, None, "blocked",
                                       "not confirmed at the pose pause — "
                                       "nothing was recorded or flown", commands)
            elif sys.stdin.isatty():
                # A bare Enter is not enough on hardware (2026-10-29): the cockpit pane is
                # shared between the operator's terminal and an agent driving it over tmux, so
                # an Enter can come from either, and a flight must not start on an accidental
                # one. On hardware the word ``fly`` starts the trial, ``no`` aborts it, anything
                # else asks again (three tries, then it aborts). Simulation keeps the plain Enter.
                if config.simulation:
                    print(_c("33;1", f"[runner] T-0 gate done — POSE THE RIG NOW "
                                     f"({nd}). Recording starts on Enter."))
                    try:
                        input()
                    except EOFError:
                        pass
                else:
                    print(_c("33;1", f"[runner] T-0 gate done — POSE THE RIG NOW "
                                     f"({nd}). Type fly and Enter to record and act, "
                                     f"no to abort."))
                    answer = ""
                    for _attempt in range(3):
                        try:
                            answer = input().strip().lower()
                        except EOFError:
                            answer = "no"
                        if answer in ("fly", "no"):
                            break
                        print(_c("33;1", "[runner] type fly to start, or no to abort"))
                    if answer != "fly":
                        return TrialResult(trial, None, "blocked",
                                           "not confirmed at the pose pause (no 'fly' typed) — "
                                           "nothing was recorded or flown", commands)

        if record_cmd and external_record:
            # rosbag2 creates its own output directory but not the parents of
            # one. On external storage the parents are <root>/<experiment>/bags,
            # which on a freshly mounted disk do not exist yet, and the recorder
            # would print one line and produce nothing.
            hooks.run_foreground(["mkdir", "-p", str(record_dir)], target,
                                 cwd=cwd, check=False)
        if record_cmd:
            procs["record"] = hooks.start_background(
                record_cmd, target, cwd=cwd, label="recorder",
                quiet_sink=log_file if quiet_children else None)
            mark("record")
            trace_step("record", "record", ok=True, note="recorder started in the background")
            time.sleep(1.0)
            announce("record_start")
        # The campaign's cue timeline, t0 = now: the recorder has had its 1 s to
        # start, and the action (or the manual hold window) starts next. It runs
        # in a daemon thread and is stopped in the finally below, so a cue
        # planned after the action ends is reported as not spoken.
        if vplan["cues"] and (action_cmd or record_cmd or launch_cmd):
            _ctx["voice_timeline"] = voice.CueTimeline(vplan["cues"]).start()
        if action_cmd:
            mark("action_start")
            _t_action = time.monotonic()
            timed_out = {"yes": False}
            timer = None
            if config.action_timeout_s:
                import threading
                from .remote_exec_utils import terminate_current_foreground

                def _expire():
                    timed_out["yes"] = terminate_current_foreground()
                    if timed_out["yes"]:
                        print(_c("31;1", f"[runner] ACTION TIMEOUT after "
                                         f"{config.action_timeout_s:.0f} s - SIGTERM sent "
                                         f"(the action lands on a signal)"))
                timer = threading.Timer(config.action_timeout_s, _expire)
                timer.daemon = True
                timer.start()
            try:
                completed = hooks.run_foreground(action_cmd, target, cwd=cwd, check=False)
            finally:
                if timer is not None:
                    timer.cancel()
            mark("action_end")
            rc = getattr(completed, "returncode", 0)
            if timed_out["yes"] and rc == 0:
                rc = 124   # a terminated action is never a finished trial
            if rc != 0:
                # The trial did NOT happen: the trajectory never ran (or died
                # part-way). Recorded here and turned into a `failed` status
                # below so it cannot be written up as a finished trial — a
                # non-flight that counts as coverage would satisfy a cell of
                # the grid forever and silently shrink the campaign.
                tail = (getattr(completed, "stdout", "") or "").strip().splitlines()
                detail = f"action returned rc={rc} — the action did not complete"
                if timed_out["yes"]:
                    detail += f" (action_timeout_s={config.action_timeout_s:.0f} expired)"
                if tail:
                    detail += "\n" + "\n".join(tail[-8:])
            trace_step("action", "action", ok=rc == 0, since=_t_action, rc=rc,
                 output=getattr(completed, "stdout", "") or "",
                 note=(f"action_timeout_s={config.action_timeout_s:.0f} expired"
                       if timed_out["yes"] else ""))
        elif not steps["action"] and (steps["launch"] or steps["record"]):
            # Manual window: the operator flies/works by hand while the stack
            # (and recorder, if enabled) stay up for a bounded time. With
            # nothing up there is nothing to hold open — skip straight through.
            print(f"[runner] action disabled - holding the stack for {hold_s:.0f}s")
            time.sleep(hold_s)
    except Exception as exc:  # noqa: BLE001
        detail = f"execution error: {exc}"
        trace_step("execution error", "error", ok=False, output=str(exc),
             note=type(exc).__name__)
    finally:
        timeline = _ctx.pop("voice_timeline", None)
        if timeline is not None:
            cue_results = timeline.stop()
            _ctx.setdefault("voice", {})["cues"] = cue_results
            unspoken = [r for r in cue_results if r.get("why")]
            if unspoken:
                print(f"[voice] {len(unspoken)} cue(s) not spoken: the action ended "
                      f"before their time")
        # Recorder first so the bag finalizes; then the stack.
        teardown: List[tuple] = [("record", "recorder")]
        if session is None:
            teardown += ([(f"aux:{n}", "launch") for n, _ in reversed(aux_cmds)]
                         + [("launch_stack", "launch")])
        else:
            # Hand the stack to the session so the next trial reuses it and
            # run_plan can tear it down once, at the end.
            session.setdefault("procs", {}).update(
                {k: v for k, v in procs.items() if k != "record"})
            session["target"] = target
            session["aux_names"] = [n for n, _ in aux_cmds] or session.get("aux_names", [])
        _t_teardown = time.monotonic()
        for name, kind in teardown:
            proc = procs.get(name)
            if proc is None:
                continue
            try:
                hooks.stop_process(proc, target, name=name, kind=kind)
            except Exception as exc:  # noqa: BLE001
                detail = f"{detail} | stop {name} failed: {exc}".strip(" |")
                trace_step(f"stop:{name}", "teardown", ok=False, output=str(exc))
            if name == "record":
                time.sleep(config.stop_grace_s)
                announce("record_end")
        print(f"[runner] teardown done in {time.monotonic() - _t_teardown:.1f} s")
        mark("teardown_done")
        sys.stdout = _saved_stdout
        if log_file is not None:
            print(f"[runner] run log: {log_path}")
            log_file.close()
            # The verify steps, the probes and the metadata follow, and their
            # output belongs in the same run log (2026-10-02: E2's per-agent
            # AGENT lines were only on the console). Reopened for appending;
            # the public run_trial() restores stdout on every return path.
            try:
                post_log = log_path.open("a", encoding="utf-8")
                _ctx["post_log"] = (_saved_stdout, post_log)
                sys.stdout = _LogTee(_saved_stdout, post_log)
            except OSError:
                pass

    if not steps["record"]:
        # No recorder was asked for, so no bag and NO metadata: an unrecorded
        # trial must never enter the coverage index. Useful for stack shakedowns
        # and manual sessions, honest about what it is.
        detail = f"{detail} | unrecorded (record step disabled) - not coverage".strip(" |")
        return TrialResult(trial, None, "ran", detail, commands)

    # An externally recorded bag is linked back into the repo before anything
    # else touches it. Everything downstream then works on the repo path exactly
    # as it always has: bag_has_data, the verify steps, trial_metadata.json, the
    # slim copy (which must stay in the repo, since fetch_slim_bags.sh pulls
    # experiments/*/bags_slim/) and trial_index's coverage glob. The link is
    # created AFTER recording because rosbag2 refuses an output path that
    # already exists.
    if external_record:
        link = bags_dir / bag_dir.name
        try:
            hooks.run_foreground(["mkdir", "-p", str(bags_dir)], target, cwd=cwd, check=False)
            completed = hooks.run_foreground(["ln", "-sfn", str(bag_dir), str(link)],
                                             target, cwd=cwd, check=False)
            if getattr(completed, "returncode", 0) == 0:
                print(f"[runner] linked {link} -> {bag_dir}")
                bag_dir = link
            else:
                print(f"[runner] WARNING could not link the external bag into the repo; "
                      f"the trial is safe at {bag_dir} but will not be counted by "
                      f"trial_index until it is offloaded or linked by hand")
        except Exception as exc:  # noqa: BLE001 - never lose a finished trial
            print(f"[runner] WARNING linking the external bag failed ({exc}); "
                  f"the bag itself is intact at {bag_dir}")

    # Did the recorder actually produce a bag, on the machine that recorded it?
    # Asked before the metadata is written, because writing trial_metadata.json
    # is what turns a directory into a trial the index will count. An empty
    # directory that claims to be a finished trial is worse than a loud failure:
    # it silently satisfies a cell of the coverage grid forever.
    try:
        from .remote_exec_utils import bag_has_data, write_trial_file
        recorded, why = bag_has_data(target, bag_dir)
    except Exception as exc:  # noqa: BLE001
        recorded, why = False, f"could not verify the bag ({exc})"
        write_trial_file = None  # type: ignore[assignment]

    if not recorded:
        detail = f"{detail} | no bag recorded: {why}".strip(" |") if detail else \
            f"no bag recorded: {why}"
        trace_step("bag check", "record", ok=False, output=str(why),
             note="the recorder left no data, so no trial_metadata.json was written")
        print(f"[runner] TRIAL FAILED - {detail}")
        print("[runner] no trial_metadata.json written: an empty bag must never "
              "count as coverage")
        return TrialResult(trial, bag_dir, "failed", detail, commands)

    # Did the trial actually HAPPEN? Asked of the bag, not of an exit code.
    # SKIPPED for a scratch flight: the verify steps check the campaign's
    # contract ("did the trial the campaign wanted happen"), and a scratch
    # flight answers the operator's ad-hoc question instead. It can never count
    # as coverage anyway, so there is no cell for a false pass to poison.
    all_verify = list(config.verify_steps) + list(extra_verify or [])
    if scratch_label is not None and all_verify:
        print("[runner] verify steps SKIPPED - scratch recordings answer ad-hoc "
              "questions, not campaign cells")
        all_verify = []
    _t_verify = time.monotonic()
    for step in all_verify:
        cmd = templated_command(step["command"], trial, f"verify step {step['name']}",
                                bag=str(bag_dir), **arg_ph)
        print(f"[verify] {step['name']}")
        _t_step = time.monotonic()
        completed = hooks.run_foreground(cmd, target, cwd=cwd, check=False)
        rc = getattr(completed, "returncode", 0)
        print(f"[verify] {step['name']} rc={rc} in {time.monotonic() - _t_step:.1f} s")
        trace_step(f"verify:{step['name']}", "verify", ok=rc == 0, since=_t_step, rc=rc,
                   output=getattr(completed, "stdout", "") or "")
        if rc != 0:
            tail = (getattr(completed, "stdout", "") or "").strip().splitlines()
            # "does not count", not "did not happen" (2026-11-04): a verify step runs on a
            # recorded bag after an action that returned 0, so the robot usually DID fly (the
            # Flow-only square of that day landed 0.67 m off and was stamped "did not happen").
            # The words "verify step" stay first: trial_decisions.abort_is_verify_only reads them.
            reason = (f"verify step {step['name']!r} failed (rc={rc}) — the trial ran and was "
                      f"recorded, but it does not count")
            if tail:
                reason += "\n" + "\n".join(tail[-6:])
            detail = f"{detail} | {reason}".strip(" |") if detail else reason
            break

    if all_verify:
        print(f"[verify] all steps done in {time.monotonic() - _t_verify:.1f} s")
        mark("verify_done")
    metadata = trial_metadata(trial, factors, config, trial_id=bag_dir.name)
    if scratch_label is not None:
        # The explicit marker, so the bag stays honest wherever it ends up:
        # bags_scratch/ is excluded from the index by name, and this field is
        # the second lock (trial_index refuses scratch: true even inside bags/,
        # should anyone ever move one there by hand).
        metadata["scratch"] = True
        if scratch_label:
            metadata["scratch_label"] = scratch_label
    # Which disk the bytes actually went to. Recorded because an offloaded or
    # externally recorded bag is one an analysis may fail to open a month later,
    # and "where was it written" should be answerable from the bag rather than
    # from whoever was standing at the rig. Bookkeeping, not a coverage axis
    # (trial_index.NON_FACTOR_FIELDS lists it).
    metadata["bag_storage"] = f"external:{record_dir}" if external_record else "repo"
    # WHICH CODE FLEW IT. Probed on `target` — the rig's own checkout, which is
    # routinely behind the laptop's — and cached per host, so a 40-trial
    # campaign pays one ssh round-trip rather than 40. Never fatal: the trial
    # has already flown, and losing it to a git query would be absurd.
    stamp_code_version(metadata, target)
    # Experiment-declared provenance (the PX4 checkout, the Gazebo release):
    # facts about the apparatus that code_version's repo HEAD cannot carry.
    # A checked factor's realized value, measured from the bag (2026-10-06):
    # the gate measured the condition before recording, this measures what the
    # recording actually saw, so declared and realized can be compared later.
    all_probes = list(config.metadata_probes) + cs.factor_realized_probes(trial.values, factors,
                                                                            trial.named)
    if all_probes:
        probed = run_metadata_probes(all_probes, hooks, target, cwd,
                                     placeholders={**trial.values, "bag": str(bag_dir),
                                                   **arg_ph})
        metadata.update(probed)
        if probed:
            print("[runner] metadata probes: " + ", ".join(f"{k}={v}" for k, v in probed.items()))
    action_failed = (detail.startswith("action returned rc=")
                     or "verify step" in detail)
    if action_failed:
        # The trajectory never ran (or died part-way), so this is NOT a flight.
        # Written up as ABORTED rather than left without metadata: the index
        # excludes aborted trials from coverage, so it cannot satisfy a cell,
        # while the bag stays visible with its reason attached instead of
        # becoming an anonymous directory nobody can account for.
        metadata["aborted"] = True
        # The schema requires abort_reason whenever aborted is true; without it
        # the runner writes metadata its own contract rejects.
        metadata["abort_reason"] = detail or "action/verify failed (no detail captured)"
        if detail.startswith("action returned rc="):
            print("[runner] TRIAL FAILED - the action failed, so the trial did not happen as "
                  "planned; recorded as aborted, so it cannot count as coverage")
        else:
            print("[runner] TRIAL FAILED - a verify step rejected the recorded trial; recorded "
                  "as aborted, so it cannot count as coverage (trials reverify, after a rule fix)")
    if gate_result is not None:
        metadata["preflight"] = gate_result.as_metadata()
        if pre_launch_meta is not None:
            # The pre-launch checks of 2026-11-04, inside the same block (no new contract field).
            metadata["preflight"]["pre_launch"] = pre_launch_meta
    if calibration_results:
        metadata["factor_calibration"] = calibration_results
    if detail:
        metadata["notes"] = detail
    try:
        from .trial_metadata_schema import annotate
        # Against the contract of the root this config came from, not the working
        # directory's project (found 2026-11-09: a test root was checked against the
        # example project's contract).
        annotate(metadata, config.experiment, experiments_root=config.experiments_root)
    except Exception as exc:  # noqa: BLE001 - never lose a finished trial
        print(f"[runner] metadata validation unavailable ({exc}) - continuing")

    # Next to the bag — which for a remote trial means on the rig, not here.
    write_trial_file(target, bag_dir / "trial_metadata.json",
                     json.dumps(metadata, indent=2))
    _ctx["metadata"] = {"aborted": metadata.get("aborted"),
                        "abort_reason": metadata.get("abort_reason")}

    # rosout.txt beside the bag (experiments/AGENTS.md), on the host that holds it, and
    # before the slim copy, which carries it (2026-10-14). Never fatal.
    export_rosout(bag_dir, target, hooks, cwd)

    # Leave a slim, transferable copy (#94). Never fatal: the trial has flown.
    if slim:
        try:
            from .slim_bag_archive import archive_and_sync
            local_slim = archive_and_sync(bag_dir, target, project_root=cwd)
        except Exception as exc:  # noqa: BLE001
            local_slim = None
            print(f"[runner] slim archiving skipped ({exc})")
        # The trial card's stills and camera video (#138), from the local slim copy,
        # when the experiment declares a camera (config/trial_media.yaml). Never fatal.
        from .trial_media import make_after_trial
        make_after_trial(local_slim, bag_dir if target is None else None)

    status = "failed" if (action_failed or detail.startswith("execution error")) else "ran"
    return TrialResult(trial, bag_dir, status, detail, commands)


from .remote_exec_utils import package_module_command  # noqa: E402,F401  (re-exported)


def export_rosout(bag_dir: Path, target, hooks: "ProcessHooks", project_root: Path) -> None:
    """Write ``<bag>/rosout.txt`` from the bag's /rosout. Never raises: the trial has flown."""
    try:
        if target is None:
            from .export_rosout_text import export_rosout_text
            count = export_rosout_text(Path(bag_dir), Path(bag_dir) / "rosout.txt")
            print(f"[runner] rosout.txt: {count} /rosout messages")
            return
        root = Path(project_root or target.workspace)
        completed = hooks.run_foreground(
            package_module_command(root, "export_rosout_text", [str(bag_dir)]),
            target, cwd=root, check=False)
        rc = getattr(completed, "returncode", 0)
        print("[runner] rosout.txt written on the rig" if rc == 0
              else f"[runner] rosout export failed on the rig (rc={rc}) - continuing")
    except Exception as exc:  # noqa: BLE001 - a finished trial outranks a text export
        print(f"[runner] rosout export skipped ({exc})")


#: The attempt ledger's file name, under experiments/<name>/runs/.
ATTEMPT_LEDGER_NAME = "attempts.jsonl"

#: Output lines a failing step keeps in the step trace (2026-10-20).
TRACE_TAIL_LINES = 12


def _gate_note(gate: Optional[Dict[str, Any]]) -> str:
    """The gate's verdict, mode and failed checks in one line, for the step trace."""
    if not isinstance(gate, dict):
        return ""
    from .trial_records import _failed_names
    note = f"{gate.get('verdict') or '?'} ({gate.get('mode') or 'binding'})"
    failed = _failed_names(gate.get("failed_checks"))
    if failed:
        note += ": " + "; ".join(failed)
    return note


def failure_report(record: Dict[str, Any]) -> Optional[str]:
    """The failure report of one attempt, as markdown, or None for a trial that ran.

    Written for whoever debugs the attempt next, a person or a coding agent: the
    first failing step with its evidence, the step trace, and where the rest of
    the evidence is. It ends with empty findings headings in the shape of
    ASPIRE's findings file (Lu et al. 2026), so a diagnosis is written down next
    to the attempt it explains. Pure, so tests can assert its content.
    """
    status = record.get("status")
    if status == "ran" and not record.get("aborted"):
        return None
    label = record.get("trial_label") or "unlabeled attempt"
    trace = [s for s in record.get("trace") or [] if isinstance(s, dict)]
    failing = [s for s in trace if not s.get("ok")]
    first = next((s for s in failing if s.get("blocking", True)), None)
    values = ", ".join(f"{k}={v}" for k, v in sorted((record.get("values") or {}).items()))
    lines = [
        f"# Failure report: {label}",
        "",
        f"Written by the trial runner at {record.get('ts_end')}. Read this first, then the "
        f"evidence it points to. Fill in the findings at the end once the cause is known.",
        "",
        f"- **Status:** {status}" + (" (aborted)" if record.get("aborted") else ""),
        f"- **Experiment:** {record.get('experiment')}, campaign {record.get('campaign')}, "
        f"host {record.get('host')}",
        f"- **Condition:** {values or 'all defaults'}, repetition {record.get('repetition')}",
    ]
    if record.get("abort_reason"):
        lines.append(f"- **Abort reason:** {record['abort_reason'].splitlines()[0]}")
    lines += ["", "## First failing step", ""]
    if first is None:
        lines.append("No step in the trace failed. The runner's detail says:")
        lines += ["", "```text", (record.get("detail") or "(no detail)").strip(), "```"]
    else:
        where = f"at t = {first.get('t_s')} s"
        if "duration_s" in first:
            where += f", after {first['duration_s']} s"
        lines.append(f"**{first.get('step')}** ({first.get('kind')}), {where}"
                     + (f", rc = {first['rc']}" if "rc" in first else "") + ".")
        if first.get("note"):
            lines += ["", first["note"]]
        if first.get("output_tail"):
            lines += ["", "```text", *first["output_tail"], "```"]
    others = [s for s in failing if s is not first]
    if others:
        lines += ["", "Other failing steps:"]
        lines += [f"- {s.get('step')} ({s.get('kind')})"
                  + (", did not block" if s.get("blocking") is False else "")
                  + (f": {s['note']}" if s.get("note") else "") for s in others]
    gate = record.get("gate")
    if isinstance(gate, dict):
        lines += ["", "## T-0 gate", "", f"- {_gate_note(gate)}"]
    if trace:
        lines += ["", "## Step trace", "", "| t (s) | step | kind | rc | result |",
                  "|---|---|---|---|---|"]
        for s in trace:
            result = "ok" if s.get("ok") else ("failed, did not block"
                                               if s.get("blocking") is False else "FAILED")
            lines.append(f"| {s.get('t_s')} | {s.get('step')} | {s.get('kind')} | "
                         f"{s.get('rc', '')} | {result} |")
    bag = record.get("bag_dir")
    lines += ["", "## Evidence", "",
              f"- Run log: `runs/{label}.log`",
              f"- Ledger line: `runs/{ATTEMPT_LEDGER_NAME}`, ts_start {record.get('ts_start')}"]
    if bag:
        lines += [f"- Bag: `{bag}`, with `trial_metadata.json` and `rosout.txt` beside it "
                  f"(on the host that recorded it)"]
    elif status == "blocked":
        lines += [f"- Gate log: `runs/preflight_gate.jsonl`, and `bags/{label}/gate_refusal.json` "
                  f"if the aux launches left a directory"]
    lines += ["", "## Findings", "",
              "- **Symptom:**",
              "- **Root cause:**",
              "- **Fix:**",
              "- **Verified by:**",
              "- **Lesson to keep** (a gate check, a verify step, a lint rule or a note):",
              ""]
    return "\n".join(lines)


def write_failure_report(record: Dict[str, Any], runs_dir: Path) -> Optional[Path]:
    """Write ``runs/<label>.failure.md`` for an attempt that did not run. Never raises."""
    try:
        text = failure_report(record)
        if text is None:
            return None
        label = record.get("trial_label") or f"attempt_{record.get('ts_start', '')}"
        path = Path(runs_dir) / f"{label}.failure.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"[runner] failure report: {path}")
        return path
    except Exception as exc:  # noqa: BLE001 - a report never outranks a trial
        print(f"[runner] failure report not written ({exc}) - continuing")
        return None


def run_trial(trial: cs.TrialSpec, factors: Dict[str, cs.Factor], config: RunnerConfig,
              bags_dir: Path, *, campaign_name: Optional[str] = None,
              session_id: Optional[str] = None, ledger: bool = True,
              **kwargs: Any) -> TrialResult:
    """One trial, end to end (see ``_run_trial`` for the order), plus its ledger line.

    The ledger line (2026-10-01) is written for every EXECUTED attempt,
    blocked, failed or ran, to ``<experiment>/runs/attempts.jsonl``: label,
    factor values, status, aborted and its reason, the gate's verdict and mode,
    and the stage timings. A dry run writes nothing. Writing it is never fatal.

    An executed attempt holds the run marker (run_marker.py, 2026-10-22) until
    its ledger line is written, so an uploader waits for the complete trial.
    """
    if kwargs.get("dry_run", True):
        return _run_trial_logged(trial, factors, config, bags_dir, campaign_name=campaign_name,
                                 session_id=session_id, ledger=ledger, **kwargs)
    with run_marker.running(Path(bags_dir).resolve().parents[1], experiment=trial.experiment,
                            campaign=campaign_name, label=trial.describe()):
        return _run_trial_logged(trial, factors, config, bags_dir, campaign_name=campaign_name,
                                 session_id=session_id, ledger=ledger, **kwargs)


def _run_trial_logged(trial: cs.TrialSpec, factors: Dict[str, cs.Factor], config: RunnerConfig,
                      bags_dir: Path, *, campaign_name: Optional[str] = None,
                      session_id: Optional[str] = None, ledger: bool = True,
                      **kwargs: Any) -> TrialResult:
    """run_trial() without the run marker."""
    ctx: Dict[str, Any] = {}
    started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    try:
        result = _run_trial(trial, factors, config, bags_dir, _ctx=ctx, **kwargs)
    except KeyboardInterrupt:
        # INTERRUPTED (2026-10-06, the first lighting_sim campaign): a SIGINT
        # mid-trial tore the stack down (the finally inside _run_trial) and
        # then left a recorded bag with no trial_metadata.json and no ledger
        # line, invisible to the index rather than excluded with a reason. The
        # trial is now finalized as ABORTED before the interrupt propagates.
        if ledger and not kwargs.get("dry_run", True):
            _finalize_interrupted(trial, factors, config, bags_dir, ctx, started=started,
                                  campaign_name=campaign_name, session_id=session_id,
                                  scratch=kwargs.get("scratch_label") is not None)
        raise
    finally:
        if "post_log" in ctx:
            saved, post_log = ctx.pop("post_log")
            sys.stdout = saved
            try:
                post_log.close()
            except OSError:
                pass
    # A dry run writes nothing, including a dry run REFUSED before its preview
    # (a calibration campaign without --scratch logged one on 2026-10-06).
    if result.status == "dry-run" or not ledger or kwargs.get("dry_run", True):
        return result
    record = attempt_record(trial, factors, config, result, ctx, started=started,
                            campaign_name=campaign_name, session_id=session_id,
                            scratch=kwargs.get("scratch_label") is not None)
    result.ledger = record
    try:
        path = bags_dir.parent / "runs" / ATTEMPT_LEDGER_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001 - a ledger line never outranks a trial
        print(f"[runner] attempt ledger not written ({exc}) - continuing")
    write_failure_report(record, bags_dir.parent / "runs")
    return result


def _note_gate_refusal(bag_dir: Path, gate: Optional[Dict[str, Any]], detail: str) -> None:
    """Explain a bag directory that a NO-GO left behind (never fatal).

    Aux launches start BEFORE the gate (the gate checks what they bring up),
    and some write sidecars into the trial's directory (sample_process_resources
    writes process_resources.csv), so a blocked attempt can leave a directory
    with sidecars and no recording. Found 2026-10-06 on the lighting mismatch:
    the directory looked like a trial the gate had let through. This file says
    what it is. No recording, no trial_metadata.json: the index never reads it.
    """
    try:
        if not bag_dir.is_dir():
            return
        note = {"status": "blocked", "recorded": False,
                "why": "the T-0 gate was NO-GO; this directory holds only sidecars of aux "
                       "launches started before the gate. Nothing was recorded or flown.",
                "detail": detail[:2000],
                "gate": {k: (gate or {}).get(k) for k in
                         ("verdict", "mode", "failed_checks", "factor_checks", "checked_at")}}
        (bag_dir / "gate_refusal.json").write_text(json.dumps(note, indent=2) + "\n",
                                                   encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] gate_refusal.json not written ({exc})")


def _finalize_interrupted(trial: cs.TrialSpec, factors: Dict[str, cs.Factor],
                          config: RunnerConfig, bags_dir: Path, ctx: Dict[str, Any], *,
                          started: str, campaign_name: Optional[str],
                          session_id: Optional[str], scratch: bool,
                          reason: str = "", recovered: Optional[Dict[str, Any]] = None
                          ) -> Optional[Path]:
    """Write ABORTED metadata into an interrupted trial's bag, and its ledger line.

    Only when the bag directory holds a recording and no trial_metadata.json:
    a capture-truth file that exists is never touched. Returns the metadata
    path written, or None. Never raises.
    """
    try:
        bag_dir = ctx.get("bag_dir") or (bags_dir / ctx["label"] if ctx.get("label") else None)
        marks = ctx.get("marks") or {}
        last = max(marks, key=marks.get) if marks else "before any stage"
        reason = reason or (f"interrupted by the operator (SIGINT) after stage {last!r}: "
                            f"the trial did not finish, so it cannot count as coverage")
        written = None
        if bag_dir is not None and Path(bag_dir).is_dir() \
                and any(Path(bag_dir).glob("*.mcap")) \
                and not (Path(bag_dir) / "trial_metadata.json").exists():
            metadata = trial_metadata(trial, factors, config, trial_id=Path(bag_dir).name)
            metadata["aborted"] = True
            metadata["abort_reason"] = reason
            if ctx.get("gate"):
                metadata["preflight"] = ctx["gate"]
            if scratch:
                metadata["scratch"] = True
            if recovered:
                metadata["_recovered"] = recovered
            path = Path(bag_dir) / "trial_metadata.json"
            path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            written = path
            ctx["metadata"] = {"aborted": True, "abort_reason": reason}
            print(f"[runner] INTERRUPTED - {path} written as aborted: {reason}")
        result = TrialResult(trial, Path(bag_dir) if bag_dir else None, "interrupted", reason, [])
        record = attempt_record(trial, factors, config, result, ctx, started=started,
                                campaign_name=campaign_name, session_id=session_id,
                                scratch=scratch)
        if recovered:
            record["_recovered"] = recovered
        path = bags_dir.parent / "runs" / ATTEMPT_LEDGER_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        write_failure_report(record, path.parent)
        return written
    except Exception as exc:  # noqa: BLE001 - never mask the interrupt
        print(f"[runner] interrupted trial not finalized ({exc})")
        return None


def attempt_record(trial: cs.TrialSpec, factors: Dict[str, cs.Factor],
                   config: RunnerConfig, result: TrialResult, ctx: Dict[str, Any], *,
                   started: str, campaign_name: Optional[str],
                   session_id: Optional[str], scratch: bool) -> Dict[str, Any]:
    """The ledger line of one attempt (pure, so tests can assert its shape)."""
    gate = ctx.get("gate") or None
    meta = ctx.get("metadata")
    wall = ctx.get("wall") or {}
    window = ([wall["action_start"], wall["action_end"]]
              if "action_start" in wall and "action_end" in wall else None)
    return {
        "ts_start": started,
        "ts_end": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "experiment": config.experiment,
        "campaign": "scratch" if scratch else campaign_name,
        "session": session_id,
        "trial_label": ctx.get("label"),
        "bag_dir": str(result.bag_dir) if result.bag_dir is not None else None,
        "scratch": scratch,
        "values": dict(trial.values),
        "repetition": trial.repetition,
        "order": dict(trial.order) if trial.order else None,
        "host": trial.host(factors),
        "status": result.status,
        "metadata_written": meta is not None,
        "aborted": (meta or {}).get("aborted"),
        "abort_reason": (meta or {}).get("abort_reason"),
        "detail": (result.detail or "")[:2000],
        "gate": ({k: gate.get(k) for k in ("mode", "verdict", "status", "allowed",
                                           "overridden", "override_reason",
                                           "failed_checks", "duration_s", "n_checks",
                                           "factor_checks")}
                 if isinstance(gate, dict) else None),
        "timings_s": dict(ctx.get("marks") or {}),
        # Which operator and setup steps this attempt got (2026-11-04): first or repeat.
        "step_set": ctx.get("step_set"),
        # What ran, how long, how it ended, and a failing step's output tail
        # (2026-10-20). The failure report is written from this.
        "trace": list(ctx.get("trace") or []),
        "action_window": window,
        # What the laptop was asked to say, and when (2026-10-13). "sent" means
        # a datagram left the runner, not that anyone heard it.
        "voice": ctx.get("voice"),
    }


def gate_factor_problems(campaign: "cs.Campaign", config: RunnerConfig,
                         override_reason: str = "") -> List[str]:
    """Why a campaign naming a `gate` factor may not run, if it may not."""
    named = [n for n in cs.gate_factors(campaign.factors)
             if n in campaign.sweep or n in campaign.fixed]
    problems: List[str] = []
    if not named:
        return problems
    if not config.simulation:
        problems.append(f"campaign {campaign.name!r} names the gate factor(s) "
                        f"{', '.join(named)}, which only an experiment whose runner.yaml "
                        f"declares `simulation: true` may run: a shadow or disabled gate "
                        f"lets a trial proceed past a NO-GO")
    if override_reason:
        problems.append("--gate-override cannot be combined with a gate factor: the "
                        "factor already decides, per trial, whether a NO-GO blocks")
    return problems


def run_plan(plan: cp.Plan, config: RunnerConfig, bags_dir: Path, *,
             gate=None, dry_run: bool = True, limit: Optional[int] = None,
             hooks: Optional[ProcessHooks] = None, slim: bool = True,
             stop_on_block: Optional[bool] = None,
             steps: Optional[Dict[str, bool]] = None, hold_s: float = 60.0,
             keep_stack: bool = False, extra_verify: Optional[List[Dict[str, Any]]] = None,
             quiet_children: bool = False,
             pause_before_action: bool = True,
             confirm_steps: Optional[Callable[[cs.TrialSpec, List[Dict[str, Any]]], bool]] = None,
             confirm_pose: Optional[Callable[[cs.TrialSpec], bool]] = None,
             should_continue: Optional[Callable[[], bool]] = None,
             on_result: Optional[Callable[[TrialResult], None]] = None,
             ) -> List[TrialResult]:
    """Run only what the plan says is still needed.

    Stops on a blocked gate by default: if the stack cannot pass pre-flight, the
    next trial will not pass either, and grinding through a whole campaign
    producing nothing is worse than stopping and saying why.

    ``should_continue`` is checked BETWEEN trials, never mid-trial: aborting a
    campaign must not interrupt a flight in progress or leave a bag unfinalized.
    Stopping the aircraft itself is the e-stop's job, not this one.
    ``on_result`` reports each trial as it finishes, so a caller can show
    progress instead of waiting for the whole batch.

    The ORDER is ``campaign_plan.execution_order``: the planner's own order
    unless the campaign declares ``order`` (randomized complete blocks with a
    recorded seed, 2026-10-01). ``stop_on_block=None`` (the default) means
    "stop", except for a campaign that names a `gate` factor, where a blocked
    attempt is an expected outcome rather than a broken rig.
    """
    factors = plan.campaign.factors
    results: List[TrialResult] = []
    # The metadata contract, refused before any trial starts (2026-10-02). The
    # CLI refuses earlier, in main(); this covers every other caller of
    # run_plan (the web UI), dry runs included, because a dry run of such a
    # campaign shows a plan that merges levels it claims to sweep.
    source = getattr(plan.campaign, "source", None)
    refusal = cs.contract_refusal(plan.campaign,
                                  Path(source).resolve().parents[2] if source else None)
    if refusal:
        raise RunnerError(refusal)
    if stop_on_block is None:
        gate_named = any(n in plan.campaign.sweep or n in plan.campaign.fixed
                         for n in cs.gate_factors(factors))
        stop_on_block = not gate_named
        if gate_named:
            print("[runner] the campaign names a gate factor: a blocked attempt is an "
                  "outcome, so the campaign continues past it")
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # One shared stack for the whole batch when asked (never in a dry run —
    # there is nothing running to share). The finally below is what guarantees
    # the rig does not stay up after an abort, a block or an exception.
    session: Optional[Dict[str, Any]] = {} if (keep_stack and not dry_run) else None
    # The run marker spans the whole campaign, the gaps between trials included
    # (run_marker.py): an uploader must not start between two flights.
    marker = contextlib.nullcontext() if dry_run else run_marker.running(
        Path(bags_dir).resolve().parents[1], experiment=plan.campaign.experiment,
        campaign=plan.campaign.name)
    with marker:
        return _run_plan_items(plan, config, bags_dir, factors, results, session, session_id,
                               limit, should_continue, gate, dry_run, hooks, slim, steps, hold_s,
                               extra_verify, quiet_children, pause_before_action, confirm_steps,
                               confirm_pose, on_result, stop_on_block)


def _run_plan_items(plan, config, bags_dir, factors, results, session, session_id, limit,
                    should_continue, gate, dry_run, hooks, slim, steps, hold_s, extra_verify,
                    quiet_children, pause_before_action, confirm_steps, confirm_pose, on_result,
                    stop_on_block):
    """run_plan()'s loop over the attempts, inside the run marker."""
    try:
        for item in cp.execution_order(plan):
            cell, rep = item.cell, item.repetition
            if limit is not None and len(results) >= limit:
                return results
            if should_continue is not None and not should_continue():
                return results
            spec = cs.TrialSpec(plan.campaign.experiment, dict(cell.values),
                                list(cell.non_defaults), repetition=rep,
                                order=item.order,
                                named=sorted(set(plan.campaign.sweep)
                                             | set(plan.campaign.fixed)),
                                factor_check_mode=plan.campaign.factor_checks)
            if item.order:
                pos = item.order
                print(f"[runner] attempt {pos['position']}/{pos['of']}"
                      + (f" (block {pos['block']}, seed {pos['seed']})"
                         if pos.get("block") else f" (seed {pos['seed']})"))
            # A REPEAT trial follows one of this invocation that was not blocked
            # (2026-11-04): after a blocked one (a NO-GO, unconfirmed steps) the rig may
            # need the whole procedure again, so it gets the first-trial steps.
            repeat = bool(results) and results[-1].status != "blocked"
            result = run_trial(spec, factors, config, bags_dir,
                               campaign_name=plan.campaign.name, session_id=session_id,
                               gate=gate, dry_run=dry_run, hooks=hooks, slim=slim,
                               steps=steps, hold_s=hold_s, session=session,
                               extra_verify=extra_verify,
                               quiet_children=quiet_children,
                               pause_before_action=pause_before_action,
                               operator_steps=plan.campaign.operator_steps,
                               confirm_steps=confirm_steps,
                               confirm_pose=confirm_pose,
                               voice_cues=getattr(plan.campaign, "voice_cues", None),
                               voice_announce=getattr(plan.campaign, "voice_announce", None),
                               repeat=repeat,
                               campaign_setup_steps=getattr(plan.campaign, "setup_steps", None),
                               robot_reset=getattr(plan.campaign, "robot_reset", True))
            results.append(result)
            if on_result is not None:
                on_result(result)
            if result.status == "blocked" and stop_on_block:
                return results
        return results
    finally:
        close_session(session, hooks)


def close_session(session: Optional[Dict[str, Any]],
                  hooks: Optional[ProcessHooks] = None) -> None:
    """Stop a continuous session's shared stack (aux launches, then the stack).

    Separate and idempotent so a caller that owns a session across several
    run_plan calls can close it explicitly, and so a partially-started session
    still tears down.
    """
    if not session or not session.get("procs"):
        return
    hooks = hooks or ProcessHooks.default()
    target = session.get("target")
    print("[runner] closing continuous session - stopping the shared stack")
    names = [f"aux:{n}" for n in reversed(session.get("aux_names", []))] + ["launch_stack"]
    for name in names:
        proc = session["procs"].get(name)
        if proc is None:
            continue
        try:
            hooks.stop_process(proc, target, name=name, kind="launch")
        except Exception as exc:  # noqa: BLE001 - report, never mask the result
            print(f"[runner] stop {name} failed: {exc}")
    session["procs"] = {}


def run_scratch(campaign: "cs.Campaign", config: RunnerConfig, bags_dir: Path, *,
                gate=None, dry_run: bool = True, action: str = "",
                label: str = "", hooks: Optional[ProcessHooks] = None,
                slim: bool = False, quiet_children: bool = False,
                pause_before_action: bool = True) -> TrialResult:
    """One ad-hoc diagnostic flight through the campaign's own machinery.

    2026-08-12, at the real rig: diagnostic flights that were not campaign
    trials (a battery-vs-airframe discriminator hover, a fixes-confirmation
    hover) were flown through hand-built tmux panes because the framework had
    no scratch mode. The costs were all real: a drone that silently never
    armed (the manual path skips the T-0 gate, whose drone_armable check
    exists exactly for the firmware latch that caused it), a recorder
    pre-typed with an already-existing bag name that captured 53 s of a landed
    drone instead of the flight, and an operator juggling five panes
    mid-flight.

    So a scratch flight is run_trial with the campaign's whole apparatus
    (stack, aux launches, T-0 gate, operator steps, recorder) and three
    deliberate differences:

    * the bag goes to ``<experiment>/bags_scratch/<stamp>_scratch[_<label>]``,
      a directory the coverage index and the campaign expander NEVER read, and
      its metadata carries ``scratch: true`` as a second lock;
    * the verify steps are skipped, because a scratch flight answers the
      operator's ad-hoc question rather than the campaign's contract;
    * no slim copy by default (``slim=True`` to keep one, which lands in
      ``bags_scratch_slim/`` and stays equally invisible to coverage).

    The trial's shape is the campaign's own: factor defaults, its ``fixed``
    block, and the first level of anything swept. Pin another point with
    ``--set``. ``action`` replaces runner.yaml's action command verbatim
    (``{factor}`` placeholders still fill from the trial's values).
    """
    spec = next(campaign.combinations())
    if action:
        config.action = action.strip()
    safe_label = _re.sub(r"[^A-Za-z0-9_-]+", "-", (label or "").strip()).strip("-")
    print(_c("36;1", f"[runner] SCRATCH FLIGHT of {campaign.name}"
                     + (f" [{safe_label}]" if safe_label else "")))
    print(f"[runner]   shape: {spec.describe()}")
    print("[runner]   recorded for the operator's own question. It can never "
          "count as coverage.")
    result = run_trial(spec, campaign.factors, config, bags_dir, gate=gate,
                       dry_run=dry_run, hooks=hooks, slim=slim,
                       quiet_children=quiet_children,
                       pause_before_action=pause_before_action,
                       operator_steps=campaign.operator_steps,
                       campaign_setup_steps=campaign.setup_steps,
                       robot_reset=getattr(campaign, "robot_reset", True),
                       scratch_label=safe_label,
                       voice_cues=getattr(campaign, "voice_cues", None),
                       voice_announce=getattr(campaign, "voice_announce", None))

    colour = {"blocked": "31;1", "failed": "31", "dry-run": "33"}.get(result.status, "32")
    print(f"\n[{_c(colour, result.status)}] scratch recording  {result.trial.describe()}")
    if result.detail:
        print(f"    {result.detail}")
    for cmd in result.commands:
        print(f"    $ {shlex.join(cmd)}")

    # The closing tally says SCRATCH in as many words: a session mixing real
    # trials and scratch flights must never let one read as the other.
    if result.status == "dry-run":
        tally = "SCRATCH dry run - nothing was launched, nothing recorded"
    elif result.status == "blocked":
        tally = "1 scratch recording BLOCKED by the T-0 gate - nothing recorded, the action did not run"
    elif result.bag_dir is not None:
        where = f"{config.experiment}/{bags_dir.name}/{result.bag_dir.name}"
        verb = "saved to" if result.status == "ran" else "FAILED - bag at"
        tally = f"1 scratch recording {verb} {where} - counts for nothing (never coverage)"
    else:
        tally = f"1 scratch recording {result.status} - nothing recorded"
    print("\n" + _c("31;1" if result.status in ("blocked", "failed") else "32;1", tally))
    return result


def build_gate(campaign: "cs.Campaign", experiments_root: Path, *,
               execute: bool, override_reason: str = "") -> Optional[PreflightGate]:
    """The T-0 gate for a CLI run, from the campaign's own checklist.

    2026-08-11: until today the CLI never constructed a gate at all — run_plan
    was called with gate=None, so `--execute` launched, recorded and "verified"
    trials with the checklist never run. The web UI gated; the CLI did not. It
    was found the expensive way: a trial recorded 2x120 s against a Spot whose
    proxy was unreachable the whole time (the checklist's HARD host_reachable
    and topic_rate probes would have refused it before the recorder started).

    Dry runs stay ungated (nothing is launched, so there is nothing to probe).
    A missing checklist file disables the gate LOUDLY rather than silently —
    lint L8 keeps campaign-declared checklists existing, but an experiment with
    no default checklist at all (the demos) still runs, and says so.
    """
    if not execute:
        return None
    rel = campaign.preflight_checklist or "config/preflight_checklist.json"
    checklist = experiments_root / campaign.experiment / rel
    if not checklist.is_file():
        print(f"[runner] ⚠ UNGATED: no gate checklist at "
              f"{campaign.experiment}/{rel} — every trial will run without a T-0 gate")
        return None
    gate = PreflightGate(checklist=checklist,
                         override=bool(override_reason),
                         override_reason=override_reason,
                         log_path=experiments_root / campaign.experiment / "runs"
                         / "preflight_gate.jsonl",
                         experiment=str(campaign.experiment),
                         campaign=getattr(campaign, "name", None))
    print(f"[runner] T-0 gate armed: {campaign.experiment}/{rel}"
          + (" (OVERRIDE SET — failures will not block)" if override_reason else ""))
    return gate


def _voice_preflight(campaign: "cs.Campaign", config: RunnerConfig, *, execute: bool) -> None:
    """Say where the cues go, and warn once when nobody is listening.

    A silent ping (nothing is spoken), at most 1 s, only for an executed run
    that has something to say. Never blocks the run and never touches the T-0
    gate: a missing voice is an inconvenience, not a reason to refuse a trial.
    """
    plan = voice_plan(getattr(campaign, "voice_cues", None),
                      getattr(campaign, "voice_announce", None),
                      simulation=config.simulation)
    if not voice_plan_lines(plan):
        return
    print(f"[runner] {voice_target_line()}")
    if execute and voice.enabled() and not voice.check(timeout_s=1.0):
        host, port = voice.target()
        print(_c("33;1", f"[runner] voice: no daemon answered at {host}:{port}. The cues are "
                         f"printed only. Start the operator-voice daemon on the laptop "
                         f"(scripts/AGENTS.md), or set {voice.ENV_HOST}"))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("campaign", type=Path)
    roots.add_root_arguments(ap)
    ap.add_argument("--set", action="append", default=[], metavar="name=a[,b]")
    ap.add_argument("--include-unverified", action="store_true")
    ap.add_argument("--limit", type=int, help="run at most N trials")
    ap.add_argument("--bags-root", metavar="repo|external|PATH",
                    help="where to WRITE the heavy bags: 'repo' (default, "
                         "experiments/<name>/bags), 'external' (storage.yaml's "
                         "external_dest for the recording host), or a path. "
                         "Overrides runner.yaml. A symlink is left in the repo, so "
                         "coverage, slimming and analyses are unaffected.")
    ap.add_argument("--execute", action="store_true",
                    help="actually run. Without this the runner only prints commands.")
    ap.add_argument("--scratch", action="store_true",
                    help="run ONE ad-hoc diagnostic trial (a scratch recording) through the campaign's "
                         "stack, T-0 gate and operator steps. Records to "
                         "experiments/<name>/bags_scratch/ (never counted as "
                         "coverage), skips the verify steps, and takes the "
                         "campaign's own defaults for its shape (--set still pins "
                         "factors, --action overrides the command).")
    ap.add_argument("--action", metavar="CMD",
                    help="(--scratch only) run this command instead of runner.yaml's "
                         "action. {factor} placeholders are filled as usual.")
    ap.add_argument("--label", metavar="WORD", default="",
                    help="(--scratch only) a word for the bag name, "
                         "<stamp>_scratch_<label>. The stamp and de-collision are "
                         "the runner's own, generated at run time - never pre-type "
                         "a bag name.")
    ap.add_argument("--slim", action="store_true",
                    help="(--scratch only) also leave a slim copy, in "
                         "bags_scratch_slim/. Campaign trials always slim; scratch "
                         "recordings default to not bothering.")
    ap.add_argument("--no-pause", action="store_true",
                    help="do not wait for Enter between the T-0 gate and the "
                         "recorder (the pause exists so the operator can pose "
                         "the rig - stand Spot, set the gripper - without "
                         "racing the dwell).")
    ap.add_argument("--quiet-stack", action="store_true",
                    help="child process output (stack, aux, recorder) goes only to "
                         "the run log; the terminal keeps the campaign narrative — "
                         "gate verdicts, trial status, dwell countdown, verify "
                         "results. Made for the tmux cockpit, whose log pane "
                         "already follows the full run log.")
    ap.add_argument("--gate-override", metavar="REASON",
                    help="proceed past a FAILED T-0 gate. The reason and the "
                         "failed checks are stamped into trial_metadata.json forever "
                         "(#87's escape hatch — visible, never silent).")
    ap.add_argument("--no-voice", action="store_true",
                    help="print the spoken cues but send none (sets TRIALS_VOICE=off for "
                         "this run and its child processes). Cues go to the laptop's voice "
                         "daemon at TRIALS_VOICE_HOST:TRIALS_VOICE_PORT, default "
                         "127.0.0.1:47900. See the framework guide.")
    ap.add_argument("--keep-going", action="store_true",
                    help="continue the campaign after a blocked trial (the default stops, "
                         "because a stack that fails the gate usually fails it again). "
                         "Automatic for a campaign that names a `gate` factor.")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)
    if not args.scratch and (args.action or args.label or args.slim):
        ap.error("--action, --label and --slim are --scratch options")

    try:
        campaign = cs.load_campaign(args.campaign, args.experiments_root, args.set)
        config = RunnerConfig.load(campaign.experiment, args.experiments_root)
    except (cs.SpecError, RunnerError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    contract = cs.contract_refusal(campaign, args.experiments_root)
    if contract:
        print(f"ERROR: {contract}", file=sys.stderr)
        return 2
    refused = gate_factor_problems(campaign, config, args.gate_override or "")
    if refused:
        for problem in refused:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 2
    config.bags_root_override = args.bags_root
    if args.no_voice:
        import os
        os.environ[voice.ENV_SWITCH] = "off"
    _voice_preflight(campaign, config, execute=args.execute)

    if args.scratch:
        # No plan, no coverage arithmetic: a scratch flight neither needs a cell
        # nor fills one. The gate is the same T-0 gate a real trial gets.
        if not args.execute:
            print("=== DRY RUN — nothing will be launched or recorded ===")
        gate = build_gate(campaign, args.experiments_root, execute=args.execute,
                          override_reason=args.gate_override or "")
        if gate is not None:
            gate.campaign = "scratch"
        result = run_scratch(
            campaign, config,
            args.experiments_root / campaign.experiment / "bags_scratch",
            gate=gate, dry_run=not args.execute,
            action=args.action or "", label=args.label, slim=args.slim,
            quiet_children=args.quiet_stack,
            pause_before_action=not args.no_pause)
        return 1 if result.status in ("blocked", "failed") else 0

    trials = ti.load_trials(args.experiments_root, campaign.experiment)
    plan = cp.build_plan(campaign, trials, args.include_unverified)
    bags_dir = args.experiments_root / campaign.experiment / "bags"

    print(cp.render(plan))
    print("")
    if not args.execute:
        print("=== DRY RUN — nothing will be launched or recorded ===")

    gate = build_gate(campaign, args.experiments_root, execute=args.execute,
                      override_reason=args.gate_override or "")
    if not args.execute:
        # Dry runs are ungated, so the per-trial line cannot name the pre-launch checks: say
        # once what every executed trial will run before its stack (2026-11-04).
        rel = campaign.preflight_checklist or "config/preflight_checklist.json"
        checklist = Path(args.experiments_root) / campaign.experiment / rel
        if checklist.is_file():
            names = PreflightGate(checklist=checklist).stage_checks("pre_launch")
            if names:
                print("[dry-run] pre-launch checks of every executed trial (from "
                      f"{campaign.experiment}/{rel}, after runner.yaml's setup steps, before the "
                      f"robot resets and the stack): {', '.join(names)}")
    if campaign.order:
        print(cp.render_schedule(plan, limit=args.limit or 200))
        print("")
    results = run_plan(plan, config, bags_dir, gate=gate,
                       dry_run=not args.execute, limit=args.limit,
                       stop_on_block=(False if args.keep_going else None),
                       extra_verify=campaign.verify_steps,
                       quiet_children=args.quiet_stack,
                       pause_before_action=not args.no_pause)
    if not results:
        print("Nothing to run — the campaign is already covered.")
        return 0

    for result in results:
        colour = {"blocked": "31;1", "failed": "31"}.get(result.status, "32")
        print(f"\n[{_c(colour, result.status)}] {result.trial.describe()}")
        if result.detail:
            print(f"    {result.detail}")
        for cmd in result.commands:
            print(f"    $ {shlex.join(cmd)}")

    # The shared closing count (tally() above) — the web UI shows the same line.
    counts = tally(r.status for r in results)
    print("\n" + _c("31;1" if counts["bad"] else "32;1", counts["line"]))
    return 1 if counts["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())
