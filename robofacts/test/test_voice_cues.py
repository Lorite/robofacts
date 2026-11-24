"""Spoken cues in campaigns and in the trial runner (2026-10-13).

Covers the campaign key's validation (campaign_spec.voice_problems and lint L15), the
runner's voice plan (defaults, overrides, placeholders), and the runner's cue scheduling
through a real run_trial with fake process hooks and a fake sender. No ROS, no audio.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent

from robofacts import campaign_spec as cs
from robofacts import operator_voice as ov
from robofacts import trial_runner as tr
CORE = """
version: 1
factors:
  execution_host:
    levels: [laptop]
    default: laptop
    metadata_key: execution_host
    maps_to: {kind: host}
  record_profile:
    levels: [position_only]
    default: position_only
    metadata_key: record_profile
    maps_to: {kind: record_profile}
  trajectory:
    levels: [hover, circle]
    default: hover
    metadata_key: trajectory
    maps_to: {kind: launch_arg, name: trajectory}
"""

CONTRACT = """
version: 1
fields:
  trial_id: {required: true, type: str}
  execution_host: {required: true, type: str, factor: true}
  record_profile: {required: false, type: str, factor: true}
  trajectory: {required: false, type: str, factor: true}
  aborted: {required: true, type: bool}
"""

RUNNER = """
simulation: false
launch:
  package: lorite_experiments
  file: flight.launch.py
action:
  command: "python3 fly.py --shape {trajectory}"
settle_s: 0
stop_grace_s: 0
cf_name: cf1
hosts:
  laptop: {local: true}
"""

HANDHELD_CUES = """
voice_cues:
  - {at_s: 0, text: "Recording. Keep still."}
  - {at_s: 8, text: "Pick up and walk."}
  - {at_s: 88, text: "Back on the stand."}
  - {at_s: 101, text: "Done. Thank you."}
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(CORE, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT,
                                                                              encoding="utf-8")
    (tmp_path / "common" / "config" / "record_profiles.yaml").write_text(
        "profiles: {position_only: {topics: [/poses, /tf, /rosout]}}\n", encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "config" / "runner.yaml").write_text(RUNNER, encoding="utf-8")
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    return tmp_path


def campaign(root: Path, body: str, name: str = "c") -> Path:
    path = root / "expA" / "campaigns" / f"{name}.yaml"
    path.write_text(f"name: {name}\nexperiment: expA\nsweep:\n  trajectory: [hover]\n{body}",
                    encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Validation: campaign_spec and lint L15
# --------------------------------------------------------------------------- #
def test_a_valid_timeline_loads_in_any_order(root):
    path = campaign(root, "voice_cues:\n  - {at_s: 8, text: walk}\n  - {at_s: 0, text: still}\n"
                          "voice_announce: {before_launch: 'Put the drone on the stand.', "
                          "record_end: null}\n")
    loaded = cs.load_campaign(path, root)
    assert [c["at_s"] for c in loaded.voice_cues] == [8, 0]
    assert loaded.voice_announce["record_end"] is None


def test_a_campaign_without_voice_is_unchanged(root):
    loaded = cs.load_campaign(campaign(root, ""), root)
    assert loaded.voice_cues == [] and loaded.voice_announce is None


@pytest.mark.parametrize("body, needle", [
    ("voice_cues:\n  - {at_s: -1, text: x}\n", "at_s must be a number"),
    ("voice_cues:\n  - {at_s: 'soon', text: x}\n", "at_s must be a number"),
    ("voice_cues:\n  - {at_s: true, text: x}\n", "at_s must be a number"),
    ("voice_cues:\n  - {at_s: .nan, text: x}\n", "at_s must be a number"),
    ("voice_cues:\n  - {at_s: 1}\n", "text must be a non-empty string"),
    ("voice_cues:\n  - {at_s: 1, text: '   '}\n", "text must be a non-empty string"),
    ("voice_cues:\n  - {at_s: 1, text: " + "w" * 201 + "}\n", "the voice channel speaks at most"),
    ("voice_cues:\n  - {at_s: 1, text: x, when: later}\n", "unknown key(s) when"),
    ("voice_cues:\n  - just a string\n", "must be a mapping"),
    ("voice_cues: {at_s: 1, text: x}\n", "voice_cues must be a list"),
    ("voice_announce: yes\n", "voice_announce must be false or a mapping"),
    ("voice_announce: {record_begin: Recording.}\n", "unknown moment(s) record_begin"),
    ("voice_announce: {record_start: 5}\n", "text must be a non-empty string"),
])
def test_malformed_voice_entries_are_refused(root, body, needle):
    problems = cs.campaign_problems(campaign(root, body), root)
    assert any(needle in p for p in problems), problems
    with pytest.raises(cs.SpecError):
        cs.load_campaign(campaign(root, body), root)


def test_voice_announce_false_is_allowed(root):
    assert cs.campaign_problems(campaign(root, "voice_announce: false\n"), root) == []


def test_lint_l15_refuses_unknown_placeholders_and_cues_past_the_deadline(root):
    factors = cs.load_factors("expA", root)
    path = campaign(root, "voice_cues:\n  - {at_s: 0, text: 'Shape {trajectory}.'}\n"
                          "  - {at_s: 5, text: 'Battery {battery}.'}\n"
                          "  - {at_s: 120, text: 'Done.'}\n")
    # Imported here, not at the top: lint resolves the experiments root at import, and
    # the example project's root is set by a fixture, which runs after collection. A
    # top-level import failed collection outside a project (the package's CI, 2026-10-15).
    from robofacts import lint
    problems = lint.voice_lint_problems(path, factors, action_timeout_s=100)
    assert any("{battery}" in p for p in problems)
    assert not any("{trajectory}" in p for p in problems)
    assert any("voice_cues[2] at 120" in p for p in problems)
    assert lint.voice_lint_problems(campaign(root, "", "plain"), factors, 100) == []


# --------------------------------------------------------------------------- #
# The voice plan
# --------------------------------------------------------------------------- #
def test_a_hardware_experiment_announces_by_default_and_a_simulation_does_not():
    hw = tr.voice_plan(None, None, simulation=False)
    assert hw["announce"]["record_start"] == "Recording."
    assert hw["announce"]["record_end"] == "Recording stopped."
    assert hw["announce"]["gate_blocked"] and hw["announce"]["before_launch"] is None
    sim = tr.voice_plan(None, None, simulation=True)
    assert not any(sim["announce"].values()) and sim["cues"] == []
    assert tr.voice_plan_lines(sim) == []


def test_voice_announce_false_silences_every_moment():
    plan = tr.voice_plan(None, False, simulation=False)
    assert not any(plan["announce"].values())


def test_a_cue_at_zero_replaces_the_default_record_start_but_not_an_explicit_one():
    cues = [{"at_s": 0, "text": "Recording. Keep still."}]
    assert tr.voice_plan(cues, None, simulation=False)["announce"]["record_start"] is None
    kept = tr.voice_plan(cues, {"record_start": "Go."}, simulation=False)
    assert kept["announce"]["record_start"] == "Go."


def test_a_simulation_campaign_can_opt_in_and_placeholders_are_filled():
    plan = tr.voice_plan([{"at_s": 3, "text": "Fly the {trajectory}."}],
                         {"record_end": "Done with {trajectory}.", "gate_blocked": None},
                         simulation=True, values={"trajectory": "circle"})
    assert [(c.at_s, c.text) for c in plan["cues"]] == [(3.0, "Fly the circle.")]
    assert plan["announce"]["record_end"] == "Done with circle."
    assert plan["announce"]["record_start"] is None and plan["announce"]["gate_blocked"] is None


# --------------------------------------------------------------------------- #
# The runner: dry run prints, a real run speaks on time, a dead daemon costs nothing
# --------------------------------------------------------------------------- #
class FakeHooks:
    """The three process primitives. The action takes ``action_s`` of real time."""

    def __init__(self, action_s: float = 0.0, events=None):
        self.action_s = action_s
        self.events = events if events is not None else []

    def hooks(self):
        return tr.ProcessHooks(start_background=self._start, run_foreground=self._fg,
                               stop_process=self._stop)

    def _start(self, cmd, target, *, cwd, label="", quiet_sink=None):
        self.events.append(("start", label))
        if "record" in cmd and "-o" in cmd:
            bag = Path(cmd[cmd.index("-o") + 1])
            bag.mkdir(parents=True, exist_ok=True)
            (bag / "metadata.yaml").write_text("rosbag2_bagfile_information:\n")
            (bag / f"{bag.name}_0.mcap").write_bytes(b"\x89MCAP0\r\n")
        return object()

    def _fg(self, cmd, target, *, cwd, check=True):
        if cmd and cmd[0] == "python3":
            self.events.append(("action_start", None))
            threading.Event().wait(self.action_s)
            self.events.append(("action_end", None))
        return SimpleNamespace(returncode=0, stdout="")

    def _stop(self, proc, target, *, name, kind):
        self.events.append(("stop", name))


@pytest.fixture
def no_sleep(monkeypatch):
    """The runner's fixed sleeps (settle, the recorder's 1 s) cost nothing here."""
    monkeypatch.setattr(tr.time, "sleep", lambda s: None)


def _pieces(root):
    factors = cs.load_factors("expA", root)
    config = tr.RunnerConfig.load("expA", root)
    trial = cs.TrialSpec("expA", {"execution_host": "laptop", "record_profile": "position_only",
                                  "trajectory": "circle"}, ["trajectory"], 1)
    return factors, config, trial


@pytest.mark.ros2
def test_a_dry_run_prints_the_plan_and_sends_nothing(root, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(ov, "send", lambda text, **kw: sent.append(text) or True)
    factors, config, trial = _pieces(root)
    cues = [{"at_s": 8, "text": "Pick up and walk."}, {"at_s": 0, "text": "Keep still."}]
    result = tr.run_trial(trial, factors, config, root / "expA" / "bags", dry_run=True,
                          hooks=FakeHooks().hooks(), voice_cues=cues,
                          voice_announce={"before_launch": "Drone on the stand."})
    out = capsys.readouterr().out
    assert result.status == "dry-run" and sent == []
    assert "voice plan (dry run, nothing is sent" in out
    assert out.index("before_launch: Drone on the stand.") < out.index("+0.0 s: Keep still.") \
        < out.index("+8.0 s: Pick up and walk.") < out.index("record_end: Recording stopped.")


@pytest.mark.ros2
def test_the_runner_speaks_the_timeline_in_order_and_announces_around_the_recording(
        root, monkeypatch, no_sleep):
    events = []
    monkeypatch.setattr(ov, "send", lambda text, **kw: events.append(("voice", text)) or True)
    factors, config, trial = _pieces(root)
    fake = FakeHooks(action_s=0.6, events=events)
    cues = [{"at_s": 0.3, "text": "Pick up and walk."}, {"at_s": 0, "text": "Keep still."},
            {"at_s": 30, "text": "Never reached."}]
    result = tr.run_trial(trial, factors, config, root / "expA" / "bags", dry_run=False,
                          hooks=fake.hooks(), slim=False, voice_cues=cues,
                          voice_announce={"before_launch": "Drone on the stand."})
    assert result.status == "ran", result.detail
    order = [e for e in events if e[0] in ("voice", "action_start", "action_end")
             or e == ("start", "recorder") or e == ("stop", "record")]
    voice_lines = [e[1] for e in order if e[0] == "voice"]
    assert voice_lines == ["Drone on the stand.", "Keep still.", "Pick up and walk.",
                           "Recording stopped."], voice_lines
    # before_launch precedes the recorder, the timeline runs during the action, and the
    # end line comes after the recorder was stopped.
    assert order.index(("voice", "Drone on the stand.")) < order.index(("start", "recorder"))
    assert order.index(("voice", "Pick up and walk.")) < order.index(("action_end", None))
    assert order.index(("stop", "record")) < order.index(("voice", "Recording stopped."))
    ledger = [json.loads(l) for l in
              (root / "expA" / "runs" / "attempts.jsonl").read_text().splitlines()]
    cues_logged = ledger[-1]["voice"]["cues"]
    assert [c["text"] for c in cues_logged if c["spoken"]] == ["Keep still.", "Pick up and walk."]
    assert [c["text"] for c in cues_logged if not c["spoken"]] == ["Never reached."]


@pytest.mark.ros2
def test_a_dead_daemon_does_not_delay_or_break_the_trial(root, monkeypatch, no_sleep):
    """The real sender, forced on, aimed at a port where nobody listens."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    monkeypatch.setenv(ov.ENV_SWITCH, "on")
    monkeypatch.setenv(ov.ENV_HOST, "127.0.0.1")
    monkeypatch.setenv(ov.ENV_PORT, str(dead_port))
    factors, config, trial = _pieces(root)
    cues = [{"at_s": i * 0.01, "text": f"cue {i}"} for i in range(20)]
    t0 = time.monotonic()
    result = tr.run_trial(trial, factors, config, root / "expA" / "bags", dry_run=False,
                          hooks=FakeHooks(action_s=0.4).hooks(), slim=False, voice_cues=cues)
    assert result.status == "ran", result.detail
    assert time.monotonic() - t0 < 3.0
    assert (result.bag_dir / "trial_metadata.json").is_file()


@pytest.mark.ros2
def test_voice_off_prints_the_cues_but_sends_nothing(root, monkeypatch, no_sleep, capsys):
    import socket
    monkeypatch.setenv(ov.ENV_SWITCH, "off")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    monkeypatch.setenv(ov.ENV_PORT, str(sock.getsockname()[1]))
    try:
        factors, config, trial = _pieces(root)
        tr.run_trial(trial, factors, config, root / "expA" / "bags", dry_run=False,
                     hooks=FakeHooks(action_s=0.2).hooks(), slim=False,
                     voice_cues=[{"at_s": 0, "text": "Keep still."}])
        assert "[voice] +0.0 s  Keep still." in capsys.readouterr().out
        sock.settimeout(0.2)
        with pytest.raises(socket.timeout):
            sock.recvfrom(4096)
    finally:
        sock.close()


@pytest.mark.ros2
def test_a_no_go_is_announced(root, monkeypatch, no_sleep):
    said = []
    monkeypatch.setattr(ov, "send", lambda text, **kw: said.append(text) or True)

    class NoGo:
        last_result = None

        def check(self, label, values=None):
            raise RuntimeError("battery low")

    factors, config, trial = _pieces(root)
    result = tr.run_trial(trial, factors, config, root / "expA" / "bags", dry_run=False,
                          hooks=FakeHooks().hooks(), slim=False, gate=NoGo())
    assert result.status == "blocked"
    assert said == [tr.DEFAULT_VOICE_ANNOUNCE["gate_blocked"]]


@pytest.mark.ros2
def test_run_plan_hands_the_campaign_voice_to_each_trial(root, monkeypatch):
    from robofacts import campaign_plan as cp
    seen = {}

    def fake_run_trial(spec, factors, config, bags_dir, **kwargs):
        seen.update(kwargs)
        return tr.TrialResult(spec, None, "dry-run")

    monkeypatch.setattr(tr, "run_trial", fake_run_trial)
    loaded = cs.load_campaign(campaign(root, HANDHELD_CUES + "voice_announce: false\n"), root)
    plan = cp.build_plan(loaded, [], False)
    tr.run_plan(plan, tr.RunnerConfig.load("expA", root), root / "expA" / "bags", dry_run=True)
    assert [c["at_s"] for c in seen["voice_cues"]] == [0, 8, 88, 101]
    assert seen["voice_announce"] is False


@pytest.mark.ros2
def test_a_go_is_announced(root, monkeypatch, no_sleep):
    """The operator stands at the drone: a passed gate is spoken too (2026-11-04)."""
    said = []
    monkeypatch.setattr(ov, "send", lambda text, **kw: said.append(text) or True)

    class Go:
        last_result = None

        def check(self, label, values=None):
            return None

    factors, config, trial = _pieces(root)
    tr.run_trial(trial, factors, config, root / "expA" / "bags", dry_run=False,
                 hooks=FakeHooks().hooks(), slim=False, gate=Go(), pause_before_action=False)
    assert "The gate passed." in said
    assert tr.DEFAULT_VOICE_ANNOUNCE["gate_blocked"] not in said


def test_gate_passed_is_a_campaign_announce_key():
    assert "gate_passed" in cs.VOICE_ANNOUNCE_KEYS
    plan = tr.voice_plan(None, {"gate_passed": None}, simulation=False)
    assert plan["announce"]["gate_passed"] is None
    plan = tr.voice_plan(None, None, simulation=False)
    assert plan["announce"]["gate_passed"] == tr.DEFAULT_VOICE_ANNOUNCE["gate_passed"]
