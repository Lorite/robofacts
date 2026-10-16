"""Framework changes for the SAC 2027 gate-effect experiment (E1), 2026-10-01.

C2 the attempt ledger, C3 `gate` as a factor kind, C4 a randomized campaign
order with a recorded seed (plus per-cell repetitions, which a block design
with a larger control cell needs), C6 metadata probes that receive the trial's
placeholders. No ROS: commands run through injected fakes, as in
test_trial_runner.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent

from robofacts import campaign_plan as cp
from robofacts import campaign_spec as cs
from robofacts import trial_runner as tr
from robofacts.preflight_gate import PreflightBlocked  # noqa: E402

CORE = """
version: 1
factors:
  execution_host:
    levels: [laptop]
    default: laptop
    metadata_key: execution_host
    maps_to: {kind: host}
"""

FACTORS = """
version: 1
factors:
  gate:
    levels: ["on", "off", "disabled"]
    default: "on"
    metadata_key: gate
    maps_to: {kind: gate}
  fault:
    levels: [none, a, b]
    default: none
    metadata_key: fault
    maps_to: {kind: internal}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  gate: {required: true, type: str, factor: true}
  fault: {required: true, type: str, factor: true}
  aborted: {required: true, type: bool}
"""

RUNNER = """
launch: {package: p, file: f.launch.py}
simulation: true
settle_s: 0
stop_grace_s: 0
record_topics: [/x, /rosout]
action: {command: "fly --fault {fault}"}
setup_steps:
  - {name: inject, command: "inject pre {fault}"}
metadata_probes:
  - {key: realized, command: "echo {fault} in {bag} awk '{print $1}'"}
hosts:
  laptop: {local: true}
"""

CAMPAIGN = """
name: ablation
experiment: expE
repetitions: 2
repetitions_where:
  - {where: {fault: none}, repetitions: 4}
order: {shuffle: true, seed: 7, blocks: true}
sweep:
  gate: ["on", "off"]
  fault: [none, a, b]
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(CORE)
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT)
    cfg = tmp_path / "expE" / "config"
    cfg.mkdir(parents=True)
    (cfg / "factors.yaml").write_text(FACTORS)
    (cfg / "runner.yaml").write_text(RUNNER)
    (tmp_path / "expE" / "campaigns").mkdir()
    (tmp_path / "expE" / "campaigns" / "ablation.yaml").write_text(CAMPAIGN)
    return tmp_path


def _campaign(root, text=None):
    path = root / "expE" / "campaigns" / "ablation.yaml"
    if text is not None:
        path.write_text(text)
    return cs.load_campaign(path, root)


class Hooks:
    def __init__(self):
        self.foreground = []

    def hooks(self):
        return tr.ProcessHooks(start_background=self._start, run_foreground=self._fg,
                               stop_process=lambda *a, **k: None)

    def _start(self, cmd, target, *, cwd, label="", quiet_sink=None):
        if "record" in cmd:
            bag = Path(cmd[cmd.index("-o") + 1])
            bag.mkdir(parents=True)
            (bag / "metadata.yaml").write_text("rosbag2_bagfile_information:\n")
            (bag / f"{bag.name}_0.mcap").write_bytes(b"\x89MCAP0\r\n")
        return object()

    def _fg(self, cmd, target, *, cwd, check=True):
        self.foreground.append(cmd)
        out = " ".join(cmd[2:]) if cmd[:2] == ["sh", "-c"] else ""
        return SimpleNamespace(returncode=0, stdout=out)


class ModalGate:
    """A gate whose checks always FAIL; the mode decides what that means."""

    def __init__(self, mode="binding"):
        self.mode, self.seen = mode, []
        self.last_result = None

    def with_mode(self, mode, reason=""):
        g = ModalGate(mode)
        g.seen = self.seen
        return g

    def check(self, label, values=None):
        self.seen.append((self.mode, values.get("gate")))
        r = SimpleNamespace(as_metadata=lambda: {"mode": self.mode, "verdict": "NO-GO",
                                                 "status": "fail"})
        self.last_result = r
        if self.mode == "binding":
            raise PreflightBlocked("NO-GO: topic_rate")
        return r


# --------------------------------------------------------------------------- #
# C3: the gate factor kind
# --------------------------------------------------------------------------- #
def test_a_gate_factor_loads_and_each_level_has_a_mode(root):
    factors = cs.load_factors("expE", root)
    assert cs.gate_factors(factors) == ["gate"]
    assert [cs.gate_mode(factors["gate"], l) for l in ("on", "off", "disabled")] == \
        ["binding", "shadow", "disabled"]


def test_an_unquoted_on_off_level_is_refused_with_the_fix(root):
    (root / "expE" / "config" / "factors.yaml").write_text(
        FACTORS.replace('["on", "off", "disabled"]', "[on, off]").replace('"on"\n', "on\n"))
    with pytest.raises(cs.SpecError, match="Quote it"):
        cs.load_factors("expE", root)


def test_a_level_without_a_mode_is_refused(root):
    (root / "expE" / "config" / "factors.yaml").write_text(
        FACTORS.replace('"disabled"]', '"half"]'))
    with pytest.raises(cs.SpecError, match="has no mode"):
        cs.load_factors("expE", root)


def test_explicit_modes_override_the_default_table(root):
    (root / "expE" / "config" / "factors.yaml").write_text(FACTORS.replace(
        "maps_to: {kind: gate}",
        "maps_to: {kind: gate, modes: {'on': binding, 'off': disabled, disabled: disabled}}"))
    factors = cs.load_factors("expE", root)
    assert cs.gate_mode(factors["gate"], "off") == "disabled"


def test_the_trial_level_selects_the_gate_mode(root):
    factors = cs.load_factors("expE", root)
    config = tr.RunnerConfig.load("expE", root)
    gate = ModalGate()
    for level, status in (("on", "blocked"), ("off", "ran")):
        trial = cs.TrialSpec("expE", {"execution_host": "laptop", "gate": level,
                                      "fault": "a"}, ["fault"], 1)
        result = tr.run_trial(trial, factors, config, root / "expE" / "bags", gate=gate,
                              dry_run=False, hooks=Hooks().hooks(), slim=False,
                              pause_before_action=False)
        assert result.status == status, (level, result.detail)
    assert gate.seen == [("binding", "on"), ("shadow", "off")]
    meta = json.loads(next((root / "expE" / "bags").glob("*/trial_metadata.json")).read_text())
    assert meta["gate"] == "off" and meta["preflight"]["mode"] == "shadow"


def test_a_gate_factor_is_refused_outside_simulation(root):
    (root / "expE" / "config" / "runner.yaml").write_text(
        RUNNER.replace("simulation: true", "simulation: false"))
    factors = cs.load_factors("expE", root)
    config = tr.RunnerConfig.load("expE", root)
    trial = cs.TrialSpec("expE", {"execution_host": "laptop", "gate": "off",
                                  "fault": "a"}, [], 1)
    fake = Hooks()
    result = tr.run_trial(trial, factors, config, root / "expE" / "bags",
                          gate=ModalGate(), dry_run=False, hooks=fake.hooks(), slim=False)
    assert result.status == "blocked" and "simulation: true" in result.detail
    assert fake.foreground == [], "nothing may run before the refusal"
    assert tr.gate_factor_problems(_campaign(root), config), "the CLI refuses it too"


def test_simulation_must_be_a_real_boolean(root):
    (root / "expE" / "config" / "runner.yaml").write_text(
        RUNNER.replace("simulation: true", "simulation: 'yes'"))
    with pytest.raises(tr.RunnerError, match="simulation must be true or false"):
        tr.RunnerConfig.load("expE", root)


def test_a_gate_that_cannot_change_mode_refuses_the_trial(root):
    class ShellGate:
        def check(self, label, values=None):
            return SimpleNamespace(as_metadata=lambda: {})
    factors = cs.load_factors("expE", root)
    config = tr.RunnerConfig.load("expE", root)
    trial = cs.TrialSpec("expE", {"execution_host": "laptop", "gate": "off",
                                  "fault": "a"}, [], 1)
    result = tr.run_trial(trial, factors, config, root / "expE" / "bags",
                          gate=ShellGate(), dry_run=False, hooks=Hooks().hooks(), slim=False)
    assert result.status == "blocked" and "cannot" in result.detail


def test_gate_override_and_a_gate_factor_do_not_mix(root):
    config = tr.RunnerConfig.load("expE", root)
    problems = tr.gate_factor_problems(_campaign(root), config, override_reason="x")
    assert any("--gate-override" in p for p in problems)


def test_a_blocked_attempt_does_not_stop_a_gate_factor_campaign(root):
    campaign = _campaign(root, CAMPAIGN.replace("repetitions_where:\n  - {where: {fault: none}, repetitions: 4}\n", "")
                         .replace("repetitions: 2", "repetitions: 1"))
    plan = cp.build_plan(campaign, [])
    config = tr.RunnerConfig.load("expE", root)
    results = tr.run_plan(plan, config, root / "expE" / "bags", gate=ModalGate(),
                          dry_run=False, hooks=Hooks().hooks(), slim=False,
                          pause_before_action=False)
    assert len(results) == 6
    assert sorted(r.status for r in results) == ["blocked"] * 3 + ["ran"] * 3


# --------------------------------------------------------------------------- #
# C2: the attempt ledger
# --------------------------------------------------------------------------- #
def _ledger(root):
    path = root / "expE" / "runs" / "attempts.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines()]


def test_every_executed_attempt_leaves_one_ledger_line(root):
    factors = cs.load_factors("expE", root)
    config = tr.RunnerConfig.load("expE", root)
    gate = ModalGate()
    for level in ("on", "off"):
        trial = cs.TrialSpec("expE", {"execution_host": "laptop", "gate": level,
                                      "fault": "b"}, [], 1)
        tr.run_trial(trial, factors, config, root / "expE" / "bags", gate=gate,
                     dry_run=False, hooks=Hooks().hooks(), slim=False,
                     pause_before_action=False, campaign_name="ablation", session_id="S1")
    blocked, ran = _ledger(root)
    assert blocked["status"] == "blocked" and blocked["metadata_written"] is False
    assert blocked["gate"]["mode"] == "binding" and blocked["gate"]["verdict"] == "NO-GO"
    assert blocked["trial_label"] and blocked["bag_dir"] is None
    assert ran["status"] == "ran" and ran["metadata_written"] is True
    assert ran["aborted"] is False and ran["gate"]["mode"] == "shadow"
    assert ran["values"]["fault"] == "b" and ran["campaign"] == "ablation"
    assert ran["session"] == "S1" and ran["action_window"] is not None
    for stage in ("start", "setup:inject", "launch", "gate_start", "gate_done",
                  "record", "action_start", "action_end", "teardown_done"):
        assert stage in ran["timings_s"], stage
    assert "record" not in blocked["timings_s"], "a NO-GO never reached the recorder"


def test_a_dry_run_writes_no_ledger_line(root):
    plan = cp.build_plan(_campaign(root), [])
    tr.run_plan(plan, tr.RunnerConfig.load("expE", root), root / "expE" / "bags",
                dry_run=True)
    assert not (root / "expE" / "runs" / "attempts.jsonl").exists()


def test_the_ledger_is_never_read_by_coverage(root):
    """Coverage stays faithful: the index reads bags, not the ledger."""
    from robofacts import trial_index as ti
    (root / "expE" / "runs").mkdir(parents=True)
    (root / "expE" / "runs" / "attempts.jsonl").write_text(
        json.dumps({"status": "ran", "values": {"gate": "on", "fault": "a"}}) + "\n")
    assert ti.load_trials(root, "expE") == []


# --------------------------------------------------------------------------- #
# C4: randomized complete blocks with a recorded seed, per-cell repetitions
# --------------------------------------------------------------------------- #
def test_repetitions_where_sets_the_control_cell_count(root):
    campaign = _campaign(root)
    plan = cp.build_plan(campaign, [])
    wanted = {(c.values["gate"], c.values["fault"]): c.wanted for c in plan.cells}
    assert wanted[("on", "none")] == 4 and wanted[("off", "a")] == 2
    assert campaign.size() == plan.wanted == 2 * (4 + 2 + 2)


def test_blocks_are_complete_and_the_order_is_reproducible(root):
    plan = cp.build_plan(_campaign(root), [])
    first = cp.execution_order(plan)
    again = cp.execution_order(cp.build_plan(_campaign(root), []))
    assert [(s.cell.describe(), s.repetition) for s in first] == \
        [(s.cell.describe(), s.repetition) for s in again], "same seed, same order"
    assert len(first) == plan.needed == 16
    blocks = {}
    for s in first:
        blocks.setdefault(s.order["block"], []).append(s)
    assert sorted(blocks) == [1, 2], "B = the smallest per-cell count (2)"
    for members in blocks.values():
        cells = [m.cell.describe() for m in members]
        assert len(set(cells)) == 6, "every block holds every cell"
        assert sum(1 for m in members if m.cell.values["fault"] == "none") == 4
    assert [s.order["position"] for s in first] == list(range(1, 17))
    assert all(s.order["seed"] == 7 for s in first)


def test_a_different_seed_gives_a_different_order(root):
    a = cp.execution_order(cp.build_plan(_campaign(root), []))
    b = cp.execution_order(cp.build_plan(_campaign(root, CAMPAIGN.replace("seed: 7", "seed: 8")), []))
    assert [s.cell.describe() for s in a] != [s.cell.describe() for s in b]


def test_without_order_the_runner_keeps_its_old_sequence(root):
    plain = CAMPAIGN.replace("order: {shuffle: true, seed: 7, blocks: true}\n", "")
    plan = cp.build_plan(_campaign(root, plain), [])
    sched = cp.execution_order(plan)
    expected = [(c.describe(), r) for c in plan.cells for r in range(1, c.needed() + 1)]
    assert [(s.cell.describe(), s.repetition) for s in sched] == expected
    assert all(s.order is None for s in sched)


def test_the_order_lands_in_the_trial_metadata(root):
    plan = cp.build_plan(_campaign(root, CAMPAIGN.replace(
        'gate: ["on", "off"]', 'gate: ["off"]')), [])
    config = tr.RunnerConfig.load("expE", root)
    results = tr.run_plan(plan, config, root / "expE" / "bags", gate=ModalGate(),
                          dry_run=False, hooks=Hooks().hooks(), slim=False, limit=1,
                          pause_before_action=False)
    meta = json.loads((results[0].bag_dir / "trial_metadata.json").read_text())
    assert meta["order"]["seed"] == 7 and meta["order"]["position"] == 1
    assert _ledger(root)[0]["order"]["block"] == 1


@pytest.mark.parametrize("bad, msg", [
    ("order: {shuffle: true}", "needs an integer order.seed"),
    ("order: {shuffle: yes, seed: 1, colour: red}", "unknown key"),
    ("order: {blocks: 0}", "positive integer"),
    ("order: [1, 2]", "must be a mapping"),
    ("repetitions_where:\n  - {where: {fault: zzz}, repetitions: 2}", "not a declared level"),
    ("repetitions_where:\n  - {where: {fault: a}, repetitions: 0}", "integer >= 1"),
])
def test_malformed_order_and_repetitions_are_refused(root, bad, msg):
    text = (CAMPAIGN.replace("order: {shuffle: true, seed: 7, blocks: true}\n", "")
            .replace("repetitions_where:\n  - {where: {fault: none}, repetitions: 4}\n", ""))
    with pytest.raises(cs.SpecError, match=msg):
        _campaign(root, text + bad + "\n")


# --------------------------------------------------------------------------- #
# C6: metadata probes receive the trial's placeholders
# --------------------------------------------------------------------------- #
def test_probes_get_the_trial_placeholders_and_keep_literal_braces(root):
    factors = cs.load_factors("expE", root)
    config = tr.RunnerConfig.load("expE", root)
    trial = cs.TrialSpec("expE", {"execution_host": "laptop", "gate": "off",
                                  "fault": "b"}, [], 1)
    result = tr.run_trial(trial, factors, config, root / "expE" / "bags", gate=ModalGate(),
                          dry_run=False, hooks=Hooks().hooks(), slim=False,
                          pause_before_action=False)
    meta = json.loads((result.bag_dir / "trial_metadata.json").read_text())
    # The fake hook echoes the command it was given, so this IS the rendered probe.
    assert meta["realized"] == f"echo b in {result.bag_dir} awk '{{print $1}}'"


def test_probes_without_placeholders_run_unchanged(tmp_path):
    seen = []
    hooks = tr.ProcessHooks(start_background=None, stop_process=None,
                            run_foreground=lambda cmd, t, *, cwd, check=True:
                            seen.append(cmd) or SimpleNamespace(returncode=0, stdout="v1"))
    out = tr.run_metadata_probes([{"key": "k", "command": "echo {fault}"}], hooks, None,
                                 tmp_path)
    assert out == {"k": "v1"} and seen == [["sh", "-c", "echo {fault}"]]


def test_the_gate_also_receives_the_resolved_launch_arguments(root):
    """arg_<name> reaches the gate (2026-10-02, E2's fleet check needs arg_symmetric_ns)."""
    (root / "expE" / "config" / "factors.yaml").write_text(FACTORS + """
  layout:
    levels: [sym]
    default: sym
    metadata_key: layout
    maps_to: {kind: launch_arg, names: [symmetric_ns], level_values: {sym: "true"}}
""")
    seen = {}

    class Gate(ModalGate):
        def with_mode(self, mode, reason=""):
            return self

        def check(self, label, values=None):
            seen.update(values)
            return SimpleNamespace(as_metadata=lambda: {"mode": self.mode})
    factors = cs.load_factors("expE", root)
    config = tr.RunnerConfig.load("expE", root)
    trial = cs.TrialSpec("expE", {"execution_host": "laptop", "gate": "off", "fault": "a",
                                  "layout": "sym"}, [], 1)
    tr.run_trial(trial, factors, config, root / "expE" / "bags", gate=Gate(), dry_run=False,
                 hooks=Hooks().hooks(), slim=False, pause_before_action=False)
    assert seen["arg_symmetric_ns"] == "true" and seen["layout"] == "sym"
