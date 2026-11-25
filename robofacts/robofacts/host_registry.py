"""One host registry for every experiment, ``common/config/hosts.yaml`` (2026-11-16).

Why. Before this file, every experiment's ``config/runner.yaml`` carried a full copy of each
host spec (ssh alias, container, container hostname, shell). In the first lab that used the
framework, 18 runner.yaml files held such copies on 2026-11-16, and they had drifted: one level
used three different ssh aliases, and two of them did not reach the robot computer on the lab's
own network. A copy that drifts aims a trial at the wrong machine, and nothing notices.

How. ``hosts.yaml`` declares the full spec of each host level once::

    version: 1
    rig: robot                      # the level RIG_HOST retargets
    hosts:
      laptop: {local: true}
      robot:
        host: my-robot-host         # an ssh alias
        container: ros2_humble_dev
        container_hostname: robot-dev
        shell: zsh

An experiment's ``runner.yaml`` still lists the levels it may run on, because not every experiment
can run everywhere. A level with no value takes the registry's spec. A level with keys takes the
registry's spec with those keys replaced, so only what differs is written::

    hosts:
      laptop:
      robot:
        env_setup: [/workspaces/my_project/loopback_dds.zsh]

The environment variable ``RIG_HOST`` replaces the ssh alias of the ``rig`` level for one
session, for example when the robot computer is reached through a VPN name instead of its name
on the lab network. A level the registry does not know is used as written, so a project without
``hosts.yaml`` works as before.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import yaml

#: Relative to the experiments root, next to robots.yaml, storage.yaml and factors.yaml.
REGISTRY_FILE = Path("common") / "config" / "hosts.yaml"

#: The environment variable that retargets the rig level for one session.
RIG_HOST_ENV = "RIG_HOST"


class HostRegistryError(ValueError):
    """hosts.yaml, or a runner.yaml hosts block, is malformed."""


@dataclass
class HostRegistry:
    hosts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: The level that RIG_HOST retargets, or None when the registry names none.
    rig: Optional[str] = None
    source: Optional[Path] = None


def _spec(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HostRegistryError(f"{where} must be a mapping of host settings, got {value!r}")
    return dict(value)


def load(experiments_root: Path) -> HostRegistry:
    """Read hosts.yaml. A missing file means an empty registry, not an error."""
    return load_file(Path(experiments_root) / REGISTRY_FILE)


def load_file(path: Path) -> HostRegistry:
    """Read one hosts.yaml file. A missing file means an empty registry."""
    path = Path(path)
    if not path.is_file():
        return HostRegistry()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise HostRegistryError(f"{path}: expected a mapping with a 'hosts' key")
    hosts_raw = raw.get("hosts") or {}
    if not isinstance(hosts_raw, dict):
        raise HostRegistryError(f"{path}: 'hosts' must map each level to its settings")
    hosts = {str(level): _spec(spec, f"{path}: hosts.{level}") for level, spec in hosts_raw.items()}
    rig = raw.get("rig")
    if rig is not None and str(rig) not in hosts:
        raise HostRegistryError(f"{path}: rig: {rig!r} is not one of the declared levels "
                                f"{sorted(hosts)}")
    return HostRegistry(hosts=hosts, rig=(str(rig) if rig is not None else None), source=path)


def resolve(registry: HostRegistry, declared: Any, where: str,
            env: Optional[Mapping[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """The host specs of one experiment: its declared levels, filled from the registry.

    ``declared`` is the runner.yaml ``hosts:`` value. Each of its levels gets the registry's spec
    for that level with the experiment's own keys on top. Levels the experiment does not declare
    are not added, so the experiment still decides where it may run. Then RIG_HOST replaces the
    ssh alias of the rig level, unless that level runs locally for this experiment.
    """
    env = os.environ if env is None else env
    if declared is None:
        declared = {}
    if not isinstance(declared, dict):
        raise HostRegistryError(f"{where}: hosts must map each level to its settings, "
                                f"got {declared!r}")
    merged: Dict[str, Dict[str, Any]] = {}
    for level, override in declared.items():
        level = str(level)
        spec = copy.deepcopy(registry.hosts.get(level, {}))
        spec.update(_spec(override, f"{where}: hosts.{level}"))
        merged[level] = spec
    rig_host = str(env.get(RIG_HOST_ENV, "") or "").strip()
    if rig_host and registry.rig in merged and not merged[registry.rig].get("local", False):
        merged[registry.rig]["host"] = rig_host
    return merged


# --------------------------------------------------------------------------- #
# Where a robot's stop and actions go (estop.sh, robot_action.sh, the web interface)
# --------------------------------------------------------------------------- #
@dataclass
class StopTarget:
    """Where one robot's commands run: ``host`` is an ssh alias or ``local``."""
    robot: str
    host: str
    container: str
    shell: str
    #: Why this target, in words, for ``estop.sh --list`` and the stop output.
    source: str
    #: The hostname INSIDE the target's container, when the registry knows it. A stop typed
    #: inside a container runs directly only when this matches (2026-11-17).
    container_hostname: str = ""

    def runs_directly_here(self, *, in_container: Optional[bool] = None,
                           hostname: Optional[str] = None) -> bool:
        """True when a stop for this target, typed here, runs in the current container.

        Inside a container, the command runs directly only when this container IS the target's
        container: its hostname matches ``container_hostname``. Before 2026-11-17 any container
        counted, so a stop typed in the laptop's container for a stack on the robot computer
        stayed on the laptop. A target without ``container_hostname`` keeps the old rule.
        """
        if not self.container:
            return False
        if in_container is None:
            in_container = Path(os.environ.get("TRIALS_DOCKERENV", "/.dockerenv")).exists()
        if not in_container:
            return False
        if not self.container_hostname:
            return True
        import socket
        return (hostname or socket.gethostname()) == self.container_hostname

    def describe(self) -> str:
        """Where the command goes from here, in words."""
        if self.runs_directly_here():
            return "here, in this container"
        where = self.host
        return f"{where}/{self.container}" if self.container else where


class StopTargetError(ValueError):
    """A host level that the registry does not define, or a malformed robots.yaml entry."""


def experiments_root_of(robots_config: Path) -> Optional[Path]:
    """The experiments root of ``<root>/common/config/robots.yaml``, else None."""
    path = Path(robots_config).resolve()
    if path.parent.name == "config" and path.parent.parent.name == "common":
        return path.parent.parent.parent
    return None


def _target_from_level(robot: str, registry: HostRegistry, level: str, source: str,
                       env: Mapping[str, str]) -> StopTarget:
    if level not in registry.hosts:
        where = registry.source or "hosts.yaml (not found)"
        raise StopTargetError(f"robot {robot!r}: host level {level!r} is not defined in {where} "
                              f"(levels: {sorted(registry.hosts) or 'none'})")
    spec = registry.hosts[level]
    local = bool(spec.get("local", False))
    host = "local" if local else str(spec.get("host") or "")
    if not host:
        raise StopTargetError(f"robot {robot!r}: host level {level!r} has neither 'local: true' "
                              f"nor 'host' in {registry.source}")
    rig_host = str(env.get(RIG_HOST_ENV, "") or "").strip()
    if rig_host and level == registry.rig and not local:
        host, source = rig_host, f"{source}, {RIG_HOST_ENV} replaced {spec.get('host')}"
    return StopTarget(robot, host, str(spec.get("container") or ""),
                      str(spec.get("shell") or "zsh"), source,
                      str(spec.get("container_hostname") or ""))


def robot_targets(robots_config: Path, robots: Mapping[str, Any], *,
                  level: Optional[str] = None,
                  marker: Optional[Mapping[str, Any]] = None,
                  env: Optional[Mapping[str, str]] = None) -> Dict[str, StopTarget]:
    """Where each robot's stop goes, in this order of precedence:

    1. ``level``, the operator's ``--host-level`` for this one call.
    2. The live run marker of a HARDWARE trial on this machine (run_marker.py), for the robots
       its experiment declares under ``robots:``, or every robot when it declares none: the
       stack, and so the robot's driver, runs on the trial's execution host. A simulated trial
       never moves a stop, because a simulation on the laptop must not pull the stop of a real
       robot away from the rig.
    3. The robot's ``host_level`` in robots.yaml, resolved through hosts.yaml next to it.
    4. The robot's own ``host``, ``container`` and ``shell`` (a project without hosts.yaml).
       ``RIG_HOST`` replaces any non-local host here, as estop.sh always did.

    ``RIG_HOST`` also replaces the alias of the registry's ``rig`` level in 1 to 3.
    """
    env = os.environ if env is None else env
    registry = load_file(Path(robots_config).parent / "hosts.yaml")
    trial_level: Optional[str] = None
    trial_robots: Optional[set] = None
    if marker and not marker.get("simulation") and marker.get("execution_host") is not None:
        candidate = str(marker["execution_host"])
        if candidate in registry.hosts:
            trial_level = candidate
            declared = marker.get("robots")
            trial_robots = None if declared is None else {str(r) for r in declared}
    targets: Dict[str, StopTarget] = {}
    for name, raw in robots.items():
        name = str(name)
        spec = raw if isinstance(raw, dict) else {}
        if level:
            targets[name] = _target_from_level(name, registry, str(level),
                                               f"--host-level {level}", env)
        elif trial_level is not None and (trial_robots is None or name in trial_robots):
            label = marker.get("label") or marker.get("campaign") or marker.get("experiment")
            targets[name] = _target_from_level(
                name, registry, trial_level,
                f"the running trial ({label}, execution_host={trial_level}, "
                f"pid {marker.get('pid')} on {marker.get('host')})", env)
        elif spec.get("host_level"):
            targets[name] = _target_from_level(name, registry, str(spec["host_level"]),
                                               f"robots.yaml host_level {spec['host_level']}", env)
        else:
            host = str(spec.get("host") or "local")
            source = "robots.yaml host"
            rig_host = str(env.get(RIG_HOST_ENV, "") or "").strip()
            if rig_host and host != "local":
                host, source = rig_host, f"{RIG_HOST_ENV} (robots.yaml says {spec.get('host')})"
            targets[name] = StopTarget(name, host, str(spec.get("container") or ""),
                                       str(spec.get("shell") or "zsh"), source,
                                       str(spec.get("container_hostname") or ""))
    return targets


# --------------------------------------------------------------------------- #
# Where a killed run left its stack (run_marker, trial_runner.recover_killed_trial)
# --------------------------------------------------------------------------- #
def run_host(experiments_root: Path, record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Where the run of a run marker ran its stack and its recorder, when that is another
    machine than the runner's: ``{"level", "host", "container", "shell", "workspace",
    "backend"}``, or None when they ran on the runner's machine or nothing tells (third review
    of 2026-11-25).

    The trial in progress names its ssh host (``trial.remote``). Otherwise the marker's
    ``execution_host`` level is resolved as the runner resolves it: the experiment's
    runner.yaml ``hosts:``, filled from hosts.yaml. A run marker sees only the process
    groups of the runner's machine, which for such a run are ssh clients: the stack and the
    recorder they started can outlive them. Never raises.
    """
    try:
        pending = record.get("trial") if isinstance(record.get("trial"), dict) else {}
        level = record.get("execution_host")
        spec: Dict[str, Any] = {}
        if level is not None:
            root = Path(experiments_root)
            registry = load(root)
            declared = None
            runner = root / str(record.get("experiment") or "") / "config" / "runner.yaml"
            if record.get("experiment") and runner.is_file():
                declared = (yaml.safe_load(runner.read_text(encoding="utf-8")) or {}).get("hosts")
            merged = resolve(registry, declared if isinstance(declared, dict) else {}, str(runner))
            spec = merged.get(str(level)) or registry.hosts.get(str(level)) or {}
        host = pending.get("remote")
        if not host:
            runner_inside = spec.get("container_hostname") and \
                record.get("host") == spec.get("container_hostname")
            if spec.get("local", False) or runner_inside:
                return None
            host = spec.get("host")
        if not host:
            return None
        backend_name = pending.get("backend")
        if not backend_name:
            from . import backend
            try:
                backend_name = backend.configured_name(experiments_root)
            except Exception:  # noqa: BLE001 - the clean-up then names no tool
                backend_name = None
        return {"level": level, "host": str(host), "container": spec.get("container") or None,
                "shell": str(spec.get("shell") or "zsh"), "workspace": spec.get("workspace") or None,
                "backend": backend_name}
    except Exception:  # noqa: BLE001 - only a message depends on it
        pending = record.get("trial") if isinstance(record, Mapping) else None
        remote = pending.get("remote") if isinstance(pending, dict) else None
        return {"level": None, "host": str(remote), "container": None, "shell": "zsh",
                "workspace": None, "backend": pending.get("backend")} if remote else None


def remote_cleanup(where: Mapping[str, Any]) -> str:
    """The clean slate to run on the machine of ``where`` (run_host), as the web interface's
    clean slate runs it there: the backend's clean-slate tool (``trials tool <tool>``) in the
    project's checkout, inside the host's container. A backend without such a tool gets the
    plain instruction."""
    tool = ""
    try:
        from . import backend
        if where.get("backend"):
            tool = backend.load(str(where["backend"])).clean_slate_tool
    except Exception:  # noqa: BLE001 - the plain instruction then
        tool = ""
    lines = [f"  ssh {where['host']}"]
    if where.get("container"):
        lines.append(f"  docker exec -it {where['container']} {where.get('shell') or 'zsh'} -l")
    if not tool:
        lines.append("  then stop the stack and the recorder of that run there")
    elif where.get("workspace"):
        lines.append(f"  cd {where['workspace']} && trials tool {tool}")
    else:
        lines.append(f"  trials tool {tool}   (in the project's checkout)")
    return "\n".join(lines)
