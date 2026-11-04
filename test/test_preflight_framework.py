"""Tests for the modular pre-flight check framework (#87).

The important one is ``test_every_experiment_checklist_is_coherent``: it is what
stops the machine gate and the human checklist from drifting apart. If someone
renames a check script or mistypes a ``check`` reference, that test fails here
instead of at T-0 on the flight line.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from robofacts import preflight_ui
from robofacts.preflight import run_preflight as rp

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parent
CHECKS_DIR = PACKAGE_ROOT / "robofacts" / "preflight" / "checks"
EXPERIMENTS_DIR = PACKAGE_ROOT / "example_project" / "experiments"

ALL_CHECKLISTS = sorted(EXPERIMENTS_DIR.glob("*/config/preflight_checklist.json"))


# --------------------------------------------------------------------------- #
# Anti-drift: the whole point of the framework
# --------------------------------------------------------------------------- #
def test_experiments_have_checklists():
    assert ALL_CHECKLISTS, "no experiment carries a config/preflight_checklist.json"


@pytest.mark.parametrize("checklist", ALL_CHECKLISTS, ids=lambda p: p.parents[1].name)
def test_every_experiment_checklist_is_coherent(checklist: Path):
    """Every 'check' reference resolves to an executable script, and every
    ${placeholder} used in its args is one the experiment's wrapper supplies."""
    entries = rp.load_entries(checklist)
    assert entries, f"{checklist} has no usable entries"

    # Variables come from the checklist's own "variables" block, so this test
    # needs no hardcoded list to fall out of date: a renamed or undeclared
    # ${placeholder} fails here instead of at T-0 on the flight line.
    problems = rp.validate(entries, rp.load_variables(checklist))
    assert problems == [], f"{checklist}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("checklist", ALL_CHECKLISTS, ids=lambda p: p.parents[1].name)
def test_declared_variables_are_all_used(checklist: Path):
    """An unused declared variable is a leftover from an edit - it means the
    check that referenced it was renamed or removed, so flag it."""
    declared = set(rp.load_variables(checklist))
    used = set()
    for entry in rp.load_entries(checklist):
        for value in entry["args"].values():
            used.update(rp._PLACEHOLDER.findall(value))
    assert declared >= used, f"{checklist}: undeclared {sorted(used - declared)}"
    assert not (declared - used), f"{checklist}: declared but unused {sorted(declared - used)}"


@pytest.mark.parametrize("script", sorted(CHECKS_DIR.glob("*.sh")), ids=lambda p: p.name)
def test_check_scripts_are_executable_and_valid_bash(script: Path):
    assert script.stat().st_mode & 0o111, f"{script.name} is not executable"
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, f"{script.name} has a syntax error:\n{result.stderr}"


# --------------------------------------------------------------------------- #
# Schema normalisation
# --------------------------------------------------------------------------- #
def test_plain_string_entries_are_manual(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"checklist": ["Prop guards installed", "  ", "Area clear"]}))
    entries = rp.load_entries(path)
    assert [e["text"] for e in entries] == ["Prop guards installed", "Area clear"]
    assert all(e["check"] is None for e in entries)


def test_object_entries_carry_check_and_args(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"checklist": [
        {"text": "[HARD] mocap streaming", "check": "mocap_stream", "args": {"min_hz": 85}},
    ]}))
    entry = rp.load_entries(path)[0]
    assert entry["check"] == "mocap_stream"
    assert entry["args"] == {"min_hz": "85"}  # coerced to strings for the CLI
    assert entry["hard"] is True


def test_bare_list_format_still_loads(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(["item one", "item two"]))
    assert len(rp.load_entries(path)) == 2


@pytest.mark.parametrize("payload,message", [
    ({"checklist": [{"check": "battery"}]}, "no 'text'"),
    ({"checklist": [{"text": "x", "args": []}]}, "non-object 'args'"),
    ({"checklist": [{"text": "x", "check": 3}]}, "non-string 'check'"),
    ({"nope": []}, "expected a list"),
])
def test_malformed_checklists_raise(tmp_path: Path, payload, message):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(rp.ChecklistError, match=message):
        rp.load_entries(path)


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(rp.ChecklistError, match="not found"):
        rp.load_entries(tmp_path / "absent.json")


# --------------------------------------------------------------------------- #
# Validation catches the mistakes that would only show up at T-0
# --------------------------------------------------------------------------- #
def test_unknown_check_is_reported():
    entries = [{"text": "x", "check": "does_not_exist", "args": {}, "hard": True}]
    problems = rp.validate(entries, {})
    assert len(problems) == 1 and "unknown check" in problems[0]


def test_unsupplied_placeholder_is_reported():
    entries = [{"text": "x", "check": "battery", "args": {"cf": "${drone}"}, "hard": True}]
    problems = rp.validate(entries, {})
    assert len(problems) == 1 and "${drone}" in problems[0]


def test_supplied_placeholder_is_accepted():
    entries = [{"text": "x", "check": "battery", "args": {"cf": "${cf}"}, "hard": True}]
    assert rp.validate(entries, {"cf": "crazylori_brushless_1"}) == []


def test_substitute_leaves_unknown_placeholders_intact():
    assert rp.substitute("/${cf}/status", {"cf": "abc"}) == "/abc/status"
    assert rp.substitute("/${other}/x", {"cf": "abc"}) == "/${other}/x"


# --------------------------------------------------------------------------- #
# Exit-code semantics: PASS / FAIL / SKIP, and unreadable == FAIL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code,text", [(0, "PASS: good"), (1, "FAIL: bad"), (3, "SKIP: n/a")])
def test_run_check_maps_exit_codes(tmp_path: Path, monkeypatch, code, text):
    fake = tmp_path / "fake_check.sh"
    fake.write_text(f"#!/usr/bin/env bash\necho '{text}'\nexit {code}\n")
    fake.chmod(0o755)
    monkeypatch.setattr(rp, "CHECKS_DIR", tmp_path)

    rc, detail = rp.run_check({"check": "fake_check", "args": {}}, {}, timeout_s=10)
    assert rc == code
    assert detail == text.split(": ", 1)[1]  # the PASS:/FAIL:/SKIP: prefix is stripped


def test_run_check_timeout_is_a_failure(tmp_path: Path, monkeypatch):
    slow = tmp_path / "slow.sh"
    slow.write_text("#!/usr/bin/env bash\nsleep 5\n")
    slow.chmod(0o755)
    monkeypatch.setattr(rp, "CHECKS_DIR", tmp_path)

    rc, detail = rp.run_check({"check": "slow", "args": {}}, {}, timeout_s=0.5)
    assert rc == rp.RC_FAIL and "timed out" in detail


def test_gate_fails_atomically(tmp_path: Path, monkeypatch, capsys):
    """One failing check must NO-GO the whole gate, even if others pass."""
    for name, code in (("good", 0), ("bad", 1)):
        script = tmp_path / f"{name}.sh"
        script.write_text(f"#!/usr/bin/env bash\necho '{name}'\nexit {code}\n")
        script.chmod(0o755)
    monkeypatch.setattr(rp, "CHECKS_DIR", tmp_path)

    checklist = tmp_path / "c.json"
    checklist.write_text(json.dumps({"checklist": [
        {"text": "a", "check": "good"},
        {"text": "b", "check": "bad"},
        "a manual item",
    ]}))

    rc = rp.main(["--checklist", str(checklist), "--no-daemon-bounce"])
    out = capsys.readouterr().out
    assert rc == rp.RC_FAIL
    assert "NO-GO" in out
    assert "a manual item" in out, "manual items must still be shown to the operator"


def test_all_pass_is_clear_to_arm(tmp_path: Path, monkeypatch, capsys):
    script = tmp_path / "good.sh"
    script.write_text("#!/usr/bin/env bash\necho 'PASS: fine'\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setattr(rp, "CHECKS_DIR", tmp_path)

    checklist = tmp_path / "c.json"
    checklist.write_text(json.dumps({"checklist": [{"text": "a", "check": "good"}]}))

    assert rp.main(["--checklist", str(checklist), "--no-daemon-bounce"]) == rp.RC_PASS
    assert "CLEAR TO START" in capsys.readouterr().out


def test_schema_error_exits_usage_not_pass(tmp_path: Path):
    checklist = tmp_path / "c.json"
    checklist.write_text(json.dumps({"checklist": [{"text": "a", "check": "nonexistent_check"}]}))
    # A gate that cannot run has NOT passed.
    assert rp.main(["--checklist", str(checklist), "--no-daemon-bounce"]) == rp.RC_USAGE


def test_list_mode_runs_nothing_and_needs_no_ros(tmp_path: Path, capsys):
    checklist = tmp_path / "c.json"
    checklist.write_text(json.dumps({"checklist": [
        {"text": "battery ok", "check": "battery", "args": {"cf": "${cf}"}},
        "manual thing",
    ]}))
    rc = rp.main(["--checklist", str(checklist), "--var", "cf=drone9", "--list"])
    out = capsys.readouterr().out
    assert rc == rp.RC_PASS
    assert "battery.sh cf=drone9" in out
    assert "[manual] manual thing" in out


# --------------------------------------------------------------------------- #
# The operator-facing UI must render object entries as text, not dict reprs
# --------------------------------------------------------------------------- #
def test_preflight_ui_renders_object_entries(tmp_path: Path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"checklist": [
        "plain manual item",
        {"text": "[HARD] battery >= 3.9 V", "check": "battery", "args": {"cf": "x"}},
    ]}))
    entries = preflight_ui.load_checklist(path)
    assert entries == ["plain manual item", "[HARD] battery >= 3.9 V"]
    assert not any("{" in e for e in entries), "a dict repr leaked into the operator checklist"


@pytest.mark.parametrize("script,argv", [
    ("battery.sh", []),
    ("ekf_convergence.sh", []),
    ("node_present.sh", []),
    ("rigidbody_continuity.sh", []),
    ("rigidbody_near.sh", []),
    ("rigidbody_near.sh", ["name=cf1"]),  # neither x= y= nor node=
    ("topic_rate.sh", ["topic=/x"]),  # min_hz missing
    ("ros_param_equals.sh", ["node=/n", "param=p"]),  # expected missing
])
def test_missing_required_argument_exits_usage(script: str, argv):
    """A check missing an argument must exit 2, not stumble on with an empty
    value. pf_arg is read inside $( ), where an exit only kills the subshell -
    hence the explicit pf_require_args gate in the main shell."""
    result = subprocess.run(["bash", str(CHECKS_DIR / script), *argv], capture_output=True, text=True)
    assert result.returncode == RC_USAGE_EXPECTED, (
        f"{script} exited {result.returncode}:\n{result.stdout}{result.stderr}")
    out = result.stdout + result.stderr
    assert "missing required argument" in out or "give x= and y=, or node=" in out


RC_USAGE_EXPECTED = 2


def test_clean_slate_does_not_count_itself():
    """pgrep -f matches the check's own argv, because the argv contains the very
    pattern being searched for. Excluding self+ancestors is what stops the
    clean-slate check from always reporting a phantom leftover."""
    result = subprocess.run(
        ["bash", str(CHECKS_DIR / "ros_clean_slate.sh"), "pattern=ros_clean_slate", "max_each=0"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"self-match leaked through:\n{result.stdout}{result.stderr}"


def test_clean_slate_self_match_is_stable_under_repetition():
    """The self-exclusion must not be timing-dependent.

    Our own short-lived forks (command/process substitution) inherit the check's
    command line, so they match the pattern; if one exits before its parent
    chain can be read, a naive implementation counts it as a real leftover.
    That race is invisible on the host and reproducible in the dev container, so
    a single run proves nothing - repeat it.
    """
    for i in range(20):
        result = subprocess.run(
            ["bash", str(CHECKS_DIR / "ros_clean_slate.sh"), "pattern=ros_clean_slate", "max_each=0"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, f"iteration {i} counted a phantom leftover:\n{result.stdout}"


def test_clean_slate_still_catches_a_real_leftover():
    """The self/descendant exclusion must not swallow genuine leftovers - a
    stale process from a previous run is a sibling, not a descendant."""
    marker = "motion_capture_tracking_pytest_fixture"
    victim = subprocess.Popen(["bash", "-c", f'exec -a "{marker}" sleep 30'])
    try:
        result = subprocess.run(
            ["bash", str(CHECKS_DIR / "ros_clean_slate.sh"), f"pattern={marker}", "max_each=0"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1, f"a real leftover was not detected:\n{result.stdout}"
        assert marker in result.stdout
    finally:
        victim.kill()
        victim.wait(timeout=10)


def _run_node_present_against(tmp_path: Path, nodes, *argv: str):
    """Run node_present.sh with a stubbed `ros2` on PATH that prints <nodes>.

    The stub makes the check testable without a ROS graph, which is exactly the
    hardware-free property the exact-match regression below needs."""
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    stub = stub_bin / "ros2"
    lines = "".join(f"echo '{n}'\n" for n in nodes)
    stub.write_text("#!/usr/bin/env bash\n" + lines)
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{stub_bin}:{os.environ['PATH']}")
    return subprocess.run(
        ["bash", str(CHECKS_DIR / "node_present.sh"), *argv],
        capture_output=True, text=True, env=env, timeout=30,
    )


def test_node_present_exact_rejects_similarly_named_neighbour(tmp_path: Path):
    """THE 2026-08-12 regression: with only apriltag_mocap_pose_bridge alive,
    the old substring check name=apriltag passed while both Isaac detector
    components had failed to load. exact=true must refuse the neighbour."""
    result = _run_node_present_against(
        tmp_path, ["/apriltag_mocap_pose_bridge"],
        "name=/apriltag_node", "exact=true")
    assert result.returncode == 1, f"the bridge satisfied the detector check:\n{result.stdout}"
    assert result.stdout.startswith("FAIL:")


def test_node_present_exact_passes_on_the_real_node(tmp_path: Path):
    result = _run_node_present_against(
        tmp_path, ["/apriltag_mocap_pose_bridge", "/apriltag_node"],
        "name=/apriltag_node", "exact=true")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("PASS:")


def test_node_present_substring_default_is_unchanged(tmp_path: Path):
    """Without exact=true the historical substring semantics stay: the other
    checklists (e.g. name=${spot}) rely on it. This also documents the trap
    that exact= exists to avoid."""
    result = _run_node_present_against(
        tmp_path, ["/apriltag_mocap_pose_bridge"], "name=apriltag")
    assert result.returncode == 0, result.stdout + result.stderr


def test_disk_space_check_runs_without_ros():
    """Checks that need no ROS must work in any shell - they are also the ones
    usable on a laptop while the rig is elsewhere."""
    ok = subprocess.run(["bash", str(CHECKS_DIR / "disk_space.sh"), "min_gb=0"], capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.startswith("PASS:")

    bad = subprocess.run(["bash", str(CHECKS_DIR / "disk_space.sh"), "min_gb=99999999"],
                         capture_output=True, text=True)
    assert bad.returncode == 1 and bad.stdout.startswith("FAIL:")



# --------------------------------------------------------------------------- #
# rosout_report and mem_available (2026-10-01, Paper 3 AI deck experiment)
# --------------------------------------------------------------------------- #
def _rosout_echo(entries) -> str:
    """What `ros2 topic echo /rosout` prints for (node, msg) log records."""
    out = []
    for name, msg in entries:
        out += ["stamp:", "  sec: 1", "  nanosec: 0", "level: 20", f"name: {name}",
                f"msg: {msg}", "file: x.py", "function: f", "line: 1", "---"]
    return "\n".join(out) + "\n"


def _run_rosout_report_against(tmp_path: Path, entries, *argv) -> subprocess.CompletedProcess:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    canned = tmp_path / "rosout.txt"
    canned.write_text(_rosout_echo(entries))
    stub = stub_bin / "ros2"
    stub.write_text(f"#!/usr/bin/env bash\ncat '{canned}'\n")
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{stub_bin}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(CHECKS_DIR / "rosout_report.sh"), *argv],
                          capture_output=True, text=True, env=env, timeout=30)


AIDECK_ARGS = ("node=aideck_camera_node", "present=fps, ", "absent=JPEG", "window_s=2")


def test_rosout_report_passes_on_the_raw_stream(tmp_path: Path):
    result = _run_rosout_report_against(
        tmp_path, [("aideck_camera_node", "10.8 fps, 540 frames total")], *AIDECK_ARGS)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("PASS:")


def test_rosout_report_refuses_the_jpeg_build(tmp_path: Path):
    """The GAP8 JPEG build still streams decoded frames at 8 fps, and only the node's own report
    says JPEG. A trial recorded on it would carry the wrong representation."""
    result = _run_rosout_report_against(
        tmp_path, [("aideck_camera_node", "8.1 fps, 405 frames total, JPEG 5303 B mean")], *AIDECK_ARGS)
    assert result.returncode == 1 and result.stdout.startswith("FAIL:"), result.stdout


def test_rosout_report_silence_is_a_failure(tmp_path: Path):
    """No report from the node is not evidence of 'no JPEG': it fails."""
    result = _run_rosout_report_against(
        tmp_path, [("motion_capture_tracking", "10.8 fps, but from another node")], *AIDECK_ARGS)
    assert result.returncode == 1 and "no 'fps, ' report" in result.stdout, result.stdout


def test_mem_available_check_runs_without_ros():
    ok = subprocess.run(["bash", str(CHECKS_DIR / "mem_available.sh"), "min_gb=0"],
                        capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.startswith("PASS:"), ok.stdout
    bad = subprocess.run(["bash", str(CHECKS_DIR / "mem_available.sh"), "min_gb=99999999"],
                         capture_output=True, text=True)
    assert bad.returncode == 1 and bad.stdout.startswith("FAIL:"), bad.stdout


# --------------------------------------------------------------------------- #
# topic_rate max_hz and ros_param_equals (2026-10-14, operator factors checked)
# --------------------------------------------------------------------------- #
def _run_topic_rate_against(tmp_path: Path, n_messages: int, *argv) -> subprocess.CompletedProcess:
    """topic_rate.sh with a stubbed `ros2`: `topic list` advertises /img, `topic echo`
    prints n_messages separators. window_s=3.2 leaves 2.0 s after the 1.2 s connect
    correction, so the measured rate is n_messages / 2."""
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    stub = stub_bin / "ros2"
    stub.write_text("#!/usr/bin/env bash\n"
                    "if [ \"$2\" = list ]; then echo /img; exit 0; fi\n"
                    f"for i in $(seq {n_messages}); do echo 'data: x'; echo '---'; done\n")
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{stub_bin}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(CHECKS_DIR / "topic_rate.sh"), "topic=/img",
                           "window_s=3.2", *argv],
                          capture_output=True, text=True, env=env, timeout=30)


def test_topic_rate_without_max_is_unchanged(tmp_path: Path):
    """No max_hz keeps the historical floor-only semantics every checklist relies on."""
    result = _run_topic_rate_against(tmp_path, 22, "min_hz=9")
    assert result.returncode == 0 and result.stdout.startswith("PASS:"), result.stdout
    assert "(>= 9)" in result.stdout


def test_topic_rate_max_hz_tells_the_aideck_builds_apart(tmp_path: Path):
    """The stream_format factor's two levels: the raw build at ~11 fps passes raw and
    fails jpeg, the JPEG build at ~8 fps passes jpeg and fails raw."""
    raw, jpeg = ("min_hz=9",), ("min_hz=7", "max_hz=9")
    assert _run_topic_rate_against(tmp_path, 22, *raw).returncode == 0
    fast_as_jpeg = _run_topic_rate_against(tmp_path, 22, *jpeg)
    assert fast_as_jpeg.returncode == 1 and "at or above max_hz=9" in fast_as_jpeg.stdout, \
        fast_as_jpeg.stdout
    slow = _run_topic_rate_against(tmp_path, 16, *jpeg)
    assert slow.returncode == 0 and "< 9" in slow.stdout, slow.stdout
    assert _run_topic_rate_against(tmp_path, 16, *raw).returncode == 1


def test_topic_rate_max_hz_still_needs_a_flowing_topic(tmp_path: Path):
    """A dead stream is below every max: the min_hz floor must still refuse it."""
    result = _run_topic_rate_against(tmp_path, 0, "min_hz=7", "max_hz=9")
    assert result.returncode == 1 and "stalled or dead" in result.stdout, result.stdout


def _run_param_check_against(tmp_path: Path, reply: str, *argv) -> subprocess.CompletedProcess:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    canned = tmp_path / "reply.txt"
    canned.write_text(reply)
    stub = stub_bin / "ros2"
    stub.write_text(f"#!/usr/bin/env bash\ncat '{canned}'\n"
                    f"grep -q 'value is' '{canned}' || exit 1\n")
    stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{stub_bin}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(CHECKS_DIR / "ros_param_equals.sh"),
                           "node=/camera/d435", "param=depth_module.visual_preset", *argv],
                          capture_output=True, text=True, env=env, timeout=30)


@pytest.mark.parametrize("reply,expected", [
    ("Integer value is: 3\n", "3"),
    ("Double value is: 4.0\n", "4"),
    ("Boolean value is: True\n", "true"),
    ("String value is: HighAccuracy\n", "highaccuracy"),
])
def test_ros_param_equals_passes_on_the_declared_value(tmp_path: Path, reply, expected):
    result = _run_param_check_against(tmp_path, reply, f"expected={expected}")
    assert result.returncode == 0 and result.stdout.startswith("PASS:"), result.stdout


def test_ros_param_equals_refuses_another_value(tmp_path: Path):
    """The visual_preset case: the camera runs preset 4 while the trial declares 3."""
    result = _run_param_check_against(tmp_path, "Integer value is: 4\n", "expected=3")
    assert result.returncode == 1 and "declares 3" in result.stdout, result.stdout


@pytest.mark.parametrize("reply", ["Node not found\n", "Parameter not set.\n", ""])
def test_ros_param_equals_unreadable_is_a_failure(tmp_path: Path, reply):
    """An absent node or an unset parameter is not evidence of the declared value."""
    result = _run_param_check_against(tmp_path, reply, "expected=3")
    assert result.returncode == 1 and "cannot read" in result.stdout, result.stdout


def test_ros_param_equals_undeclared_expected_fails(tmp_path: Path):
    """A null per-level value (expected=) is "not declared yet" and must never pass."""
    result = _run_param_check_against(tmp_path, "Integer value is: 3\n", "expected=")
    assert result.returncode == 1 and "no expected value" in result.stdout, result.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_a_wedged_daemon_cannot_hang_the_gate(monkeypatch, capsys):
    """2026-10-06: `ros2 daemon stop` blocked on a wedged daemon until the 300 s gate timeout."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("timeout")))
        if cmd[:3] == ["ros2", "daemon", "stop"]:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(rp.shutil, "which", lambda name: "/usr/bin/ros2")
    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    monkeypatch.setenv("ROS_DOMAIN_ID", "42")
    rp.bounce_daemon()
    assert calls[0] == (["ros2", "daemon", "stop"], rp.DAEMON_STOP_TIMEOUT_S)
    assert calls[1][0][:3] == ["pkill", "-KILL", "-f"] and "--ros-domain-id 42" in calls[1][0][3]
    assert calls[2] == (["ros2", "daemon", "start"], rp.DAEMON_START_TIMEOUT_S)
    assert "wedged" in capsys.readouterr().out


def _dds_check(env_updates):
    import os
    env = {k: v for k, v in os.environ.items() if k not in ("RMW_IMPLEMENTATION", "CYCLONEDDS_URI")}
    env.update(env_updates)
    return subprocess.run(["bash", str(CHECKS_DIR / "dds_socket_buffer.sh")],
                          capture_output=True, text=True, env=env)


def test_dds_socket_buffer_skips_a_stack_that_does_not_use_cyclonedds():
    """Humble's default RMW is Fast DDS: a project made by `trials init` on a machine
    without a CycloneDDS config must not fail its first gate (the package's CI,
    2026-10-15). Exit 3 is SKIP, which the gate does not count as a failure."""
    for env in ({}, {"RMW_IMPLEMENTATION": "rmw_fastrtps_cpp"}):
        result = _dds_check(env)
        assert result.returncode == 3, result.stdout + result.stderr
        assert "not rmw_cyclonedds_cpp" in result.stdout


def test_dds_socket_buffer_still_fails_cyclonedds_without_a_config():
    """The lab's false-dead trap stays a FAIL: Cyclone in use, its config unknown."""
    result = _dds_check({"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"})
    assert result.returncode == 1 and "CYCLONEDDS_URI is unset" in result.stdout


def _stub_late_topic(tmp_path: Path, appear_after_calls: int) -> dict:
    """A `ros2` stub whose `topic list` advertises /late only from the n-th call on,
    like OpenVINS, which creates /ov_msckf/odomimu once its initializer fired."""
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    counter = tmp_path / "calls"
    counter.write_text("0")
    stub = stub_bin / "ros2"
    stub.write_text("#!/usr/bin/env bash\n"
                    f"if [ \"$2\" = list ]; then n=$(( $(cat {counter}) + 1 )); echo $n > {counter}; "
                    f"[ $n -ge {appear_after_calls} ] && echo /late; exit 0; fi\n"
                    "for i in $(seq 20); do echo 'data: x'; echo '---'; done\n")
    stub.chmod(0o755)
    return dict(os.environ, PATH=f"{stub_bin}:{os.environ['PATH']}")


def test_topic_rate_appear_s_waits_for_a_late_topic(tmp_path: Path):
    """appear_s: a topic that is advertised only after a few probes still passes."""
    env = _stub_late_topic(tmp_path, appear_after_calls=3)
    result = subprocess.run(["bash", str(CHECKS_DIR / "topic_rate.sh"), "topic=/late", "min_hz=5",
                             "window_s=3.2", "appear_s=10"],
                            capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0 and result.stdout.startswith("PASS:"), result.stdout + result.stderr


def test_topic_rate_without_appear_s_still_fails_at_once(tmp_path: Path):
    """The default keeps the old semantics: one probe, then FAIL 'not advertised at all'."""
    env = _stub_late_topic(tmp_path, appear_after_calls=3)
    result = subprocess.run(["bash", str(CHECKS_DIR / "topic_rate.sh"), "topic=/late", "min_hz=5",
                             "window_s=3.2"],
                            capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode != 0 and "not advertised at all" in result.stdout, result.stdout


def test_topic_rate_appear_s_fails_with_its_own_message(tmp_path: Path):
    env = _stub_late_topic(tmp_path, appear_after_calls=1000)
    result = subprocess.run(["bash", str(CHECKS_DIR / "topic_rate.sh"), "topic=/late", "min_hz=5",
                             "window_s=3.2", "appear_s=2"],
                            capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode != 0 and "not advertised within 2s" in result.stdout, result.stdout
