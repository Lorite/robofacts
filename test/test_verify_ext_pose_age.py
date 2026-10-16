"""verify_ext_pose_age: the drone's own locSrv.ageMs is the evidence of pose loss.

Born 2026-10-14. On the 2026-10-07 lab day the server's ``Motion capture rate
off`` warnings read like 1.2 s and 7 s gaps, and the bag proved them an artifact
(no /poses gap over 100 ms, on-board age max 2 ms). These tests pin the check
that reads the drone's report instead: healthy, a long stale run, a missing
topic that is required, and a missing topic that is not.

Two layers. The judging logic is pure and runs on any machine. The round trip
writes real MCAP bags with rosbag2_py and crazyflie_interfaces, so it needs the
ROS 2 dev container and is skipped elsewhere.
"""
import datetime as dt
import json

import pytest

from robofacts import verify_ext_pose_age as vea

RATE_HZ = 10.0
T0 = 1_791_370_000.0          # an arbitrary epoch second on the recorder's clock


def _series(n, age_ms=1.0, start=T0, stale=None):
    """n samples at 10 Hz. ``stale`` = (first index, count, age) overrides a run."""
    out = []
    for i in range(n):
        age = age_ms
        if stale and stale[0] <= i < stale[0] + stale[1]:
            age = stale[2]
        out.append((start + i / RATE_HZ, float(age)))
    return out


def _flags(n, fly_from, fly_to, start=T0):
    """n status samples at 10 Hz, IS_FLYING for indices [fly_from, fly_to)."""
    return [(start + i / RATE_HZ, fly_from <= i < fly_to) for i in range(n)]


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #
def test_flying_intervals_open_close_and_end_in_flight():
    flags = [(0, False), (1, True), (2, True), (3, False), (4, True)]
    assert vea.flying_intervals(flags) == [(1, 3), (4, float("inf"))]
    assert vea.flying_intervals([(0, False), (1, False)]) == []


def test_healthy_flight_passes_and_reports_max_and_median():
    verdicts = vea.evaluate({"cf": _series(240, 1.0)}, {"cf": _flags(240, 10, 230)},
                            require=["cf"])
    rc, lines = vea.report(verdicts, max_age_ms=200, min_consecutive=3)
    assert rc == 0
    (v,) = verdicts
    assert v.window == "airborne" and v.n_window == 220
    assert v.max_ms == 1.0 and v.median_ms == 1.0
    assert lines[0].startswith("VERIFY-OK") and "cf max 1 ms median 1 ms" in lines[0]


def test_long_stale_run_while_airborne_fails_with_its_duration():
    ages = _series(240, 1.0, stale=(100, 8, 450.0))
    verdicts = vea.evaluate({"cf": ages}, {"cf": _flags(240, 10, 230)}, require=["cf"])
    rc, lines = vea.report(verdicts, max_age_ms=200, min_consecutive=3)
    assert rc == 1
    (v,) = verdicts
    assert v.failed and v.longest_stale_samples == 8 and v.max_ms == 450.0
    assert lines[0].startswith("VERIFY-FAIL") and "8 consecutive samples" in lines[0]


def test_a_short_spike_under_the_declared_run_length_passes():
    ages = _series(240, 1.0, stale=(100, 2, 450.0))
    verdicts = vea.evaluate({"cf": ages}, {"cf": _flags(240, 10, 230)}, min_consecutive=3)
    assert not verdicts[0].failed and verdicts[0].longest_stale_samples == 2


def test_stale_ages_on_the_pads_are_not_a_flight_fault():
    # stale before takeoff (indices 0..9) and after landing (230..239) only
    ages = [(t, 900.0 if (i < 10 or i >= 231) else 1.0)
            for i, (t, _) in enumerate(_series(240))]
    verdicts = vea.evaluate({"cf": ages}, {"cf": _flags(240, 10, 230)})
    assert not verdicts[0].failed and verdicts[0].max_ms == 1.0


def test_threshold_and_run_length_are_declared_not_fixed():
    ages = _series(240, 1.0, stale=(100, 3, 150.0))
    flags = {"cf": _flags(240, 10, 230)}
    assert not vea.evaluate({"cf": ages}, flags, max_age_ms=200)[0].failed
    assert vea.evaluate({"cf": ages}, flags, max_age_ms=100, min_consecutive=3)[0].failed


def test_missing_topic_required_fails():
    verdicts = vea.evaluate({"lead": _series(50)}, {}, require=["lead", "follower"])
    rc, lines = vea.report(verdicts, max_age_ms=200, min_consecutive=3)
    assert rc == 1
    assert "follower" in lines[0] and "required" in lines[0]


def test_missing_topic_optional_reports_not_recorded_without_failing():
    rc, lines = vea.report(vea.evaluate({}, {}), max_age_ms=200, min_consecutive=3)
    assert rc == 0 and "not recorded" in lines[0]
    verdicts = vea.evaluate({}, {"cf": _flags(50, 5, 45)}, require_when_airborne=[])
    assert verdicts == []


def test_require_when_airborne_applies_only_to_a_drone_that_flew():
    grounded = vea.evaluate({}, {"cf": _flags(50, 99, 99)}, require_when_airborne=["cf"])
    assert not grounded[0].failed and grounded[0].reason == "not recorded"
    flew = vea.evaluate({}, {"cf": _flags(50, 5, 45)}, require_when_airborne=["cf"])
    assert flew[0].failed


def test_a_drone_that_never_flew_is_not_judged():
    ages = _series(50, 900.0)
    verdicts = vea.evaluate({"cf": ages}, {"cf": _flags(50, 99, 99)}, require=["cf"])
    assert not verdicts[0].failed and verdicts[0].window == "not_airborne"


def test_no_status_topic_judges_the_whole_bag():
    ages = _series(50, 1.0, stale=(20, 5, 300.0))
    verdicts = vea.evaluate({"cf": ages}, {})
    assert verdicts[0].window == "whole_bag" and verdicts[0].failed


def test_requirements_waived_for_sim_and_for_bags_before_the_declared_date():
    start = dt.datetime(2026, 7, 1, 12, tzinfo=dt.timezone.utc)
    assert vea.requirements_waived(start, dt.date(2026, 8, 17), None)
    assert vea.requirements_waived(start, None, "sim")
    assert vea.requirements_waived(start, dt.date(2026, 6, 1), "cpp") is None
    assert vea.requirements_waived(None, dt.date(2026, 8, 17), None) is None
    verdicts = vea.evaluate({}, {}, require=["cf"], waived="backend sim")
    assert verdicts == []


def test_comma_and_repeat_names():
    assert vea._names(["a,b", " c "]) == ["a", "b", "c"]
    assert vea._drone_of("/cfb2/ext_pose_age", vea.TOPIC_SUFFIX) == "cfb2"
    assert vea._drone_of("/a/b/ext_pose_age", vea.TOPIC_SUFFIX) is None
    assert vea._drone_of("/markerless_extpos_bridge/status", vea.STATUS_SUFFIX) == \
        "markerless_extpos_bridge"


# --------------------------------------------------------------------------- #
# Round trip through real bags (dev container)
# --------------------------------------------------------------------------- #
def _write_bag(path, drones):
    """A real MCAP bag. ``drones`` = {name: (ages, flags)}, flags may be None."""
    rosbag2_py = pytest.importorskip("rosbag2_py", reason="needs ROS 2 (dev container)")
    msgs = pytest.importorskip("crazyflie_interfaces.msg",
                               reason="needs crazyflie_interfaces (dev container)")
    from rclpy.serialization import serialize_message

    writer = rosbag2_py.SequentialWriter()
    writer.open(rosbag2_py.StorageOptions(uri=str(path), storage_id="mcap"),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    events = []
    for name, (ages, flags) in drones.items():
        writer.create_topic(rosbag2_py.TopicMetadata(
            name=f"/{name}/ext_pose_age", type="crazyflie_interfaces/msg/LogDataGeneric",
            serialization_format="cdr"))
        for t, age in ages:
            m = msgs.LogDataGeneric()
            m.values = [float(age), 0.0]          # the yaml may log more vars after ageMs
            events.append((t, f"/{name}/ext_pose_age", m))
        if flags is not None:
            writer.create_topic(rosbag2_py.TopicMetadata(
                name=f"/{name}/status", type="crazyflie_interfaces/msg/Status",
                serialization_format="cdr"))
            for t, flying in flags:
                s = msgs.Status()
                s.supervisor_info = (msgs.Status.SUPERVISOR_INFO_IS_FLYING if flying else 0) | 1
                events.append((t, f"/{name}/status", s))
    for t, topic, m in sorted(events, key=lambda e: e[0]):
        writer.write(topic, serialize_message(m), int(t * 1e9))
    del writer
    return path


def _run(capsys, *argv):
    rc = vea.main([str(a) for a in argv])
    return rc, capsys.readouterr().out


def test_bag_healthy(tmp_path, capsys):
    bag = _write_bag(tmp_path / "healthy", {
        "lead": (_series(240, 1.0), _flags(240, 10, 230)),
        "follower": (_series(240, 2.0), _flags(240, 20, 220)),
    })
    rc, out = _run(capsys, bag, "--require", "lead,follower")
    assert rc == 0, out
    assert "lead max 1 ms median 1 ms" in out and "follower max 2 ms median 2 ms" in out


def test_bag_long_stale_run(tmp_path, capsys):
    bag = _write_bag(tmp_path / "stale", {
        "lead": (_series(240, 1.0, stale=(120, 15, 700.0)), _flags(240, 10, 230)),
    })
    rc, out = _run(capsys, bag, "--require", "lead", "--max-age-ms", "200",
                   "--min-consecutive", "3")
    assert rc == 1, out
    assert out.strip().splitlines()[0].startswith("VERIFY-FAIL: lead: pose age above 200 ms")


def test_bag_missing_topic_required(tmp_path, capsys):
    bag = _write_bag(tmp_path / "missing_req", {"lead": (_series(100), _flags(100, 5, 95))})
    rc, out = _run(capsys, bag, "--require", "lead", "--require", "cfb2")
    assert rc == 1, out
    assert "cfb2" in out.strip().splitlines()[0]


def test_bag_missing_topic_optional(tmp_path, capsys):
    bag = _write_bag(tmp_path / "missing_opt", {"lead": (_series(100), _flags(100, 5, 95))})
    rc, out = _run(capsys, bag, "--require", "lead")
    assert rc == 0, out
    rc, out = _run(capsys, bag, "--require", "lead", "--require", "cfb2",
                   "--backend", "sim")
    assert rc == 0 and "requirements waived" in out


def test_bag_expected_latency_is_added_to_the_threshold(tmp_path, capsys):
    # the position-only experiment's injected_latency_ms=180 level reads ~182 ms on board
    bag = _write_bag(tmp_path / "injected", {
        "lead": (_series(240, 1.0, stale=(10, 220, 260.0)), _flags(240, 10, 230)),
    })
    rc, out = _run(capsys, bag, "--require", "lead")
    assert rc == 1, out
    rc, out = _run(capsys, bag, "--require", "lead", "--expected-latency-ms", "180")
    assert rc == 0, out
    assert "threshold 380 ms" in out


def test_bag_summary_json_is_one_line_and_exits_zero_even_on_failure(tmp_path, capsys):
    bag = _write_bag(tmp_path / "summary", {
        "lead": (_series(240, 1.0, stale=(120, 15, 700.0)), _flags(240, 10, 230)),
    })
    rc, out = _run(capsys, bag, "--require", "lead,cfb2", "--summary-json")
    assert rc == 0
    lines = out.strip().splitlines()
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["ok"] is False
    assert summary["drones"]["lead"]["max_ms"] == 700.0
    assert summary["drones"]["cfb2"]["recorded"] is False
