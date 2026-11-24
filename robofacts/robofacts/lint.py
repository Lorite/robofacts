#!/usr/bin/env python3
"""Lint EVERY experiment's declarative config against the lessons that cost flights.

Each rule here is a defect that actually happened, mostly on 2026-07-29, and
each was caught by a human noticing rather than by anything automatic. Prose in
AGENTS.md tells the next author what to do; this fails the build when they do
not — and, more importantly, it applies to experiments that do not exist yet.

The rules, and the incident behind each:

L1  level_values may only map declared levels.
L2  a launch_arg factor must name an argument the launch file declares.
    (`trajectory` was mapped to a launch_arg this launch does not have; ros2
    launch rejects unknown arguments, so every trial would have failed.)
L3  an `internal` factor must be CONSUMED by some command template, else it is
    recorded in the metadata but never applied. (`altitude` was recorded while
    the action still used the script's 0.5 m default, so the campaign claimed
    1.0 m and the bag was flown at 0.5 m.)
L4  every {placeholder} in a command must be a declared factor (or {bag} in a
    verify step or the action: the runner hands both the trial's bag
    directory, which is how a flight script knows where to write its
    flight-time commanded_waypoints.json). (`{cf_name_placeholder}` was
    written by hand and would have raised at trial time, i.e. on the rig.)
L4b (2026-11-04) the same for a campaign's own setup_steps (a reset step between
    repeated trials): {factor} or {arg_<launch argument>}, never {bag}, because
    setup runs before the trial has a bag. Its `when` must be first, repeat or
    always, and so must an operator step's.
L5  a campaign's pre-flight operator_steps must not start ROS processes: a
    trial's setup steps tear down the previous stack first (scripts/kill_ros.sh,
    which matches /opt/ros/ and any *_ws/install/, or an experiment's own stop
    script), so anything the operator starts beforehand is killed by the trial
    it was started for. Such things belong in aux_launches, or in
    operator_live_steps.
L6  every experiment must declare verify_steps in runner.yaml, or say why it has
    none with `no_verify_steps: <reason>`. An action's exit code describes a
    process, only the recording describes the trial. (A latched e-stop produced
    a "successful" trial with the robot stationary on the ground.) Until
    2026-11-24 the rule read flight words in the action and in the factor
    levels, so it bound only experiments that fly. A ground robot, an arm or a
    vehicle moved by hand can fail to move in the same way, and the lint cannot
    know from a declaration what moves, so the rule now binds every experiment
    and the exception is a written reason.
L7  a command may not reference a script that does not exist.
    L4 and L7 also cover the commands only Explore runs: `explore.viewers`
    (2026-10-17) and `explore.actions` (the run page's buttons).
L8  a campaign's preflight_checklist must exist and be coherent. A campaign may
    declare its own T-0 gate; naming one that is missing or malformed would fail
    at T-0 on the rig, with the stack up and the operator waiting.
L9  every record profile (and every record_profile factor level) must be a legal
    value of the metadata contract's enum. (a new replay profile was added to the
    profiles and to the factor on 2026-08-03 but not to the contract, which would
    have made every week-33 replay flight count as `unverified` rather than
    `usable` and had the campaign ask for flights that already happened.)
L10 every campaign must LOAD — or be refused only because it asks for a level
    the factor space declares `blocked:`. (`icra_budget_latency_ab` swept
    `latency_stamping: [off, on]` on a rig whose mocap path yields an age of
    0.49 ms, which truncates to 0, so both arms were bit-identical firmware. The
    factor is operator-applied, so nothing at the rig could refuse the trial and
    three bags labelled `on` would have read as coverage forever. A level whose
    apparatus does not exist is refused at plan time; this rule is what keeps
    that refusal a reviewed declaration instead of a broken file.)
L11 every record profile, and every explicit record_topics list, must record
    /rosout. On goTo flights the /rosout log is a recorder of the commanded
    reference itself (the swarm server logs every go_to call, and the
    2026-08-13 goto_tracking analysis reconstructs lateral tracking from it),
    besides being the trim-window and backfill source for every bag. A profile
    edit that drops it would silently make future flights unanalyzable, so the
    guard lives here rather than in prose.
L12 a factor of `maps_to.kind: gate` (levels that bind, shadow or disable the
    T-0 gate per trial, 2026-10-01, the SAC 2027 gate-effect experiment) is
    allowed only in an experiment whose runner.yaml declares
    `simulation: true`. A shadow or disabled gate lets a trial proceed past a
    NO-GO, which is a measurement in simulation and a hazard on a robot. The
    runner refuses it at run time as well, and this rule refuses it at review time.
    L4 also covers `metadata_probes` since the same date, because probes now
    receive the trial's placeholders.
L13 a factor of `maps_to.kind: operator` (a level the operator realizes by
    hand, such as the room's lighting, 2026-10-06, SAC 2027 framework paper)
    must declare a `check` (a preflight check with per-level arguments, added
    to every trial's T-0 gate) or an explicit `unchecked: <reason>`. Before
    this, a level nothing could verify (`latency_stamping`, `scene`) read as
    coverage on the word of whoever stood at the rig. In the paper's words: a
    usable trial can carry a *silent error* (the realized condition differs from
    the declared one) only in a factor that no check covers, so this rule makes
    every such factor visible. Every factor `check`
    must also name a check script that exists, and once two levels both carry
    numeric `min`/`max` bands, the bands may not overlap: a check that passes
    both levels at once cannot tell a mismatched trial from a correct one.
L14 every runner.yaml declares `simulation: true|false` explicitly (2026-10-06).
    Every trial records its `domain` (simulation or hardware) from that key,
    and the coverage view per condition counts the two domains apart. A
    missing key reads as hardware, which put an autopilot SITL demo's simulated
    trials in the hardware column until it was added.
L15 a campaign's spoken cues (`voice_cues`, `voice_announce`, 2026-10-13) are
    well formed (campaign_spec.voice_problems, also enforced by L10), every
    {placeholder} in their text is a declared factor, and no cue is planned at
    or after runner.yaml's `action_timeout_s`: the runner stops the timeline
    when the action ends, so such a cue would never be spoken, and the
    operator who waits for it would wait in vain.
L16 on a real robot (`simulation: false`), runner.yaml's `explore:` block may
    not open every parameter (2026-10-16, Explore mode). Its
    `param_whitelist` is what a Foxglove tab may change on a running stack, so
    a pattern such as `.*` would make every gain and every safety limit of the
    robot writable from a browser. Name each parameter you allow. In
    simulation the default is `['.*']` and this rule does not apply.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
import yaml


from . import campaign_spec as cs
from . import explore as ex
from . import trial_runner as tr
from . import roots
from .preflight import run_preflight

# Resolved once, at import: pytest builds the parametrized cases while it collects
# this module, so the root has to be known then (`trials lint` sets it before).
EXPERIMENTS_ROOT = roots.experiments_root()
REPO_ROOT = roots.project_root(experiments=EXPERIMENTS_ROOT)


def launch_dirs(package: str) -> list[Path]:
    """Where a source package's launch files may live under the project root."""
    return [REPO_ROOT / "ros2_ws" / "src" / package / "launch",
            REPO_ROOT / "src" / package / "launch",
            REPO_ROOT / package / "launch"]

PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


#: The trial's recording directory in a command: ``{recording}``, or ``{bag}``, the older name.
RECORDING_PLACEHOLDERS = ("bag", "recording")


def ros2_project() -> bool:
    """Is this project on the ROS 2 backend? Some rules are facts about ROS 2 (the launch
    arguments of a launch file, /rosout in every recording) and bind only there."""
    from . import backend
    return backend.configured_name(EXPERIMENTS_ROOT) == "ros2"
STARTS_ROS_PROCESS = re.compile(r"\bros2\s+(run|launch)\b")


# `trials lint --experiment <name>` (and `trials validate`) set this to lint ONE experiment.
# The rules that read shared files (the record profiles, the core factors) still run.
ENV_ONLY_EXPERIMENT = "TRIALS_LINT_EXPERIMENT"


def experiments_with_runner() -> list[str]:
    names = sorted(p.parent.parent.name
                   for p in EXPERIMENTS_ROOT.glob("*/config/runner.yaml"))
    only = os.environ.get(ENV_ONLY_EXPERIMENT, "")
    return [n for n in names if n == only] if only else names


def campaigns_of(experiment: str) -> list[Path]:
    return sorted((EXPERIMENTS_ROOT / experiment / "campaigns").glob("*.yaml"))


def declared_launch_args(launch_file: str, package: str = "") -> set[str] | None:
    """Arguments a launch file declares, or None if it cannot be read."""
    path = next((d / launch_file for d in launch_dirs(package) if (d / launch_file).is_file()), None)
    if path is None:
        return None
    src = path.read_text(encoding="utf-8")
    return set(re.findall(r"DeclareLaunchArgument\(\s*[\"']([a-zA-Z_][a-zA-Z0-9_]*)[\"']", src))


def command_templates(config) -> list[tuple[str, str]]:
    """(where, template) for every command the runner may issue."""
    out = [("action", config.action)] if config.action else []
    if (config.stack or {}).get("command"):
        # A backend whose stack is a templated command (the process backend): a factor it
        # names is consumed there, like in the action.
        out.append(("launch", config.stack["command"]))
    for group, steps in (("setup_steps", config.setup_steps),
                         ("aux_launches", config.aux_launches),
                         ("verify_steps", config.verify_steps)):
        out += [(f"{group}:{s['name']}", s["command"]) for s in steps]
    if config.livestream:
        out.append(("livestream", config.livestream))
    return out


def explore_templates(config) -> list[tuple[str, str]]:
    """(where, template) of the commands only Explore runs (explore.viewers and
    explore.actions). Kept apart from command_templates because a
    trial never runs them, so a factor they name is not thereby consumed (L3)."""
    block = config.explore or {}
    return [(f"explore.{group}:{v['name']}", v["command"])
            for group in ("viewers", "actions") for v in block.get(group, [])]


def probe_templates(config) -> list[tuple[str, str]]:
    """(where, template) of every metadata probe. Kept apart from
    command_templates because a probe READS a factor and never applies it, so
    it must not satisfy L3's "is the factor consumed" question."""
    return [(f"metadata_probes:{p['key']}", p["command"]) for p in config.metadata_probes]


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_level_values_map_declared_levels_only(experiment):
    """L1 — a level_values key that is not a level silently never applies."""
    for name, factor in cs.load_factors(experiment, EXPERIMENTS_ROOT).items():
        levels = {str(x) for x in factor.levels}
        for level in (factor.maps_to.get("level_values") or {}):
            assert str(level) in levels, (
                f"{experiment}: factor {name!r} maps level_values[{level!r}], which is not "
                f"one of its levels {sorted(levels)} — it would never be used")


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_launch_arg_factors_name_real_launch_arguments(experiment):
    """L2 — ros2 launch REJECTS an unknown argument, so this fails every trial."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    declared = declared_launch_args(config.launch_file, config.launch_package)
    if declared is None:
        pytest.skip(f"{config.launch_file} not found (external package?)")
    for name, factor in cs.load_factors(experiment, EXPERIMENTS_ROOT).items():
        if factor.maps_to.get("kind") != "launch_arg":
            continue
        targets = factor.maps_to.get("names") or [factor.maps_to.get("name")]
        for target in targets:
            assert target in declared, (
                f"{experiment}: factor {name!r} maps to launch argument {target!r}, which "
                f"{config.launch_file} does not declare — ros2 launch would reject it")


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_internal_factors_are_actually_consumed(experiment):
    """L3 — recorded but never applied is the worst kind of factor: the bag
    claims a setting the trial never had."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    used = set()
    for _, template in command_templates(config):
        used |= set(PLACEHOLDER.findall(template))
    for name, factor in cs.load_factors(experiment, EXPERIMENTS_ROOT).items():
        kind = factor.maps_to.get("kind", "internal")
        if kind != "internal" or name not in _swept_or_fixed(experiment):
            continue
        assert name in used or _documented_as_unapplied(factor), (
            f"{experiment}: factor {name!r} is `internal` and used by a campaign, but no "
            f"command template references {{{name}}} — it is recorded in trial_metadata "
            f"while the rig is never told about it. Either consume it, or say in its "
            f"`describe` that it is confirmed by the pre-flight rather than set")


def _swept_or_fixed(experiment: str) -> set[str]:
    names: set[str] = set()
    for path in campaigns_of(experiment):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        names |= set(raw.get("sweep") or {}) | set(raw.get("fixed") or {})
    return names


def _documented_as_unapplied(factor) -> bool:
    """An honest 'the runner cannot set this' note in the factor's describe."""
    text = factor.describe.lower()
    return any(k in text for k in ("cannot set", "not runner-set", "recorded, not",
                                   "confirms", "preflight", "pre-flight"))


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_command_placeholders_are_declared_factors(experiment):
    """L4 — an unknown placeholder raises at trial time, i.e. at the rig."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    all_factors = cs.load_factors(experiment, EXPERIMENTS_ROOT)
    factors = set(all_factors)
    # {arg_<name>} is the RESOLVED value of a launch argument some factor
    # drives — e.g. {arg_drone_rigidbody_name} is the mocap asset name that
    # `airframe` maps to, which the semantic level ("bare") cannot give.
    launch_args = set()
    for factor in all_factors.values():
        if factor.maps_to.get("kind") == "launch_arg":
            launch_args |= set(factor.maps_to.get("names") or [factor.maps_to.get("name")])
    for where, template in command_templates(config) + probe_templates(config) + explore_templates(config):
        for placeholder in PLACEHOLDER.findall(template):
            # A probe gets {bag} and {arg_*} like a verify step (2026-10-01).
            # Probes keep literal braces of their own (awk '{print $1}'), which
            # the runner leaves alone, so only identifiers that LOOK like
            # factor names are policed there.
            if where.startswith("metadata_probes") and placeholder in RECORDING_PLACEHOLDERS:
                continue
            # The runner hands {bag} to the verify steps, to the action
            # (trial_runner.action_command extra placeholders, 2026-08-13:
            # a flight script is told where the trial's bag lives so it can
            # write its flight-time commanded_waypoints.json beside it), and
            # since 2026-08-18 to the AUX LAUNCHES, so a side process that
            # records something about the trial can file it with the trial
            # (sample_process_resources.py).
            if placeholder in RECORDING_PLACEHOLDERS and (where == "action"
                                         or where.startswith("verify_steps")
                                         or where.startswith("aux_launches")):
                continue
            if placeholder.startswith("arg_"):
                target = placeholder[len("arg_"):]
                assert target in launch_args, (
                    f"{experiment}: {where} references {{{placeholder}}}, but no factor maps "
                    f"to a launch argument {target!r} — it would resolve to nothing")
                continue
            assert placeholder in factors, (
                f"{experiment}: {where} references {{{placeholder}}}, which is not a declared "
                f"factor — this raises when the trial runs, on the rig")


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_campaign_setup_step_placeholders_are_declared_factors(experiment):
    """L4b — a campaign's reset steps run on the rig between trials, so a bad placeholder
    or a bad `when` must fail here, not at the second trial of a lab day."""
    all_factors = cs.load_factors(experiment, EXPERIMENTS_ROOT)
    launch_args = set()
    for factor in all_factors.values():
        if factor.maps_to.get("kind") == "launch_arg":
            launch_args |= set(factor.maps_to.get("names") or [factor.maps_to.get("name")])
    for path in campaigns_of(experiment):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for i, step in enumerate(raw.get("setup_steps") or []):
            assert isinstance(step, dict) and str(step.get("command", "")).strip(), (
                f"{path.name}: setup_steps[{i}] needs a mapping with a non-empty command")
            assert str(step.get("when") or "always") in cs.STEP_WHEN, (
                f"{path.name}: setup_steps[{i}] when={step.get('when')!r} is not one of "
                f"{', '.join(cs.STEP_WHEN)}")
            for placeholder in PLACEHOLDER.findall(str(step["command"])):
                if placeholder.startswith("arg_"):
                    assert placeholder[len("arg_"):] in launch_args, (
                        f"{path.name}: setup_steps[{i}] references {{{placeholder}}}, but no "
                        f"factor maps to that launch argument")
                    continue
                assert placeholder in all_factors, (
                    f"{path.name}: setup_steps[{i}] references {{{placeholder}}}, which is not a "
                    f"declared factor ({{bag}} does not exist yet before the launch)")
        for i, step in enumerate(raw.get("operator_steps") or []):
            if isinstance(step, dict):
                assert str(step.get("when") or "always") in cs.STEP_WHEN, (
                    f"{path.name}: operator_steps[{i}] when={step.get('when')!r} is not one of "
                    f"{', '.join(cs.STEP_WHEN)}")


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_pre_flight_operator_steps_do_not_start_ros_processes(experiment):
    """L5: the setup steps' stack teardown would kill them. They belong in aux or live steps."""
    for path in campaigns_of(experiment):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for step in raw.get("operator_steps") or []:
            assert not STARTS_ROS_PROCESS.search(str(step)), (
                f"{path.name}: an operator_step starts a ROS process:\n  {str(step)[:120]}\n"
                f"A trial's setup steps tear down the previous stack before the launch "
                f"(scripts/kill_ros.sh matches /opt/ros/ and any *_ws/install/), so it would "
                f"be killed by the very trial it was started for. Put it in aux_launches "
                f"(the runner starts it) or operator_live_steps (started while the trial "
                f"runs)")


#: runner.yaml's way to say that an experiment has no verify step on purpose (L6).
NO_VERIFY_KEY = "no_verify_steps"


def verify_step_problem(raw: dict) -> str | None:
    """L6 on one runner.yaml (its parsed content): the problem, or None when it is fine.

    Fine means: at least one verify step, or no verify step and a written reason under
    ``no_verify_steps``. Both together contradict each other, and a reason that is not
    text (``true``, an empty string) is no reason.
    """
    steps = raw.get("verify_steps") or []
    declared = NO_VERIFY_KEY in raw
    reason = raw.get(NO_VERIFY_KEY)
    if steps and declared:
        return (f"declares verify_steps and {NO_VERIFY_KEY} together. Remove {NO_VERIFY_KEY}: "
                f"the experiment has verify steps")
    if steps:
        return None
    if declared:
        if isinstance(reason, str) and reason.strip():
            return None
        return (f"{NO_VERIFY_KEY} must be a sentence that says why no check of the recording "
                f"is possible or needed, got {reason!r}")
    return (f"declares no verify_steps. An action can exit 0 without the trial having happened "
            f"(a latched e-stop once produced a 'successful' trial with the robot stationary on "
            f"the ground for 48 s), so only a check of the recording shows that it did. Add a "
            f"verify step to runner.yaml (a command that reads {{bag}} and exits non-zero when "
            f"the trial did not happen), or say why there is none with "
            f"`{NO_VERIFY_KEY}: <reason>`")


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_every_experiment_declares_a_verify_step(experiment):
    """L6: an exit code describes a process, only the recording describes the trial."""
    path = EXPERIMENTS_ROOT / experiment / "config" / tr.RUNNER_CONFIG_NAME
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    problem = verify_step_problem(raw)
    assert problem is None, f"{experiment}: runner.yaml {problem}"


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_commands_reference_scripts_that_exist(experiment):
    """L7 — a typo'd path fails on the rig, after the stack is up."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    # A campaign's own setup steps (2026-11-04) run on the rig too.
    campaign_setup = [(f"{path.name}:setup_steps[{i}]", str(step.get("command", "")))
                      for path in campaigns_of(experiment)
                      for i, step in enumerate((yaml.safe_load(path.read_text(encoding="utf-8"))
                                                or {}).get("setup_steps") or [])
                      if isinstance(step, dict)]
    for where, template in command_templates(config) + explore_templates(config) + campaign_setup:
        # `ros2 launch <pkg> <file>.launch.py` names a launch file INSIDE a
        # package, resolved by ament at run time — not a path in this repo.
        launch_files = set(re.findall(r"ros2\s+launch\s+\S+\s+(\S+)", template))
        for token in re.findall(r"[\w./-]+\.(?:py|sh)", template):
            if token.startswith("/") or token in launch_files:
                continue  # absolute container paths / package-resolved launch files
            path = REPO_ROOT / token
            assert path.is_file(), (
                f"{experiment}: {where} references {token!r}, which does not exist at {path}")


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_campaign_preflight_checklists_exist_and_validate(experiment):
    """L8 — a campaign's own gate must be real, and coherent, before the rig sees it."""
    import json
    for path in campaigns_of(experiment):
        raw = yaml.safe_load(path.read_text()) or {}
        rel = str(raw.get("preflight_checklist") or "")
        if not rel:
            continue  # uses the experiment's checklist, covered by run_preflight --validate
        checklist = EXPERIMENTS_ROOT / experiment / rel
        assert checklist.is_file(), (
            f"{path.name}: preflight_checklist {rel!r} does not exist at {checklist}. "
            f"A campaign that names a missing gate fails at T-0, on the rig, with the "
            f"stack up.")
        try:
            data = json.loads(checklist.read_text())
        except json.JSONDecodeError as exc:
            pytest.fail(f"{path.name}: preflight_checklist {rel!r} is not valid JSON: {exc}")
        assert isinstance(data.get("checklist"), list) and data["checklist"], (
            f"{rel}: has no 'checklist' entries — an empty gate passes everything, which "
            f"is worse than having no gate because it looks like one.")


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_campaigns_load_or_only_ask_for_a_blocked_level(experiment):
    """L10 - a campaign is either runnable or deliberately parked, never broken.

    Two failures this separates, because they need opposite responses:

    * a MALFORMED campaign (unknown factor, undeclared level, a factor both
      swept and fixed) is a defect and fails the build here, where it costs
      seconds, rather than at T-0 with the stack up and the operator waiting;
    * a campaign asking for a level the factor space declares `blocked:` is a
      correct, reviewed statement that the apparatus cannot deliver it yet. It
      must still be refused by every surface that could run it, which
      `load_campaign` does, but it must NOT turn the whole suite red - a red
      suite is how the next person learns to ignore it.
    """
    for path in campaigns_of(experiment):
        problems = cs.campaign_problems(path, EXPERIMENTS_ROOT)
        unexpected = [p for p in problems if cs.BLOCKED_MARKER not in p]
        assert not unexpected, (
            f"{path.name} does not load:\n  " + "\n  ".join(unexpected) + "\n"
            f"Every surface that runs a campaign goes through load_campaign, so this "
            f"campaign fails at the rig, not here. Fix it, or - if a level it needs is "
            f"genuinely unrunnable - declare that level `blocked:` in the factor space "
            f"with the reason, which refuses it at plan time and keeps this test green.")


def test_every_record_profile_is_a_legal_metadata_level():
    """L9 - a record profile missing from the metadata contract invalidates every
    trial recorded on it.

    The incident: a replay profile landed on 2026-08-03 in
    record_profiles.PROFILES and in the record_profile FACTOR, but not in the
    contract's enum. Nothing failed, and the failure would have been silent and
    late: the week-33 flights would have recorded fine, then been counted
    `unverified` rather than `usable` by trial_index, so campaign_plan would
    report their cells as STILL TO FLY and the campaign would ask for flights
    that already happened. Found on 2026-08-04 by a test that happened to fly a
    replay trial through the runner, which is not a mechanism to rely on.

    Both directions are checked, because either gap has the same consequence:
    a profile the recorder can produce, and a level a campaign can pin.
    """
    from .record_profiles import profiles
    from .trial_metadata_schema import load_schema

    PROFILES = profiles(EXPERIMENTS_ROOT)

    enum = set(load_schema(None, EXPERIMENTS_ROOT).fields["record_profile"].enum or [])
    assert enum, "the contract must constrain record_profile, or it constrains nothing"

    missing_profiles = sorted(set(PROFILES) - enum)
    assert not missing_profiles, (
        f"record_profiles.PROFILES has {missing_profiles} which the metadata contract "
        f"(experiments/common/config/trial_metadata_core.yaml, record_profile.enum) does "
        f"not allow. A trial recorded on one of those fails the contract, so it is "
        f"counted `unverified` instead of `usable` and its campaign asks for it again.")

    factors = cs.load_factors(None, EXPERIMENTS_ROOT)
    factor = factors.get("record_profile")
    if factor is not None:
        missing_levels = sorted({str(lvl) for lvl in factor.levels} - enum)
        assert not missing_levels, (
            f"the record_profile factor offers levels {missing_levels} that the metadata "
            f"contract does not allow, so a campaign could pin a level whose every trial "
            f"is then unverifiable.")


def test_every_record_profile_records_rosout():
    """L11 - /rosout is a recorder of the commanded reference, not just a log.

    On goTo flights nothing streams a setpoint topic: the reference is
    reconstructed from the swarm server's go_to log on /rosout (the
    goto_tracking analysis, 2026-08-13), cross-checked against the flight-time
    commanded_waypoints.json. /rosout is also what the trim windows and the
    metadata backfill read. Today every profile inherits it from a composed
    topic group; this pins that so a future profile (or a group edit) cannot
    silently drop the channel and make every flight recorded on it
    unanalyzable.
    """
    from .record_profiles import build_topics, profiles

    if not ros2_project():
        pytest.skip("a rule of the ROS 2 backend")
    for profile in profiles(EXPERIMENTS_ROOT):
        topics = build_topics(profile, experiments_root=EXPERIMENTS_ROOT)
        assert "/rosout" in topics, (
            f"record profile {profile!r} does not record /rosout. That channel "
            f"carries the go_to log (the commanded reference of goTo flights), "
            f"the analysis trim events and the backfill evidence, so a bag "
            f"without it cannot be trimmed, backfilled, or scored for "
            f"tracking. Add /rosout (or a group that carries it) to the "
            f"profile.")


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_explicit_record_topics_record_rosout(experiment):
    """L11 (explicit-list form) - a runner.yaml that bypasses the named
    profiles with its own record_topics list must still record /rosout."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    if not ros2_project():
        pytest.skip("a rule of the ROS 2 backend")
    if not config.record_topics:
        pytest.skip("uses a named record profile (covered by the profile rule)")
    assert "/rosout" in config.record_topics, (
        f"{experiment}: config/runner.yaml record_topics does not include "
        f"/rosout. The trim windows, the metadata backfill and (for goTo "
        f"flights) the commanded reference all read it.")


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_gate_factors_only_in_simulated_experiments(experiment):
    """L12 - a gate that may be shadowed or disabled belongs in simulation only."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    gate_factors = cs.gate_factors(cs.load_factors(experiment, EXPERIMENTS_ROOT))
    if not gate_factors:
        pytest.skip("declares no gate factor")
    assert config.simulation is True, (
        f"{experiment}: factor(s) {gate_factors} are of kind `gate`, but "
        f"config/runner.yaml does not declare `simulation: true`. A `gate=off` "
        f"(shadow) or `gate=disabled` trial runs past a NO-GO, so the factor kind is "
        f"refused for anything that is not entirely simulated.")


def factor_files() -> list[Path]:
    """The core factor file and every experiment's own one (or only the linted one's)."""
    only = os.environ.get(ENV_ONLY_EXPERIMENT, "")
    return [EXPERIMENTS_ROOT / cs.CORE_FACTORS] + sorted(
        EXPERIMENTS_ROOT.glob((only or "*") + "/" + cs.PER_EXPERIMENT_FACTORS))


@pytest.mark.parametrize("path", factor_files(),
                         ids=lambda p: str(p.relative_to(EXPERIMENTS_ROOT)))
def test_operator_factors_declare_a_check_or_why_not(path):
    """L13 - a condition the operator realizes is checked, or says why it cannot be."""
    problems = []
    for name, factor in cs._load_factor_file(path).items():
        if factor.maps_to.get("kind") == "operator" and not (factor.check or factor.unchecked):
            problems.append(
                f"factor {name!r} is realized by the operator (`kind: operator`) but declares "
                f"neither `check:` (a gate check with per-level arguments, e.g. "
                f"image_brightness with a band per lighting level) nor `unchecked: <reason>`. "
                f"An unchecked level reads as coverage on the operator's word alone: a trial "
                f"that ran at another level would be a silent error")
        if factor.check and run_preflight.find_check(factor.check['script'], EXPERIMENTS_ROOT) is None:
            problems.append(f"factor {name!r} checks with {factor.check['script']!r}, "
                            f"which is not a script in "
                            f"{' or '.join(str(d) for d in run_preflight.check_dirs(EXPERIMENTS_ROOT))}")
        bands = {}
        for level, args in ((factor.check or {}).get("per_level") or {}).items():
            lo, hi = args.get("min"), args.get("max")
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                if lo > hi:
                    problems.append(f"factor {name!r} level {level!r} has min {lo} > max {hi}")
                bands[level] = (lo, hi)
        levels = list(bands)
        for i, a in enumerate(levels):
            for b in levels[i + 1:]:
                (alo, ahi), (blo, bhi) = bands[a], bands[b]
                if alo <= bhi and blo <= ahi:
                    problems.append(
                        f"factor {name!r}: the bands of {a!r} [{alo}, {ahi}] and {b!r} "
                        f"[{blo}, {bhi}] overlap, so the check cannot tell the two levels "
                        f"apart. Recalibrate, or declare the factor `unchecked` with the reason")
    assert not problems, f"{path}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_runner_declares_its_domain(experiment):
    """L14 - simulation: true|false is stated, never defaulted."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    assert config.simulation_declared, (
        f"{experiment}: config/runner.yaml does not declare `simulation:`. Every trial "
        f"records its domain from it (simulation or hardware), and a missing key reads "
        f"as hardware. Write `simulation: true` for simulated robots (or none at all), "
        f"`simulation: false` for real ones, and map a level that runs without hardware "
        f"(a swarm server backend: sim) with `domain:` in factors.yaml")


def voice_lint_problems(path: Path, factors: dict, action_timeout_s) -> list[str]:
    """L15 for one campaign file: well formed, fillable, inside the action's deadline."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cues, announce = raw.get("voice_cues"), raw.get("voice_announce")
    if cues is None and announce is None:
        return []
    problems = [f"{path.name}: {p}" for p in cs.voice_problems(
        cues if cues is not None else [], announce)]
    texts = [(f"voice_cues[{i}]", c.get("text")) for i, c in enumerate(cues or [])
             if isinstance(c, dict)] if isinstance(cues, list) else []
    if isinstance(announce, dict):
        texts += [(f"voice_announce.{k}", v) for k, v in announce.items()]
    for where, text in texts:
        for placeholder in PLACEHOLDER.findall(str(text or "")):
            if placeholder not in factors:
                problems.append(f"{path.name}: {where} names {{{placeholder}}}, which is not "
                                f"a declared factor, so it would be spoken literally")
    if action_timeout_s and isinstance(cues, list):
        for i, cue in enumerate(cues):
            at_s = cue.get("at_s") if isinstance(cue, dict) else None
            if (isinstance(at_s, (int, float)) and not isinstance(at_s, bool)
                    and at_s >= action_timeout_s):
                problems.append(f"{path.name}: voice_cues[{i}] at {at_s} s is at or after "
                                f"action_timeout_s={action_timeout_s:g} s, so it can never "
                                f"be spoken")
    return problems


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_voice_cues_are_speakable(experiment):
    """L15 - every planned cue is well formed, fillable and inside the action."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    factors = cs.load_factors(experiment)
    problems = []
    for path in campaigns_of(experiment):
        problems += voice_lint_problems(path, factors, config.action_timeout_s)
    assert not problems, "\n  ".join(problems)


@pytest.mark.parametrize("experiment", experiments_with_runner())
def test_explore_does_not_open_every_parameter_of_a_real_robot(experiment):
    """L16 - on hardware, each parameter Explore may change is named."""
    config = tr.RunnerConfig.load(experiment, EXPERIMENTS_ROOT)
    if config.simulation or not config.explore_declared:
        return
    wide = [p for p in config.explore.get("param_whitelist", []) if ex.matches_everything(p)]
    assert not wide, (
        f"{experiment}: explore.param_whitelist has {wide}, which matches every parameter. "
        f"On real robots Explore lets a Foxglove tab change these on the running stack, so "
        f"name the parameters you allow, one pattern each (for example '^/drone1/controller\\.kp$')")
