#!/usr/bin/env python3
"""Declarative trial and campaign specification (#96 Phase 2).

A trial used to exist only as loose variables inside a run notebook, so nothing
could enumerate the space, diff it against what had been recorded, or hand a
chosen point to a runner. This makes a trial a **value**.

Two files, deliberately separate:

* **factor definitions** — ``experiments/common/config/factors.yaml`` plus an
  optional ``experiments/<name>/config/factors.yaml`` merged on top. These say
  which axes exist, which levels are legal, what the default is, and how the
  runner realises a level (host / launch argument / record profile).
* **a campaign** — ``experiments/<name>/campaigns/<id>.yaml``: for *this* run,
  sweep these levels, fix those, repeat N times.

Why the split: the axes change rarely and are shared; campaigns are cheap and
disposable. It is the same shape as Hydra's config groups plus experiment
configs, and the same reason — but without the dependency, because a trial here
is ``ros2 launch`` on a possibly-remote host with hardware in the loop, not a
Python entry point Hydra can own.

The cross-check that earns its keep
-----------------------------------
Every factor declares ``metadata_key``: the field the trial must record. If a
campaign sweeps a factor that never lands in ``trial_metadata.json``, its
coverage can never be measured — the grid would silently collapse that axis. So
``validate_against_metadata_contract`` refuses it, closing the loop
**factors → recorded metadata → coverage axes**.

Overrides use Hydra's shape, e.g. ``trajectory=hover,circle execution_host=rig``.
"""

from __future__ import annotations

import argparse
import itertools
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import yaml


from . import roots


def __getattr__(name):
    """``EXPERIMENTS_ROOT`` and ``REPO_ROOT`` for callers of the old constants, resolved on access."""
    return roots.module_root_attribute(name)
CORE_FACTORS = "common/config/factors.yaml"
PER_EXPERIMENT_FACTORS = "config/factors.yaml"

VALID_KINDS = {"host", "launch_arg", "record_profile", "internal", "gate", "operator"}

#: Keys of a factor's ``check:`` block that are not arguments of the check.
#: Everything else in the block (topic, window_s, ...) is an argument common
#: to every level, and ``per_level`` adds or overrides arguments per level.
CHECK_RESERVED = {"script", "args", "per_level", "realized", "text", "hard", "calibrate", "parallel"}

#: The two domains a trial can belong to (2026-10-06, the coverage view per
#: condition and domain). A trial is `simulation` when its runner.yaml says
#: `simulation: true`, or when one of its factor levels declares the trial
#: simulated (a crazyswarm2 `backend: sim`), else `hardware`.
DOMAINS = ("simulation", "hardware")

#: What a campaign does with its factors' checks. `binding`: the checks join
#: the T-0 gate (the default). `calibration`: they are MEASURED instead (the
#: check's `calibrate` command runs after the gate), for the scratch-only run
#: that produces the bands the binding campaign then checks against.
FACTOR_CHECK_MODES = ("binding", "calibration")

#: The meaning of each level of a ``maps_to.kind: gate`` factor, when the
#: factor does not spell it out under ``maps_to.modes``. Added 2026-10-01 for
#: the SAC 2027 gate-effect experiment: ``off`` is a SHADOW gate (every check
#: runs and its verdict is recorded, a NO-GO never blocks), so both arms carry
#: a verdict and the only difference between them is the blocking decision.
DEFAULT_GATE_MODES = {"on": "binding", "off": "shadow", "disabled": "disabled"}
GATE_MODE_NAMES = ("binding", "shadow", "disabled")

#: Marker every blocked-level refusal carries, so a caller can tell "this
#: campaign asks for something the world cannot deliver yet" apart from "this
#: campaign is malformed". The config lint (L10) uses it to keep a deliberately
#: parked campaign from failing the build while every other defect still does.
BLOCKED_MARKER = "BLOCKED LEVEL"

#: Spoken operator cues (2026-10-13). The runner speaks a campaign's
#: ``voice_cues`` during the recorded action and its ``voice_announce`` lines
#: at fixed moments of the trial, through operator_voice.py and the laptop's
#: voice daemon. These are the moments a ``voice_announce`` may name.
VOICE_ANNOUNCE_KEYS = ("before_launch", "record_start", "record_end", "gate_blocked")
#: Keys of one ``voice_cues`` entry.
VOICE_CUE_KEYS = ("at_s", "text")


class SpecError(Exception):
    """The factor definitions or the campaign are malformed."""


# --------------------------------------------------------------------------- #
# Factors
# --------------------------------------------------------------------------- #
@dataclass
class Factor:
    name: str
    levels: List[Any]
    default: Any
    describe: str = ""
    metadata_key: Optional[str] = None
    maps_to: Dict[str, Any] = field(default_factory=dict)
    #: level -> other spellings of it seen in already-recorded metadata. Host
    #: names in particular were written ad hoc before this existed ("local",
    #: "lori@jetson"), and without aliases every historical bag would be
    #: permanently unmatchable against the canonical levels.
    aliases: Dict[Any, List[str]] = field(default_factory=dict)
    #: level -> why it CANNOT BE RUN TODAY. A level stays declared (it is a real
    #: point of the space, and bags recorded on it must keep matching) but no
    #: campaign may ask for it, and every surface that loads a campaign refuses.
    #:
    #: The incident: `icra_budget_latency_ab` swept `latency_stamping: [off, on]`
    #: while the firmware capture-time stamping it names did not exist. The
    #: factor is operator-applied, so nothing in the rig could tell the two arms
    #: apart: the `on` trials would have flown STOCK firmware and recorded a bag
    #: labelled `latency_stamping=on`, i.e. a meaningless cell that reads as
    #: coverage forever. A level whose apparatus does not exist has to be refused
    #: at plan time, before anyone drives to the lab.
    blocked: Dict[Any, str] = field(default_factory=dict)
    #: A REALIZED-CONDITION CHECK (2026-10-06, SAC 2027 framework paper). A
    #: level is a claim (``lighting=lights_dimmed``) until a measurement on the
    #: rig confirms it. When a trial's value of this factor is set, the runner
    #: adds this check to the trial's T-0 gate with the level's own arguments
    #: (``factor_check_entries``), so a trial declared dimmed with the lights
    #: on is refused before anything is recorded. Normalized form:
    #: ``{script, args, per_level: {level: {arg: value}}, realized, hard}``.
    check: Optional[Dict[str, Any]] = None
    #: Why a factor the operator realizes is NOT checked. Lint L13 requires a
    #: ``kind: operator`` factor to carry either ``check`` or this reason, so an
    #: unverifiable condition is a reviewed decision rather than an omission.
    unchecked: str = ""
    #: level -> "simulation" | "hardware", for a factor whose level decides
    #: the trial's domain (crazyswarm2's `backend: sim` runs no hardware in an
    #: experiment that otherwise flies). Levels not named follow runner.yaml.
    domain: Dict[Any, str] = field(default_factory=dict)

    def is_default(self, value: Any) -> bool:
        return value == self.default

    def canonical(self, recorded: Any) -> str:
        """Map a value as recorded onto its declared level, when it is an alias."""
        text = str(recorded)
        for level, spellings in self.aliases.items():
            if text == str(level) or text in [str(s) for s in spellings]:
                return str(level)
        return text


#: Parsed YAML by (path, mtime, size), handed out as deep copies. A page that
#: lists 27 campaigns parsed the shared factors.yaml 27 times on every request,
#: which made the web UI's experiment page take seconds (2026-10-16, #131). A
#: changed file has a new mtime, so an edit is never missed.
_YAML_CACHE: Dict[tuple, Any] = {}


def _yaml_file(path: Path) -> Any:
    import copy
    stat = Path(path).stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _YAML_CACHE:
        _YAML_CACHE[key] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return copy.deepcopy(_YAML_CACHE[key])


def _load_factor_file(path: Path) -> Dict[str, Factor]:
    try:
        raw = _yaml_file(path)
    except yaml.YAMLError as exc:
        raise SpecError(f"{path}: invalid YAML: {exc}") from exc
    factors = raw.get("factors")
    if not isinstance(factors, dict):
        raise SpecError(f"{path}: expected a top-level 'factors' mapping")

    out: Dict[str, Factor] = {}
    for name, spec in factors.items():
        if not isinstance(spec, dict):
            raise SpecError(f"{path}: factor {name!r} must be a mapping")
        levels = spec.get("levels")
        if not isinstance(levels, list) or not levels:
            raise SpecError(f"{path}: factor {name!r} needs a non-empty 'levels' list")
        if "default" not in spec:
            raise SpecError(f"{path}: factor {name!r} needs a 'default' — it is what "
                            f"'non-default' is measured against when a plan is shown")
        default = spec["default"]
        if default not in levels:
            raise SpecError(f"{path}: factor {name!r} default {default!r} is not among its levels")
        maps_to = spec.get("maps_to") or {}
        if not isinstance(maps_to, dict):
            raise SpecError(f"{path}: factor {name!r} has a non-mapping 'maps_to'")
        kind = maps_to.get("kind", "internal")
        if kind not in VALID_KINDS:
            raise SpecError(f"{path}: factor {name!r} has unknown maps_to.kind {kind!r} "
                            f"(known: {', '.join(sorted(VALID_KINDS))})")
        if kind == "launch_arg" and not (maps_to.get("name") or maps_to.get("names")):
            raise SpecError(f"{path}: factor {name!r} maps to a launch_arg but names no argument")
        if kind == "gate":
            _check_gate_factor(path, name, levels, maps_to)
        aliases = spec.get("aliases") or {}
        if not isinstance(aliases, dict):
            raise SpecError(f"{path}: factor {name!r} has a non-mapping 'aliases'")
        for level in aliases:
            if level not in levels:
                raise SpecError(f"{path}: factor {name!r} aliases unknown level {level!r}")
        blocked = spec.get("blocked") or {}
        if not isinstance(blocked, dict):
            raise SpecError(f"{path}: factor {name!r} has a non-mapping 'blocked' — it must be "
                            f"level -> the reason that level cannot be run")
        for level, reason in blocked.items():
            if level not in levels:
                raise SpecError(f"{path}: factor {name!r} blocks unknown level {level!r}")
            if not str(reason).strip():
                raise SpecError(f"{path}: factor {name!r} blocks level {level!r} with no reason. "
                                f"A block an operator cannot read is a block they will delete")
            # Blocking the default would apply to every campaign that never
            # mentions the factor, including all the historical ones, and the
            # refusal would name a level nobody chose. If the default itself is
            # unrunnable, the factor should not be in the space yet.
            if level == default:
                raise SpecError(f"{path}: factor {name!r} blocks its own default {level!r} — "
                                f"that would refuse every campaign that never mentions it")
        check = _normalize_check(path, name, levels, spec.get("check"))
        domain = spec.get("domain") or {}
        if not isinstance(domain, dict):
            raise SpecError(f"{path}: factor {name!r} has a non-mapping 'domain' (level -> "
                            f"simulation|hardware)")
        for level, value in domain.items():
            if level not in levels:
                raise SpecError(f"{path}: factor {name!r} `domain` names unknown level {level!r}")
            if value not in DOMAINS:
                raise SpecError(f"{path}: factor {name!r} `domain.{level}` is {value!r} "
                                f"(known: {', '.join(DOMAINS)})")
        unchecked = str(spec.get("unchecked") or "").strip()
        if check and unchecked:
            raise SpecError(f"{path}: factor {name!r} declares both `check` and `unchecked`. "
                            f"Keep the one that is true")
        out[name] = Factor(
            name=name, levels=list(levels), default=default,
            describe=str(spec.get("describe", "")).strip(),
            metadata_key=spec.get("metadata_key"), maps_to=maps_to,
            aliases={k: list(v) for k, v in aliases.items()},
            blocked={k: str(v).strip() for k, v in blocked.items()},
            check=check, unchecked=unchecked, domain=dict(domain),
        )
    return out


def _normalize_check(path: Path, name: str, levels: List[Any],
                     raw: Any) -> Optional[Dict[str, Any]]:
    """Validate a factor's ``check:`` block and return its normalized form.

    Accepted as written in factors.yaml::

        check:
          script: image_brightness.sh        # a preflight/checks/ script, .sh optional
          topic: /camera/image               # arguments common to every level
          window_s: 3                        # (or nested under `args:`)
          per_level:                         # EVERY level, so none is unchecked by accident
            lights_on:     {min: 90, max: 200}
            lights_dimmed: {min: null, max: null}   # null = not calibrated: the check FAILS
          realized:                          # optional, the value measured from the bag
            key: realized_brightness
            command: python3 ... bag {bag} --topic ... --json

    A null argument is passed as an empty value, so a band nobody measured yet
    fails the gate ("not calibrated") instead of silently passing.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: factor {name!r} has a non-mapping `check`")
    script = str(raw.get("script") or "").strip()
    if script.endswith(".sh"):
        script = script[:-3]
    if not script:
        raise SpecError(f"{path}: factor {name!r} `check` names no `script`")
    args = raw.get("args") or {}
    if not isinstance(args, dict):
        raise SpecError(f"{path}: factor {name!r} `check.args` must be a mapping")
    args = dict(args)
    args.update({k: v for k, v in raw.items() if k not in CHECK_RESERVED})
    per_level = raw.get("per_level") or {}
    if not isinstance(per_level, dict):
        raise SpecError(f"{path}: factor {name!r} `check.per_level` must be level -> arguments")
    for level in per_level:
        if level not in levels:
            raise SpecError(f"{path}: factor {name!r} `check.per_level` names unknown level {level!r}")
    missing = [lv for lv in levels if lv not in per_level]
    if per_level and missing:
        raise SpecError(f"{path}: factor {name!r} `check.per_level` leaves level(s) {missing} out. "
                        f"Declare every level, with null bounds until it is calibrated")
    for level, level_args in per_level.items():
        if not isinstance(level_args, dict):
            raise SpecError(f"{path}: factor {name!r} `check.per_level.{level}` must be a mapping")
    realized = raw.get("realized")
    if realized is not None:
        if not (isinstance(realized, dict) and str(realized.get("key") or "").strip()
                and str(realized.get("command") or "").strip()):
            raise SpecError(f"{path}: factor {name!r} `check.realized` needs a `key` and a `command`")
        realized = {"key": str(realized["key"]).strip(), "command": str(realized["command"]),
                    "json": bool(realized.get("json", True))}
    return {"script": script, "args": args,
            "per_level": {k: dict(v) for k, v in per_level.items()},
            "realized": realized, "hard": bool(raw.get("hard", True)),
            "text": str(raw.get("text") or "").strip(),
            # The measurement a calibration run (factor_checks: calibration)
            # makes instead of the check: a command whose last stdout line is
            # JSON, with the trial's {factor} placeholders.
            "calibrate": str(raw.get("calibrate") or "").strip(),
            # Whether the gate may run this check together with other read-only
            # probes (None leaves it to the gate runner's own classification).
            "parallel": (bool(raw["parallel"]) if raw.get("parallel") is not None else None)}


def _check_value(value: Any) -> str:
    """A check argument as the checklist carries it (null -> empty = uncalibrated)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


#: ``{factor}`` but not the checklist's own ``${variable}``.
_FACTOR_PLACEHOLDER = re.compile(r"(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)\}")


def factor_check_entries(values: Dict[str, Any], factors: Dict[str, "Factor"],
                         named: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """The checklist entries a trial's factor values add to its T-0 gate.

    One entry per factor that declares a ``check`` and has a value in the
    trial (and, when ``named`` is given, that the campaign names: a default no
    campaign asked for is not checked), in factor-name order, with the level's arguments merged over the
    common ones. ``{name}`` placeholders in the arguments are filled from the
    trial's values (``substitute_known``), and ``${var}`` ones are left to the
    checklist's own variables. Each entry carries ``factor`` and ``level`` so
    the gate can report which declared condition it confirmed or refuted.
    """
    out: List[Dict[str, Any]] = []
    for name in sorted(values):
        factor = factors.get(name)
        if factor is None or not factor.check or (named is not None and name not in named):
            continue
        level = values[name]
        chk = factor.check
        args = dict(chk["args"])
        args.update(chk["per_level"].get(level, {}))
        rendered = {str(k): _FACTOR_PLACEHOLDER.sub(
            lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
            _check_value(v)) for k, v in args.items()}
        text = chk["text"] or (f"the declared condition {name}={level} is realized "
                               f"(factor check, added by the runner)")
        entry = {"text": ("[HARD] " if chk["hard"] else "") + f"[factor {name}={level}] " + text,
                 "check": chk["script"], "args": rendered,
                 "factor": name, "level": level}
        if chk.get("parallel") is not None:
            entry["parallel"] = chk["parallel"]
        out.append(entry)
    return out


def factor_realized_probes(values: Dict[str, Any], factors: Dict[str, "Factor"],
                           named: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """The metadata probes that record each checked factor's REALIZED value."""
    probes = []
    for name in sorted(values):
        factor = factors.get(name)
        if named is not None and name not in named:
            continue
        if factor is not None and factor.check and factor.check.get("realized"):
            probes.append(dict(factor.check["realized"]))
    return probes


def factor_calibration_commands(values: Dict[str, Any], factors: Dict[str, "Factor"],
                                named: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """{factor, level, command} of each named checked factor that declares `calibrate`."""
    out = []
    for name in sorted(values):
        factor = factors.get(name)
        if factor is None or not factor.check or not factor.check.get("calibrate"):
            continue
        if named is not None and name not in named:
            continue
        out.append({"factor": name, "level": values[name],
                    "command": substitute_known(factor.check["calibrate"], values)})
    return out


def trial_domain(values: Dict[str, Any], factors: Dict[str, "Factor"],
                 simulation: bool) -> tuple:
    """(domain, basis) of a trial: runner.yaml's `simulation`, unless a level says otherwise."""
    for name in sorted(values):
        factor = factors.get(name)
        if factor is not None and values[name] in factor.domain:
            return factor.domain[values[name]], f"factor {name}={values[name]}"
    return ("simulation", "runner.yaml simulation: true") if simulation \
        else ("hardware", "runner.yaml simulation: false")


def operator_factors(factors: Dict[str, "Factor"]) -> List[str]:
    """Names of the factors the operator realizes (``maps_to.kind: operator``)."""
    return sorted(n for n, f in factors.items() if f.maps_to.get("kind") == "operator")


def _check_gate_factor(path: Path, name: str, levels: List[Any],
                       maps_to: Dict[str, Any]) -> None:
    """A gate factor's every level must mean binding, shadow or disabled.

    YAML 1.1 reads an unquoted ``on``/``off`` as a boolean, which would make a
    level silently fail to match its mode, so a non-string level is refused
    with the fix in the message.
    """
    modes = maps_to.get("modes")
    if modes is not None and not isinstance(modes, dict):
        raise SpecError(f"{path}: gate factor {name!r} has a non-mapping maps_to.modes")
    table = {str(k): str(v) for k, v in (modes or DEFAULT_GATE_MODES).items()}
    for level in levels:
        if not isinstance(level, str):
            raise SpecError(f"{path}: gate factor {name!r} level {level!r} is not a string. "
                            f"Quote it (\"on\", \"off\"): YAML reads bare on/off as booleans")
        if level not in table:
            raise SpecError(f"{path}: gate factor {name!r} level {level!r} has no mode. "
                            f"Declare it under maps_to.modes ({', '.join(GATE_MODE_NAMES)})")
        if table[level] not in GATE_MODE_NAMES:
            raise SpecError(f"{path}: gate factor {name!r} maps {level!r} to unknown mode "
                            f"{table[level]!r} (known: {', '.join(GATE_MODE_NAMES)})")


def gate_mode(factor: "Factor", level: Any) -> str:
    """The gate mode a level of a gate factor selects."""
    table = {str(k): str(v) for k, v in (factor.maps_to.get("modes") or DEFAULT_GATE_MODES).items()}
    return table[str(level)]


def gate_factors(factors: Dict[str, "Factor"]) -> List[str]:
    """Names of the factors of kind ``gate``."""
    return sorted(n for n, f in factors.items() if f.maps_to.get("kind") == "gate")


def load_factors(experiment: Optional[str] = None,
                 experiments_root: Optional[Path] = None) -> Dict[str, Factor]:
    """Core factors with the experiment's own merged on top (override by name)."""
    root = roots.experiments_root(experiments_root)
    core = root / CORE_FACTORS
    if not core.is_file():
        raise SpecError(f"core factor definitions not found: {core}")
    factors = _load_factor_file(core)
    if experiment:
        per = root / experiment / PER_EXPERIMENT_FACTORS
        if per.is_file():
            factors.update(_load_factor_file(per))
    return factors


#: When a step applies within ONE runner invocation (2026-11-04, the lab's #159): ``first``
#: only on its first trial, ``repeat`` only on the trials after it, ``always`` on every
#: trial (the default, which is every step before this key existed). A repeated trial of a
#: campaign rarely needs the whole setup again: a 16 s square uses almost no battery, and
#: a drone that boots where it landed needs no re-placement. A runner that stops (a
#: blocked trial, Ctrl-C) starts again with the ``first`` steps, so a broken repeat
#: always falls back to the full procedure.
STEP_WHEN = ("always", "first", "repeat")


def step_applies(step: Dict[str, Any], repeat: bool) -> bool:
    """Does an operator or setup step apply to this trial (``repeat`` = not the first)?"""
    when = str(step.get("when") or "always")
    return when == "always" or (when == "repeat") == bool(repeat)


def steps_for(steps: List[Dict[str, Any]], repeat: bool) -> List[Dict[str, Any]]:
    """The steps of a list that apply to the first trial (``repeat=False``) or a later one."""
    return [st for st in steps or [] if step_applies(st, repeat)]


def _when_problems(where: str, steps: List[Dict[str, Any]]) -> List[str]:
    return [f"{where}[{i}]: when must be one of {', '.join(STEP_WHEN)}, got {st.get('when')!r}"
            for i, st in enumerate(steps or []) if str(st.get("when") or "always") not in STEP_WHEN]


def _setup_steps(raw: Any) -> List[Dict[str, Any]]:
    """Campaign-scoped setup steps, ``{name, command, required, when}`` as runner.yaml's.

    Malformed entries are kept with ``_invalid`` so ``validate`` reports them, instead of
    vanishing (a dropped reset step would make every repeat trial run without it).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return [{"_invalid": raw}]
    out: List[Dict[str, Any]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or not str(entry.get("command", "")).strip():
            out.append({"_invalid": entry})
            continue
        out.append({"name": str(entry.get("name", f"setup_steps_{i}")),
                    "command": str(entry["command"]).strip(),
                    "required": bool(entry.get("required", True)),
                    "when": str(entry.get("when") or "always")})
    return out


def _operator_steps(raw: Any) -> List[Dict[str, Any]]:
    """Normalise operator steps to ``{text, run, when}``.

    A step may be a plain string (instruction only) or a mapping with an
    optional ``run``: a command the UI can execute on the trial's host with
    one click. Declaring the command HERE rather than letting the browser
    post one is what keeps the control surface an allowlist — the campaign
    file, version-controlled and reviewed, is the only source of things that
    can be run, exactly as robots.yaml is for the e-stop.
    """
    out: List[Dict[str, Any]] = []
    for entry in raw or []:
        if isinstance(entry, str):
            out.append({"text": entry.strip(), "run": "", "when": "always"})
        elif isinstance(entry, dict):
            out.append({"text": str(entry.get("text", "")).strip(),
                        "run": str(entry.get("run", "")).strip(),
                        "when": str(entry.get("when") or "always")})
    return out


def substitute_known(template: str, values: Dict[str, Any]) -> str:
    """Fill only ``{known_factor}`` placeholders, leaving other braces alone.

    str.format cannot be used: an operator command legitimately contains
    braces of its own — ``ros2 service call ... "{data: true}"`` — and would
    raise KeyError on them.
    """
    out = template
    for name, value in values.items():
        out = out.replace("{" + str(name) + "}", str(value))
    return out


# --------------------------------------------------------------------------- #
# Campaign
# --------------------------------------------------------------------------- #
@dataclass
class TrialSpec:
    """One concrete point in the factor space."""
    experiment: str
    values: Dict[str, Any]
    non_defaults: List[str]
    repetition: int = 1
    #: Where this trial sits in a randomized campaign order (seed, block,
    #: position), or None when the campaign declares no ``order``. Written into
    #: trial_metadata.json so the order is recoverable from every bag.
    order: Optional[Dict[str, Any]] = None
    #: The factors the campaign NAMES (sweep, fixed, --set), as opposed to the
    #: defaults every trial carries. A factor check runs, and an operator
    #: factor is recorded, only when named (2026-10-06): a default nobody
    #: staged or checked is not a realized condition. None = unknown (a spec
    #: built by hand), which runs every declared check.
    named: Optional[List[str]] = None
    #: binding | calibration, from the campaign's `factor_checks`.
    factor_check_mode: str = "binding"

    def gate_mode(self, factors: Dict[str, "Factor"]) -> Optional[tuple]:
        """(factor name, level, mode) of the trial's gate factor, or None."""
        for name in gate_factors(factors):
            if name in self.values:
                return name, self.values[name], gate_mode(factors[name], self.values[name])
        return None

    def key(self) -> tuple:
        """Stable identity of the combination, ignoring repetition."""
        return tuple(sorted((k, str(v)) for k, v in self.values.items()))

    def launch_args(self, factors: Dict[str, Factor]) -> Dict[str, Any]:
        # A level may drive SEVERAL launch arguments (`names:`), because some
        # identities are spelled more than once in a launch file — the drone's
        # name is both its ROS namespace and its mocap rigid body, and letting
        # those drift apart is how a trial records one drone while tracking
        # another.
        args: Dict[str, Any] = {}
        for n, v in self.values.items():
            factor = factors.get(n)
            if factor is None or factor.maps_to.get("kind") != "launch_arg":
                continue
            names = factor.maps_to.get("names") or [factor.maps_to["name"]]
            # `level_values` translates a semantic level into the string the
            # launch actually needs — e.g. airframe `patched` selecting the
            # mocap asset `cfb1_paperv1`. Without it the two would have to be
            # separate factors that a campaign could set inconsistently.
            resolved = (factor.maps_to.get("level_values") or {}).get(v, v)
            if isinstance(resolved, dict):
                # Per-argument values: one level can mean different things to
                # different arguments (a pose_fusion level picks BOTH the
                # crazyflies config and which node feeds /poses).
                for arg_name, arg_value in resolved.items():
                    args[str(arg_name)] = arg_value
            else:
                for arg_name in names:
                    args[str(arg_name)] = resolved
        return args

    def host(self, factors: Dict[str, Factor]) -> Optional[Any]:
        for name, value in self.values.items():
            if name in factors and factors[name].maps_to.get("kind") == "host":
                return value
        return None

    def describe(self) -> str:
        parts = []
        for name in sorted(self.values):
            marker = "*" if name in self.non_defaults else " "
            parts.append(f"{name}={self.values[name]}{marker}")
        return " ".join(parts)


@dataclass
class Campaign:
    name: str
    experiment: str
    factors: Dict[str, Factor]
    sweep: Dict[str, List[Any]]
    fixed: Dict[str, Any]
    repetitions: int = 1
    describe: str = ""
    #: Ordered steps the OPERATOR must do that no runner can: physical setup
    #: (move the pad, tape the panels), and the deliberately-manual safety
    #: steps (starting the extpos bridge with shadow off). Declared here, next
    #: to the trials they gate, because instructions living only in prose get
    #: read once and then lost behind the button that needs them.
    operator_steps: List[Dict[str, Any]] = field(default_factory=list)
    #: Steps the operator performs WHILE the trial is running — the drone is
    #: in the air and the runner is mid-sequence. Kept apart from
    #: `operator_steps` because the two are read at different moments and
    #: mixing them is how a live action gets done too early, or missed.
    operator_live_steps: List[Dict[str, Any]] = field(default_factory=list)
    #: Campaign-scoped post-trial checks, ADDED to the runner's own. This is
    #: where "did the operator actually do their part" lives: the runner's
    #: verify steps are universal (did it fly), these are specific to what
    #: this campaign asked a human to do.
    verify_steps: List[Dict[str, Any]] = field(default_factory=list)
    #: Campaign-scoped setup steps (2026-11-04), run AFTER runner.yaml's ``setup_steps``
    #: and before the stack launches, ``{name, command, required, when}``. The reset a
    #: repeated trial needs (the Crazyflie's radio power cycle, so its frame restarts
    #: where it landed) belongs to the campaign that repeats, not to every trial of the
    #: experiment. ``{factor}`` and ``{arg_*}`` placeholders fill as in runner.yaml.
    setup_steps: List[Dict[str, Any]] = field(default_factory=list)
    #: False opts this campaign out of the robots' reset steps (robots.yaml ``reset:``, run for
    #: the robots runner.yaml declares, 2026-11-04). Default True.
    robot_reset: bool = True
    #: The T-0 gate for THIS campaign, relative to the experiment directory.
    #: Empty = the experiment's config/preflight_checklist.json.
    #:
    #: Preflight belongs to the campaign for the same reason operator_steps and
    #: verify_steps do: a campaign is "what set of trials, under what conditions",
    #: and the conditions are what the gate checks. One list per EXPERIMENT cannot
    #: serve both a campaign that flies and one whose drone hangs unpowered from
    #: the ceiling — the first attempt subtracted checks from a shared list, which
    #: made the gate something you had to read two files to understand.
    preflight_checklist: str = ""
    #: Randomized execution order (2026-10-01): ``{shuffle: bool, seed: int,
    #: blocks: bool | int}``. Empty = the planner's own order, unchanged.
    #: ``blocks: true`` makes randomized complete blocks, one per repetition
    #: of the least-repeated cell, each holding every cell, and ``shuffle`` draws
    #: the order inside each block from ``seed``. It changes WHEN a trial runs,
    #: never WHAT is wanted or covered.
    order: Dict[str, Any] = field(default_factory=dict)
    #: binding (default) | calibration: see FACTOR_CHECK_MODES. A calibration
    #: campaign runs only with --scratch, so its bags never count.
    factor_checks: str = "binding"
    #: Per-cell repetition counts (2026-10-01): a list of
    #: ``{where: {factor: level, ...}, repetitions: N}``. The first entry whose
    #: ``where`` matches a combination sets its count, else ``repetitions``.
    #: Exists so a control cell can be repeated more often than the treatment
    #: cells inside ONE campaign, which a randomized block design needs.
    repetitions_where: List[Dict[str, Any]] = field(default_factory=list)
    #: Spoken cues during the recorded action (2026-10-13): a list of
    #: ``{at_s, text}``, ``at_s`` in seconds after the recording start (the
    #: moment the action starts, 1 s after the recorder). Kept raw here and
    #: checked by ``validate``, and the runner sorts them by time. Empty = none.
    voice_cues: List[Any] = field(default_factory=list)
    #: The runner's own spoken lines at fixed moments (VOICE_ANNOUNCE_KEYS):
    #: None = the runner's defaults (spoken on hardware experiments only),
    #: False = none at all, a mapping = per-moment text (null or "" silences
    #: one moment, an absent key keeps its default).
    voice_announce: Any = None
    source: Optional[Path] = None

    def repetitions_for(self, values: Dict[str, Any]) -> int:
        for entry in self.repetitions_where:
            where = entry.get("where") or {}
            if all(str(values.get(k)) == str(v) for k, v in where.items()):
                return int(entry.get("repetitions", self.repetitions))
        return self.repetitions

    def combinations(self) -> Iterator[TrialSpec]:
        """The full cross product. Phase 3 subtracts what already exists."""
        names = sorted(self.sweep)
        level_lists = [self.sweep[n] for n in names]
        for combo in itertools.product(*level_lists) if names else [()]:
            values = {name: self.factors[name].default for name in self.factors}
            values.update(self.fixed)
            values.update(dict(zip(names, combo)))
            non_defaults = sorted(n for n, v in values.items()
                                  if n in self.factors and not self.factors[n].is_default(v))
            for rep in range(1, self.repetitions_for(values) + 1):
                yield TrialSpec(self.experiment, dict(values), non_defaults, rep,
                                named=sorted(set(self.sweep) | set(self.fixed)),
                                factor_check_mode=self.factor_checks)

    def size(self) -> int:
        if self.repetitions_where:
            return sum(1 for _ in self.combinations())
        n = 1
        for levels in self.sweep.values():
            n *= len(levels)
        return n * self.repetitions


def _build_campaign(path: Path, experiments_root: Optional[Path] = None,
                    overrides: Optional[List[str]] = None) -> Campaign:
    """Parse a campaign file WITHOUT validating it (see load_campaign)."""
    try:
        raw = _yaml_file(Path(path))
    except FileNotFoundError as exc:
        raise SpecError(f"campaign not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: expected a mapping")

    experiment = raw.get("experiment")
    if not experiment:
        raise SpecError(f"{path}: 'experiment' is required — a campaign belongs to one experiment")

    factors = load_factors(experiment, experiments_root)
    sweep = {k: (v if isinstance(v, list) else [v]) for k, v in (raw.get("sweep") or {}).items()}
    fixed = dict(raw.get("fixed") or {})

    for name, value in parse_overrides(overrides or []).items():
        # A --set value arrives as text; a YAML level may be an int or a bool. Match
        # it to the declared level by its printed form so `--set
        # injected_latency_ms=0` pins the int level 0 rather than failing as the
        # string '0' (2026-09-23, first --scratch flight from the laptop).
        if name in factors:
            declared = {str(lv): lv for lv in factors[name].levels}
            value = [declared.get(v, v) for v in value]
        # An override with several values sweeps; with one, it pins.
        (sweep if len(value) > 1 else fixed)[name] = value if len(value) > 1 else value[0]
        if len(value) > 1:
            fixed.pop(name, None)
        else:
            sweep.pop(name, None)

    campaign = Campaign(
        name=str(raw.get("name") or Path(path).stem),
        experiment=str(experiment),
        factors=factors,
        sweep=sweep,
        fixed=fixed,
        repetitions=int(raw.get("repetitions", 1)),
        describe=str(raw.get("describe", "")).strip(),
        operator_steps=_operator_steps(raw.get("operator_steps")),
        operator_live_steps=_operator_steps(raw.get("operator_live_steps")),
        verify_steps=[dict(v) for v in (raw.get("verify_steps") or [])],
        setup_steps=_setup_steps(raw.get("setup_steps")),
        robot_reset=raw.get("robot_reset", True),
        preflight_checklist=str(raw.get("preflight_checklist") or ""),
        order=dict(raw.get("order") or {}) if isinstance(raw.get("order"), dict)
        else ({"_invalid": raw.get("order")} if raw.get("order") is not None else {}),
        repetitions_where=list(raw.get("repetitions_where") or [])
        if isinstance(raw.get("repetitions_where"), list)
        else ([{"_invalid": raw.get("repetitions_where")}]
              if raw.get("repetitions_where") is not None else []),
        factor_checks=str(raw.get("factor_checks") or "binding"),
        voice_cues=(list(raw.get("voice_cues") or []) if isinstance(raw.get("voice_cues"), list)
                    else ([{"_invalid": raw.get("voice_cues")}]
                          if raw.get("voice_cues") is not None else [])),
        voice_announce=raw.get("voice_announce"),
        source=Path(path),
    )
    return campaign


def campaign_problems(path: Path, experiments_root: Optional[Path] = None,
                      overrides: Optional[List[str]] = None) -> List[str]:
    """Everything wrong with a campaign, WITHOUT raising.

    The config lint needs to tell a deliberately parked campaign (one asking for
    a ``blocked`` level, which carries ``BLOCKED_MARKER``) apart from a broken
    one, and an exception message is a poor thing to parse.
    """
    return validate(_build_campaign(path, experiments_root, overrides))


def load_campaign(path: Path, experiments_root: Optional[Path] = None,
                  overrides: Optional[List[str]] = None) -> Campaign:
    campaign = _build_campaign(path, experiments_root, overrides)
    problems = validate(campaign)
    if problems:
        raise SpecError(f"{path}:\n  " + "\n  ".join(problems))
    return campaign


def parse_overrides(overrides: List[str]) -> Dict[str, List[Any]]:
    """Hydra-shaped overrides: ``name=a`` pins, ``name=a,b`` sweeps."""
    out: Dict[str, List[Any]] = {}
    for override in overrides:
        if "=" not in override:
            raise SpecError(f"override {override!r} must be name=value[,value...]")
        name, _, value = override.partition("=")
        out[name.strip()] = [v.strip() for v in value.split(",") if v.strip()]
    return out


def validate(campaign: Campaign) -> List[str]:
    problems: List[str] = []
    known = campaign.factors

    for source, entries in (("sweep", campaign.sweep), ("fixed", campaign.fixed)):
        for name, value in entries.items():
            if name not in known:
                problems.append(f"{source}: unknown factor {name!r} "
                                f"(declared: {', '.join(sorted(known))})")
                continue
            for level in (value if isinstance(value, list) else [value]):
                if level not in known[name].levels:
                    problems.append(f"{source}: {name}={level!r} is not a declared level "
                                    f"({known[name].levels})")
                    continue
                # A level the apparatus cannot deliver is refused HERE, at plan
                # time, rather than at the rig — because an operator-applied
                # factor has nothing at the rig to refuse it. Every surface that
                # runs a campaign (campaign_plan, trial_runner, the UI) goes
                # through load_campaign, so blocking once blocks everywhere.
                reason = known[name].blocked.get(level)
                if reason:
                    problems.append(f"{BLOCKED_MARKER}: {source} {name}={level!r} cannot be run. "
                                    f"{reason}")

    overlap = set(campaign.sweep) & set(campaign.fixed)
    if overlap:
        problems.append(f"factor(s) both swept and fixed: {', '.join(sorted(overlap))}")

    if campaign.repetitions < 1:
        problems.append(f"repetitions must be >= 1 (got {campaign.repetitions})")

    # Step timing within a runner invocation (2026-11-04): first | repeat | always.
    problems += _when_problems("operator_steps", campaign.operator_steps)
    for i, st in enumerate(campaign.setup_steps):
        if "_invalid" in st:
            problems.append(f"setup_steps[{i}] must be a mapping with a non-empty command "
                            f"({{name, command, required, when}}), got {st['_invalid']!r}")
    problems += _when_problems("setup_steps", [st for st in campaign.setup_steps
                                               if "_invalid" not in st])
    if not isinstance(campaign.robot_reset, bool):
        problems.append(f"robot_reset must be true or false, got {campaign.robot_reset!r}")
    if any(st.get("when") not in (None, "always") for st in campaign.operator_live_steps):
        problems.append("operator_live_steps take no `when`: a live step is part of the "
                        "flight itself, so it applies to every trial")
    problems += _order_problems(campaign.order)
    problems += _repetitions_where_problems(campaign)
    if campaign.factor_checks not in FACTOR_CHECK_MODES:
        problems.append(f"factor_checks must be one of {', '.join(FACTOR_CHECK_MODES)} "
                        f"(got {campaign.factor_checks!r})")
    problems += voice_problems(campaign.voice_cues, campaign.voice_announce)
    return problems


def _voice_text_problem(where: str, text: Any) -> Optional[str]:
    from .operator_voice import MAX_CHARS
    if not isinstance(text, str) or not text.strip():
        return f"{where}: text must be a non-empty string, got {text!r}"
    if len(text.strip()) > MAX_CHARS:
        return (f"{where}: text is {len(text.strip())} characters, the voice channel speaks at "
                f"most {MAX_CHARS} (split it into two cues)")
    return None


def voice_problems(voice_cues: Any, voice_announce: Any) -> List[str]:
    """What is wrong with a campaign's ``voice_cues`` and ``voice_announce``.

    ``voice_cues`` is a list of ``{at_s, text}``: ``at_s`` a number of seconds
    >= 0 after the recording start (any order, the runner sorts them), ``text``
    a non-empty string of at most operator_voice.MAX_CHARS characters.
    ``voice_announce`` is absent, ``false``, or a mapping of
    VOICE_ANNOUNCE_KEYS to a text, ``null`` or ``""``.
    """
    import math
    problems: List[str] = []
    if not isinstance(voice_cues, list):
        return [f"voice_cues must be a list of {{at_s, text}}, got {voice_cues!r}"]
    for i, entry in enumerate(voice_cues):
        where = f"voice_cues[{i}]"
        if not isinstance(entry, dict) or "_invalid" in entry:
            bad = entry.get("_invalid") if isinstance(entry, dict) else entry
            problems.append(f"voice_cues must be a list of {{at_s, text}}, got {bad!r}"
                            if isinstance(entry, dict) else
                            f"{where} must be a mapping {{at_s, text}}, got {entry!r}")
            continue
        unknown = sorted(set(entry) - set(VOICE_CUE_KEYS))
        if unknown:
            problems.append(f"{where}: unknown key(s) {', '.join(map(str, unknown))} "
                            f"(known: {', '.join(VOICE_CUE_KEYS)})")
        at_s = entry.get("at_s")
        if (isinstance(at_s, bool) or not isinstance(at_s, (int, float))
                or not math.isfinite(at_s) or at_s < 0):
            problems.append(f"{where}: at_s must be a number of seconds >= 0 after the "
                            f"recording start, got {at_s!r}")
        text_problem = _voice_text_problem(where, entry.get("text"))
        if text_problem:
            problems.append(text_problem)
    if voice_announce is None or voice_announce is False:
        return problems
    if not isinstance(voice_announce, dict):
        problems.append(f"voice_announce must be false or a mapping of "
                        f"{', '.join(VOICE_ANNOUNCE_KEYS)} to a text, got {voice_announce!r}")
        return problems
    unknown = sorted(set(map(str, voice_announce)) - set(VOICE_ANNOUNCE_KEYS))
    if unknown:
        problems.append(f"voice_announce: unknown moment(s) {', '.join(unknown)} "
                        f"(known: {', '.join(VOICE_ANNOUNCE_KEYS)})")
    for key, text in voice_announce.items():
        if text is None or text is False or text == "":
            continue
        text_problem = _voice_text_problem(f"voice_announce.{key}", text)
        if text_problem:
            problems.append(text_problem)
    return problems


def _order_problems(order: Dict[str, Any]) -> List[str]:
    if not order:
        return []
    if "_invalid" in order:
        return [f"order must be a mapping {{shuffle, seed, blocks}}, got {order['_invalid']!r}"]
    problems = []
    unknown = sorted(set(order) - {"shuffle", "seed", "blocks"})
    if unknown:
        problems.append(f"order: unknown key(s) {', '.join(unknown)} (known: shuffle, seed, blocks)")
    shuffle = order.get("shuffle", False)
    if not isinstance(shuffle, bool):
        problems.append(f"order.shuffle must be true or false, got {shuffle!r}")
    seed = order.get("seed")
    if shuffle is True and (not isinstance(seed, int) or isinstance(seed, bool)):
        # A shuffle without a recorded seed is an order nobody can reproduce,
        # which is the operator-dependent randomization this replaces.
        problems.append(f"order.shuffle needs an integer order.seed, got {seed!r}")
    blocks = order.get("blocks", False)
    if not (isinstance(blocks, bool) or (isinstance(blocks, int) and blocks >= 1)):
        problems.append(f"order.blocks must be true, false or a positive integer, got {blocks!r}")
    return problems


def _repetitions_where_problems(campaign: Campaign) -> List[str]:
    problems = []
    for i, entry in enumerate(campaign.repetitions_where):
        if not isinstance(entry, dict) or "_invalid" in entry:
            problems.append(f"repetitions_where must be a list of {{where, repetitions}}")
            continue
        where, reps = entry.get("where"), entry.get("repetitions")
        if not isinstance(where, dict) or not where:
            problems.append(f"repetitions_where[{i}]: 'where' must be a non-empty mapping")
            continue
        if not isinstance(reps, int) or isinstance(reps, bool) or reps < 1:
            problems.append(f"repetitions_where[{i}]: repetitions must be an integer >= 1")
        for name, level in where.items():
            factor = campaign.factors.get(name)
            if factor is None:
                problems.append(f"repetitions_where[{i}]: unknown factor {name!r}")
            elif level not in factor.levels:
                problems.append(f"repetitions_where[{i}]: {name}={level!r} is not a declared "
                                f"level ({factor.levels})")
    return problems


def validate_against_metadata_contract(campaign: Campaign,
                                       experiments_root: Optional[Path] = None) -> List[str]:
    """Every swept factor must actually be recorded, or its coverage is unmeasurable.

    This is the loop-closing check: factors -> recorded metadata -> coverage
    axes. A campaign that sweeps something the trial never writes down produces
    bags whose grid silently collapses that axis, which is worse than not
    sweeping it — the data looks like coverage and is not.
    """
    problems: List[str] = []
    try:
        from .trial_metadata_schema import load_schema
        schema = load_schema(campaign.experiment, experiments_root)
    except Exception as exc:  # noqa: BLE001
        return [f"could not load the metadata contract: {exc}"]

    for name in sorted(campaign.sweep):
        factor = campaign.factors.get(name)
        if factor is None:
            continue
        key = factor.metadata_key
        if not key:
            problems.append(f"factor {name!r} is swept but declares no metadata_key — "
                            f"its coverage could never be measured")
        elif key not in schema.fields:
            problems.append(f"factor {name!r} records to {key!r}, which the metadata "
                            f"contract does not declare — add it as a coverage axis "
                            f"(factor: true) or the coverage index cannot group by it")
        elif not schema.fields[key].factor:
            problems.append(f"factor {name!r} records to {key!r}, which the contract "
                            f"declares but not as a coverage axis (`factor: true`) — the "
                            f"coverage index will not group by it")
    return problems


#: Marker of a contract refusal, so callers and tests can tell it apart.
CONTRACT_REFUSAL = "REFUSED BY THE METADATA CONTRACT"


def contract_refusal(campaign: Campaign,
                     experiments_root: Optional[Path] = None) -> Optional[str]:
    """The refusal message when a campaign fails the metadata contract, else None.

    A hard refusal since 2026-10-02 (it printed a warning and exited 0 before,
    while the guide and the paper said "refused"). Two cases, both named:

    * a swept factor with no ``metadata_key``: the planner cannot tell its
      levels apart, merges them into one cell, and every trial flies at the
      first level while the campaign claims the sweep (mutant D_P3 of the E3
      study, experiments/llm_design_study/gold/broken/);
    * a swept factor whose key the contract does not declare as
      ``factor: true``: its coverage can never be measured.

    ``load_campaign`` itself is unchanged; every surface that RUNS or PLANS a
    campaign (campaign_spec, campaign_plan, trial_runner and run_plan, which
    the web UI uses) calls this and refuses.
    """
    problems = validate_against_metadata_contract(campaign, experiments_root)
    if not problems:
        return None
    return (f"{CONTRACT_REFUSAL}: campaign {campaign.name!r} sweeps factor(s) whose "
            f"coverage cannot be measured:\n  " + "\n  ".join(problems)
            + "\nRecord the factor under a metadata_key that the metadata contract "
              "declares as a coverage axis (`factor: true`), or do not sweep it.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("campaign", nargs="?", type=Path, help="path to a campaign YAML")
    roots.add_root_arguments(ap)
    ap.add_argument("--factors", metavar="EXPERIMENT",
                    help="list the factor space for an experiment and exit")
    ap.add_argument("--set", action="append", default=[], metavar="name=a[,b]",
                    help="override a factor (Hydra-shaped); repeatable")
    ap.add_argument("--limit", type=int, default=20, help="max trials to print")
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)

    if args.factors:
        factors = load_factors(args.factors, args.experiments_root)
        print(f"Factor space for {args.factors} ({len(factors)} axes):")
        for name, factor in sorted(factors.items()):
            kind = factor.maps_to.get("kind", "internal")
            print(f"\n  {name}  [{kind}]  default={factor.default!r}")
            print(f"      levels: {', '.join(str(x) for x in factor.levels)}")
            for level, reason in sorted(factor.blocked.items()):
                print(f"      ⛔ {level} is BLOCKED: {reason.splitlines()[0]}")
            if factor.describe:
                print(f"      {factor.describe.splitlines()[0]}")
        return 0

    if not args.campaign:
        ap.error("give a campaign file, or --factors <experiment>")

    try:
        campaign = load_campaign(args.campaign, args.experiments_root, args.set)
    except SpecError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    refusal = contract_refusal(campaign, args.experiments_root)
    if refusal:
        print(f"ERROR: {refusal}", file=sys.stderr)
        return 2
    warnings: List[str] = []
    print(f"Campaign: {campaign.name}  ({campaign.experiment})")
    if campaign.describe:
        print(f"  {campaign.describe}")
    print(f"  {campaign.size()} trial(s) = "
          f"{' x '.join(f'{len(v)} {k}' for k, v in sorted(campaign.sweep.items())) or '1 combination'}"
          f" x {campaign.repetitions} rep(s)"
          + (" (per-cell counts in repetitions_where)" if campaign.repetitions_where else ""))
    if campaign.order:
        print(f"  order: {campaign.order}")
    if campaign.voice_cues:
        times = sorted(float(c["at_s"]) for c in campaign.voice_cues)
        print(f"  voice cues: {len(times)}, from +{times[0]:.1f} s to +{times[-1]:.1f} s "
              f"after the recording start")
    if warnings:
        print("\n  ⚠ coverage warnings:")
        for warning in warnings:
            print(f"      {warning}")
    print("\n  (* marks a non-default value)")
    for i, trial in enumerate(campaign.combinations()):
        if i >= args.limit:
            print(f"    … and {campaign.size() - args.limit} more")
            break
        print(f"    [{trial.repetition}] {trial.describe()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
