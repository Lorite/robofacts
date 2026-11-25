"""The cflib example without a drone and without a Crazyradio.

The hover logic runs against a fake drone, so every way out of a flight is tested: the end of
the hover, the stop latch, the emergency stop, a signal, the geofence, an error in flight, and
a closed output pipe. The gate and the reset are run where no radio can answer (a radio index
that does not exist), so a drone that happens to be on is never contacted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT / "experiments" / "cflib_hover" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cf_hover  # noqa: E402
import cf_link  # noqa: E402
import verify_hover  # noqa: E402
from cflib_drone import LinkError  # noqa: E402

#: A radio index that no computer has: cflib fails at once, and no drone hears anything.
NO_RADIO_URI = "radio://9/125/2M/E7E7E7E7E7"
_real_sleep = time.sleep


# --------------------------------------------------------------------------- #
# The verify step on synthetic flight logs
# --------------------------------------------------------------------------- #
def write_log(recording: Path, heights, dt=0.05, events=()) -> Path:
    data = recording / "data"
    data.mkdir(parents=True)
    lines = [json.dumps({"t": 1000.0 + i * dt, "kind": "state", "x": 0.0, "y": 0.0, "z": h,
                         "range_m": h, "vbat": 3.9, "squal": 120}) for i, h in enumerate(heights)]
    lines += [json.dumps({"t": 2000.0, "kind": "event", "event": e}) for e in events]
    (data / "flight.jsonl").write_text("\n".join(lines) + "\n")
    return recording


def flight(hover_s=10.0):
    up = [0.02 * i for i in range(26)]             # 0 to 0.5 m in 1.3 s
    return [0.02] * 20 + up + [0.5] * int(hover_s / 0.05) + up[::-1] + [0.02] * 10


def run_verify(recording: Path, *extra: str) -> int:
    return verify_hover.main([str(recording), *extra])


def test_verify_passes_a_flight(tmp_path, capsys):
    assert run_verify(write_log(tmp_path / "r", flight(), events=["takeoff", "land"])) == 0
    out = capsys.readouterr().out
    assert out.startswith("PASS: airborne 1") and "longest gap between samples 0.05 s" in out


def test_verify_fails_when_the_drone_never_left_the_ground(tmp_path, capsys):
    """A refusal before arming: the script exits cleanly, the log holds only ground samples."""
    assert run_verify(write_log(tmp_path / "r", [0.02] * 200, events=["refused"])) == 1
    assert "airborne 0.0 s" in capsys.readouterr().out


def test_verify_fails_a_hop_shorter_than_the_minimum(tmp_path):
    assert run_verify(write_log(tmp_path / "r", flight(hover_s=1.0))) == 1
    assert run_verify(write_log(tmp_path / "r2", flight(hover_s=1.0)), "--min-airborne-s", "1") == 0


def test_verify_fails_a_recording_that_ends_in_the_air(tmp_path, capsys):
    assert run_verify(write_log(tmp_path / "r", [0.02] * 10 + [0.5] * 100)) == 1
    assert "ends in the air" in capsys.readouterr().out


def test_verify_fails_without_a_log_or_without_samples(tmp_path):
    (tmp_path / "empty" / "data").mkdir(parents=True)
    assert run_verify(tmp_path / "empty") == 1
    (tmp_path / "empty" / "data" / "flight.jsonl").write_text('{"t": 1, "kind": "event"}\n')
    assert run_verify(tmp_path / "empty") == 1


# --------------------------------------------------------------------------- #
# The hover logic against a fake drone
# --------------------------------------------------------------------------- #
class FakeDrone:
    """Records every command. Its height follows the commands, its xy can drift."""

    def __init__(self, uri, *, deck=True, vbat=4.0, settles=True, ground_xy=0.0, drift=0.0,
                 fail_on=None, on_tick=None, on_land=None, no_samples=False):
        self.uri, self.deck, self.vbat, self.settles = uri, deck, vbat, settles
        self.xy, self.drift, self.fail_on, self.on_tick = ground_xy, drift, fail_on, on_tick
        self.z, self.calls, self.params, self.sink, self.ticks = 0.0, [], [], None, 0
        self.on_land, self.no_samples, self.frozen_at = on_land, no_samples, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("close")

    def _do(self, name, *args):
        self.calls.append(name)
        if name == self.fail_on:
            raise RuntimeError(f"radio lost during {name}")

    def has_flow_deck(self):
        return self.deck

    def battery_v(self):
        return self.vbat

    def reset_estimator(self):
        return self.settles

    def start_log(self, sink):
        self.sink = sink

    def age_s(self):
        if self.no_samples:
            return None
        return 0.0 if self.frozen_at is None else time.monotonic() - self.frozen_at

    def latest(self):
        if self.no_samples:
            return {}
        self.ticks += 1
        if self.z > 0.1:
            self.xy += self.drift
        if self.on_tick:
            self.on_tick(self)
        state = {"x": self.xy, "y": 0.0, "z": self.z, "range_m": self.z, "vbat": self.vbat, "squal": 100}
        if self.sink:
            self.sink(state)
        return state

    def set_param(self, name, value):
        self.params.append((name, int(value)))

    def arm(self, on):
        self._do("arm" if on else "disarm")

    def takeoff(self, height, duration):
        self._do("takeoff")
        self.z = height

    def climb(self, dz, duration):
        self._do("climb")
        self.z += dz

    def land(self, height, duration):
        self._do("land")
        self.z = height
        if self.on_land:
            self.on_land(self)

    def stop_commander(self):
        self._do("stop_commander")

    def emergency_stop(self):
        self._do("emergency_stop")
        self.z = 0.0


CFG = {"uri": NO_RADIO_URI, "min_vbat": 3.85, "min_squal": 50, "ground_guard_m": 0.05,
       "stage1_m": 0.2, "stage1_s": 0.05, "flow_on_m": 0.1, "stage2_s": 0.05, "height_m": 0.5,
       "max_hover_s": 60, "fence_xy_m": 0.5, "fence_z_m": 0.9, "land_m": 0.05, "land_s": 0.05,
       "stale_s": 0.1}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A config with short stages, a private stop latch, and sleeps capped at 5 ms."""
    cfg = tmp_path / "drone.yaml"
    cfg.write_text(json.dumps(CFG))
    latch = tmp_path / "latch"
    monkeypatch.setenv("TRIALS_STOP_LATCH", str(latch))
    monkeypatch.delenv("CF_URI", raising=False)
    monkeypatch.setattr(cf_hover.time, "sleep", lambda s: _real_sleep(min(s, 0.005)))
    handlers = {s: cf_hover.signal.getsignal(s) for s in (cf_hover.signal.SIGINT, cf_hover.signal.SIGTERM)}
    yield {"cfg": cfg, "latch": latch, "log": tmp_path / "flight.jsonl"}
    for signum, handler in handlers.items():   # main() installs its own, give pytest its Ctrl+C back
        cf_hover.signal.signal(signum, handler)


def hover(env, hover_s="0.2", **fake):
    drones = []

    def factory(uri):
        drones.append(FakeDrone(uri, **fake))
        return drones[-1]

    rc = cf_hover.main(["--log", str(env["log"]), "--hover-s", hover_s, "--config", str(env["cfg"])],
                       drone_factory=factory)
    events = [json.loads(line) for line in env["log"].read_text().splitlines()]
    return rc, (drones[0] if drones else None), [e.get("event") for e in events if e["kind"] == "event"], events


def test_a_hover_takes_off_in_two_stages_holds_and_lands(env):
    rc, drone, events, log = hover(env)
    assert rc == 0
    assert drone.calls == ["arm", "takeoff", "climb", "land", "stop_commander", "disarm", "close"]
    # Flow off on the floor, back on in the first stage.
    assert drone.params == [("motion.disable", 1), ("motion.disable", 0)]
    assert events == ["start", "takeoff", "land", "end"]
    assert any(e["kind"] == "state" and e["z"] == 0.5 for e in log), "the state is logged"


def test_the_stop_latch_lands_the_drone_and_the_trial_fails(env):
    def write_latch(drone):
        if drone.z >= 0.5 and drone.ticks > 5:
            env["latch"].write_text("soft_stop crazyflie now\n")

    rc, drone, events, log = hover(env, hover_s="30", on_tick=write_latch)
    assert rc == cf_hover.STOP_RC
    assert "land" in drone.calls and "emergency_stop" not in drone.calls and drone.calls[-2] == "disarm"
    assert [e["reason"] for e in log if e.get("event") == "land"] == ["operator stop latch: soft_stop crazyflie now"]


def test_an_estop_latch_cuts_the_motors_instead_of_landing(env):
    def write_latch(drone):
        if drone.z >= 0.5:
            env["latch"].write_text("estop crazyflie now\n")

    rc, drone, events, _ = hover(env, hover_s="30", on_tick=write_latch)
    assert rc == cf_hover.STOP_RC
    assert "emergency_stop" in drone.calls and "land" not in drone.calls
    assert "emergency_stop" in events


def test_a_signal_lands_the_drone(env):
    def send_sigterm(drone):
        if drone.z >= 0.5:
            cf_hover.signal.raise_signal(cf_hover.signal.SIGTERM)

    rc, drone, _, log = hover(env, hover_s="30", on_tick=send_sigterm)
    assert rc == cf_hover.STOP_RC and "land" in drone.calls
    assert [e["reason"] for e in log if e.get("event") == "land"] == ["signal SIGTERM"]


def test_an_error_in_flight_lands_the_drone(env):
    rc, drone, _, log = hover(env, fail_on="climb")
    assert rc == cf_hover.ERROR_RC
    assert drone.calls[-4:] == ["land", "stop_commander", "disarm", "close"]
    assert "radio lost during climb" in [e for e in log if e.get("event") == "land"][0]["reason"]


def test_leaving_the_geofence_lands_the_drone(env):
    rc, drone, _, log = hover(env, hover_s="30", drift=0.02)
    assert rc == cf_hover.GEOFENCE_RC and "land" in drone.calls
    assert "fence 0.5 m" in [e for e in log if e.get("event") == "land"][0]["reason"]


def test_a_lost_link_lands_with_its_own_reason_and_tells_the_operator(env, capsys):
    """Review finding 1 (2026-11-25): the last sample freezes when the link dies in the air."""
    def lose_link(drone):
        if drone.z >= 0.5 and drone.ticks > 3 and drone.frozen_at is None:
            drone.frozen_at = time.monotonic()

    rc, drone, _, log = hover(env, hover_s="30", on_tick=lose_link)
    assert rc == cf_hover.STALE_RC
    assert drone.calls[-4:] == ["land", "stop_commander", "disarm", "close"], "the land command is still sent"
    reason = [e for e in log if e.get("event") == "land"][0]["reason"]
    assert reason.startswith("no state sample for") and "radio link is probably lost" in reason
    out = capsys.readouterr().out
    assert "STALE STATE" in out and "use the physical stop" in out


def test_a_dead_link_that_refuses_the_land_command_still_disarms(env):
    rc, drone, _, log = hover(env, fail_on="land")
    assert drone.calls[-3:] == ["stop_commander", "disarm", "close"]
    assert [e["command"] for e in log if e.get("event") == "command_failed"] == ["land"]


def test_an_estop_during_the_landing_cuts_the_motors(env):
    """Review finding 2: the landing wait reads the latch."""
    rc, drone, events, _ = hover(env, on_land=lambda d: env["latch"].write_text("estop crazyflie now\n"))
    assert drone.calls[-4:] == ["land", "emergency_stop", "disarm", "close"]
    assert "stop_commander" not in drone.calls and events[-2:] == ["emergency_stop", "end"]


def test_an_estop_after_ctrl_c_is_not_hidden_by_the_signal(env):
    """Review finding 2: Ctrl+C starts the landing, a later estop line still cuts the motors."""
    def ctrl_c(drone):
        if drone.z >= 0.5 and drone.ticks > 3:
            cf_hover.signal.raise_signal(cf_hover.signal.SIGINT)

    rc, drone, _, log = hover(env, hover_s="30", on_tick=ctrl_c,
                              on_land=lambda d: env["latch"].write_text("estop crazyflie now\n"))
    assert rc == cf_hover.STOP_RC
    assert [e["reason"] for e in log if e.get("event") == "land"] == ["signal SIGINT"]
    assert "emergency_stop" in drone.calls and "stop_commander" not in drone.calls


def test_an_estop_line_wins_over_a_signal_that_came_first(env, tmp_path):
    stop = cf_hover.Stop(env["latch"], time.time())
    stop.on_signal(cf_hover.signal.SIGINT, None)
    assert stop.reason() == "signal SIGINT"
    env["latch"].write_text("soft_stop crazyflie\n")
    assert stop.reason() == "signal SIGINT" and stop.estop() == ""
    env["latch"].write_text("estop crazyflie\n")
    assert stop.reason() == stop.estop() == "operator stop latch: estop crazyflie"


def test_an_empty_state_is_outside_the_fence():
    """Review finding 3: no sample is never a pass."""
    assert cf_hover.outside_fence({}, CFG) == "no state sample"


def test_a_closed_output_pipe_does_not_stop_the_landing(env, monkeypatch):
    """Ctrl+C stops the runner too, and the pipe to the action closes while it lands."""
    class ClosedPipe:
        def write(self, _text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stdout", ClosedPipe())
    rc, drone, _, _ = hover(env)
    assert rc == 0 and drone.calls[-4:] == ["land", "stop_commander", "disarm", "close"]


@pytest.mark.parametrize("fake, words", [
    ({"deck": False}, "no Flow deck detected"),
    ({"vbat": 3.70}, "battery 3.70 V below 3.85 V"),
    ({"settles": False}, "did not settle"),
    ({"ground_xy": 0.08}, "ground position 0.080 m"),
    ({"no_samples": True}, "the state is not streaming (no state sample from the drone yet)"),
])
def test_refusals_before_arming(env, fake, words):
    rc, drone, events, log = hover(env, **fake)
    assert rc == cf_hover.REFUSED_RC
    assert "arm" not in drone.calls and "takeoff" not in drone.calls
    assert words in [e for e in log if e.get("event") == "refused"][0]["reason"]


def test_a_fresh_stop_latch_refuses_before_the_radio_opens(env):
    env["latch"].write_text("soft_stop crazyflie a minute ago\n")
    rc, drone, events, _ = hover(env)
    assert rc == cf_hover.STOP_RC and drone is None and events == ["start", "refused"]


def test_an_old_stop_latch_does_not_refuse(env):
    env["latch"].write_text("soft_stop crazyflie long ago\n")
    old = time.time() - 600
    os.utime(env["latch"], (old, old))
    assert hover(env)[0] == 0


def test_a_link_that_does_not_come_up_refuses(env):
    def no_link(uri):
        raise LinkError(f"cannot open {uri}: Cannot find a Crazyradio Dongle")

    rc = cf_hover.main(["--log", str(env["log"]), "--config", str(env["cfg"])], drone_factory=no_link)
    assert rc == cf_hover.REFUSED_RC
    assert "Crazyradio" in env["log"].read_text()


def test_a_hover_longer_than_the_config_allows_is_refused(env):
    assert hover(env, hover_s="61")[0] == cf_hover.REFUSED_RC


# --------------------------------------------------------------------------- #
# The gate probe with a fake drone, then the gate and the reset where no radio answers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question, fake, rc, words", [
    ("battery", {"vbat": 4.01}, 0, "PASS: battery 4.01 V"),
    ("battery", {"vbat": 3.70}, 1, "FAIL: battery 3.70 V is below 3.85 V"),
    ("battery", {"vbat": None}, 1, "FAIL: no battery sample"),
    ("flow-deck", {"deck": False}, 1, "FAIL: no Flow deck detected"),
])
def test_the_gate_probe(env, capsys, question, fake, rc, words):
    assert cf_link.main([question, "--config", str(env["cfg"])],
                        drone_factory=lambda uri: FakeDrone(uri, **fake)) == rc
    assert capsys.readouterr().out.startswith(words)


def project_env(tmp_path):
    env = {**os.environ, "CF_URI": NO_RADIO_URI, "TRIALS_STOP_LATCH": str(tmp_path / "latch")}
    core = PROJECT.parents[1] / "robofacts"
    if (core / "robofacts").is_dir():   # the source tree, when robofacts is not installed
        env["PYTHONPATH"] = f"{core}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return env


def trials(tmp_path, *args):
    return subprocess.run([sys.executable, "-m", "robofacts.cli", *args], cwd=PROJECT, text=True,
                          capture_output=True, env=project_env(tmp_path), timeout=120)


def test_the_gate_says_no_go_without_a_radio_and_names_the_reason(tmp_path):
    out = trials(tmp_path, "gate", "--checklist", "experiments/cflib_hover/config/preflight_checklist.json")
    text = out.stdout + out.stderr
    assert out.returncode != 0 and "NO-GO" in text
    assert "PASS  stop_latch" in text and "PASS  recording_dir_writable" in text
    for check in ("cf_battery", "cf_flow_deck"):
        line = next(line for line in text.splitlines() if f"  {check}:" in line)
        assert line.strip().startswith("FAIL") and ("cflib is not installed" in line or "cannot open" in line), line


def test_the_reset_fails_without_a_radio(tmp_path):
    out = subprocess.run([sys.executable, str(SCRIPTS / "cf_link.py"), "power-cycle"], cwd=PROJECT,
                         text=True, capture_output=True, env=project_env(tmp_path), timeout=60)
    assert out.returncode == 1 and out.stdout.startswith("FAIL:")


def test_the_lint_passes(tmp_path):
    out = trials(tmp_path, "lint")
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]


def test_the_dry_run_prints_the_reset_the_recorder_and_the_hover(tmp_path):
    out = trials(tmp_path, "run", "experiments/cflib_hover/campaigns/baseline.yaml", "--limit", "1")
    assert out.returncode == 0, out.stdout[-2000:]
    assert "DRY RUN" in out.stdout
    for command in ("cf_link.py power-cycle", "process_recorder", "experiments/cflib_hover/runs/flight.jsonl",
                    "cf_hover.py --log experiments/cflib_hover/runs/flight.jsonl --hover-s 10"):
        assert command in out.stdout, command
