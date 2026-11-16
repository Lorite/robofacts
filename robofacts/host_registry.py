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
    path = Path(experiments_root) / REGISTRY_FILE
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
