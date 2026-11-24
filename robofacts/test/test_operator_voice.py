"""Tests of the spoken-cue client (operator_voice.py): never blocks, never raises, never loud.

No audio and no daemon: a plain UDP socket on an ephemeral loopback port stands in for the
laptop's voice daemon where a test needs to see what arrived.
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from robofacts import operator_voice as ov
@pytest.fixture
def listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(1.0)
    yield sock, sock.getsockname()[1]
    sock.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_nobody_listening_neither_blocks_nor_raises():
    port = _free_port()
    t0 = time.monotonic()
    for _ in range(20):
        assert ov.send("Recording.", host="127.0.0.1", port=port, env={}) is True
    assert time.monotonic() - t0 < 0.5
    assert ov.check(host="127.0.0.1", port=port, timeout_s=0.2, env={}) is False


def test_an_unresolvable_host_name_returns_quickly_and_false():
    t0 = time.monotonic()
    # The trailing dot makes the name absolute: a network whose DNS search domain answers every
    # name (a guest Wi-Fi, 2026-10-14) would otherwise resolve no-such-host.invalid.<domain>.
    assert ov.send("x", host="no-such-host.invalid.", port=47900, env={}) is False
    assert time.monotonic() - t0 < 2 * ov.RESOLVE_TIMEOUT_S + 0.5


def test_the_datagram_carries_the_text(listener):
    sock, port = listener
    assert ov.send("Pick up and walk.", host="127.0.0.1", port=port, env={})
    data, _ = sock.recvfrom(4096)
    assert data.decode("utf-8") == "Pick up and walk."


@pytest.mark.parametrize("switch", ["off", "OFF", "0", "false", "no"])
def test_voice_off_sends_nothing_but_still_prints(listener, switch):
    sock, port = listener
    lines = []
    sent = ov.speak("Back on the stand.", out=lines.append, host="127.0.0.1", port=port,
                    env={ov.ENV_SWITCH: switch})
    assert sent is False and lines == ["[voice] Back on the stand."]
    sock.settimeout(0.2)
    with pytest.raises(socket.timeout):
        sock.recvfrom(4096)


def test_the_length_is_capped_at_a_word_boundary(listener):
    sock, port = listener
    assert ov.send("walk " * 100, host="127.0.0.1", port=port, env={})
    data, _ = sock.recvfrom(4096)
    text = data.decode("utf-8")
    assert len(text) <= ov.MAX_CHARS and text.endswith("walk")


def test_clip_removes_the_bell_and_collapses_whitespace():
    assert ov.clip("\a  >>> PASS\tSTARTS <<<\n") == ">>> PASS STARTS <<<"
    assert ov.clip("x" * 500) == "x" * ov.MAX_CHARS


def test_the_target_comes_from_the_environment_with_safe_defaults():
    assert ov.target({}) == ("127.0.0.1", 47900)
    assert ov.target({ov.ENV_HOST: "192.168.0.30", ov.ENV_PORT: "47901"}) == ("192.168.0.30", 47901)
    assert ov.target({ov.ENV_PORT: "not-a-port"}) == ("127.0.0.1", 47900)
    assert ov.target({ov.ENV_PORT: "70000"}) == ("127.0.0.1", 47900)
    assert ov.enabled({}) and not ov.enabled({ov.ENV_SWITCH: "off"})


def test_a_test_run_never_talks_unless_forced(monkeypatch):
    """The runner's own tests drive hardware-like configs, so the laptop must stay quiet."""
    monkeypatch.delenv(ov.ENV_SWITCH, raising=False)
    assert ov.enabled() is False                       # PYTEST_CURRENT_TEST is set
    assert ov.enabled({ov.ENV_SWITCH: "on", "PYTEST_CURRENT_TEST": "x"}) is True
    assert ov.enabled({}) is True                      # outside pytest, unset means on


def test_speak_keeps_the_terminal_bell_when_asked():
    lines = []
    ov.speak("STOP", bell=True, out=lines.append, env={ov.ENV_SWITCH: "off"})
    assert lines == ["\a[voice] STOP"]


def test_the_cli_sends_and_never_fails(listener, monkeypatch, capsys):
    sock, port = listener
    monkeypatch.setenv(ov.ENV_SWITCH, "on")     # a pytest run is silent unless forced
    assert ov.main(["--host", "127.0.0.1", "--port", str(port), "Done.", "Thank", "you."]) == 0
    data, _ = sock.recvfrom(4096)
    assert data == b"Done. Thank you."
    assert "[voice] Done. Thank you." in capsys.readouterr().out


def test_the_cli_check_fails_without_a_daemon():
    assert ov.main(["--check", "--host", "127.0.0.1", "--port", str(_free_port())]) == 1


# --------------------------------------------------------------------------- #
# The cue timeline, on a fake clock
# --------------------------------------------------------------------------- #
class FakeTime:
    """now() and wait(timeout): wait advances the clock, as a real wait would."""

    def __init__(self, stop_at=None):
        self.t = 0.0
        self.stop_at = stop_at

    def now(self):
        return self.t

    def wait(self, timeout):
        if self.stop_at is not None and self.t + timeout > self.stop_at:
            self.t = self.stop_at
            return True          # stopped while waiting
        self.t += timeout
        return False


HANDHELD = [{"at_s": 101, "text": "Done. Thank you."},
            {"at_s": 0, "text": "Recording. Keep still."},
            {"at_s": 88, "text": "Back on the stand."},
            {"at_s": 8, "text": "Pick up and walk."}]


def test_the_timeline_speaks_in_time_order_at_each_cue_time():
    clock, sent = FakeTime(), []
    timeline = ov.CueTimeline(HANDHELD, send=lambda text: sent.append((clock.t, text)) or True,
                              out=lambda line: None, now=clock.now, wait=clock.wait)
    results = timeline.run(t0=0.0)
    assert sent == [(0.0, "Recording. Keep still."), (8.0, "Pick up and walk."),
                    (88.0, "Back on the stand."), (101.0, "Done. Thank you.")]
    assert all(r["spoken"] for r in results)


def test_stopping_the_timeline_drops_the_later_cues_and_says_so():
    clock, sent = FakeTime(stop_at=50.0), []
    timeline = ov.CueTimeline(HANDHELD, send=lambda text: sent.append(text) or True,
                              out=lambda line: None, now=clock.now, wait=clock.wait)
    results = timeline.run(t0=0.0)
    assert sent == ["Recording. Keep still.", "Pick up and walk."]
    assert [r["text"] for r in results if not r["spoken"]] == ["Back on the stand.",
                                                                 "Done. Thank you."]


def test_a_failing_sender_never_stops_the_timeline():
    clock = FakeTime()

    def broken(text):
        return False      # what send() returns when nothing could be sent

    results = ov.CueTimeline(HANDHELD, send=broken, out=lambda line: None,
                             now=clock.now, wait=clock.wait).run(t0=0.0)
    assert len(results) == 4 and not any(r["spoken"] for r in results)


def test_the_threaded_timeline_stops_promptly():
    sent = []
    timeline = ov.CueTimeline([{"at_s": 0, "text": "now"}, {"at_s": 60, "text": "later"}],
                              send=lambda text: sent.append(text) or True,
                              out=lambda line: None).start()
    time.sleep(0.2)
    t0 = time.monotonic()
    results = timeline.stop()
    assert time.monotonic() - t0 < 0.5
    assert sent == ["now"] and results[-1]["spoken"] is False
