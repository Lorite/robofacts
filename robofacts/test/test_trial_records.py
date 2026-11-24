"""Tests for trial_records: the per-trial join behind `trials records` and the web UI's table (#131).

Each test builds the on-disk shapes the runner and the analyses really write (bags,
slim copies, the attempt ledger, the gate log, run logs, result folders) and checks
that one record carries all of them, so the UI and the command line can never tell
two stories about one trial.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from robofacts import campaign_spec as cs
from robofacts import trial_records as trec

FACTORS = """
version: 1
factors:
  execution_host:
    levels: [laptop, orin]
    default: orin
    metadata_key: execution_host
    maps_to: {kind: host}
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
  trajectory: {required: false, type: str, factor: true}
  aborted: {required: true, type: bool}
"""

CAMPAIGN = """
name: c
experiment: expA
repetitions: 2
sweep:
  trajectory: [hover, circle]
fixed:
  execution_host: laptop
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "common" / "config").mkdir(parents=True)
    (tmp_path / "common" / "config" / "factors.yaml").write_text(FACTORS, encoding="utf-8")
    (tmp_path / "common" / "config" / "trial_metadata_core.yaml").write_text(CONTRACT, encoding="utf-8")
    (tmp_path / "expA" / "campaigns").mkdir(parents=True)
    (tmp_path / "expA" / "campaigns" / "c.yaml").write_text(CAMPAIGN, encoding="utf-8")
    (tmp_path / "expA" / "config").mkdir(parents=True)
    (tmp_path / "expA" / "runs").mkdir(parents=True)
    return tmp_path


def bag(root: Path, name: str, folder: str = "bags", slim: bool = False, **metadata) -> Path:
    meta = {"trial_id": name, "execution_host": "laptop", "aborted": False, "trajectory": "hover"}
    meta.update(metadata)
    d = root / "expA" / folder / name
    d.mkdir(parents=True)
    (d / "trial_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / f"{name}_0.mcap").write_bytes(b"mcap")
    if slim:
        s = root / "expA" / (folder.replace("bags", "bags", 1) + "_slim") / f"{name}_slim"
        s.mkdir(parents=True)
        (s / f"{name}_slim_0.mcap").write_bytes(b"slim")
        (s / "trial_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


def ledger(root: Path, **row) -> None:
    with (root / "expA" / "runs" / "attempts.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def gate_line(root: Path, **row) -> None:
    with (root / "expA" / "runs" / "preflight_gate.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def by_name(records):
    return {r.name: r for r in records}


def campaigns(root: Path):
    return {"c": cs.load_campaign(root / "expA" / "campaigns" / "c.yaml", root)}


# --------------------------------------------------------------------------- #
def test_one_record_carries_the_campaign_slim_copy_run_log_and_gate(root):
    bag(root, "20261015T090000_rep01", slim=True,
        preflight={"verdict": "GO", "mode": "binding", "n_checks": 7, "failed_checks": []})
    ledger(root, trial_label="20261015T090000_rep01", campaign="c", status="ran",
           ts_start="2026-10-15T09:00:00.000+00:00", values={"trajectory": "hover"})
    (root / "expA" / "runs" / "20261015T090000_rep01.log").write_text(
        "[runner] go\n[verify] flight_happened\n[verify] flight_happened rc=0 in 0.6 s\n",
        encoding="utf-8")

    r = by_name(trec.load_records(root, "expA", campaigns=campaigns(root)))["20261015T090000_rep01"]
    assert (r.status, r.kind, r.campaign) == ("usable", "campaign", "c")
    assert r.slim_name == "20261015T090000_rep01_slim"
    assert r.run_log is not None and r.gate["verdict"] == "GO" and r.gate["n_checks"] == 7
    assert r.counts_for == ["c"], "the plan's own matching decides what a trial counts for"
    assert r.counts
    assert trec.verify_report(r.run_log) == [
        {"name": "flight_happened", "rc": 0, "seconds": "0.6", "ok": True}]


def test_a_slim_only_trial_is_one_record_not_two(root):
    """A remote trial syncs only its slim copy back: the record has no full bag."""
    s = root / "expA" / "bags_slim" / "t_remote_slim"
    s.mkdir(parents=True)
    (s / "d_0.mcap").write_bytes(b"x")
    (s / "trial_metadata.json").write_text(json.dumps(
        {"trial_id": "t", "execution_host": "laptop", "aborted": False}), encoding="utf-8")
    bag(root, "t_local", slim=True)
    records = trec.load_records(root, "expA")
    assert sorted(r.name for r in records) == ["t_local", "t_remote"]
    remote = by_name(records)["t_remote"]
    assert remote.bag_dir is None and remote.slim_name == "t_remote_slim"


def test_a_blocked_attempt_is_a_record_with_its_failed_checks(root):
    ledger(root, trial_label="20261015T100000_rep01", campaign="c", status="blocked",
           ts_start="2026-10-15T10:00:00", values={"trajectory": "circle"},
           detail="pre-flight gate FAIL: topic_rate", gate={"verdict": "NO-GO", "mode": "binding",
                                                            "failed_checks": ["topic_rate: 0 Hz"]})
    r = by_name(trec.load_records(root, "expA"))["20261015T100000_rep01"]
    assert (r.status, r.campaign) == ("blocked", "c")
    assert r.gate["verdict"] == "NO-GO" and r.gate["failed"] == ["topic_rate: 0 Hz"]
    assert r.values == {"trajectory": "circle"} and "topic_rate" in r.reason
    assert not r.counts and r.bag_dir is None


def test_aborted_and_quarantined_are_told_apart(root):
    bag(root, "t_abort", aborted=True, abort_reason="action returned rc=2")
    bag(root, "t_quar", folder="bags_issues")
    records = by_name(trec.load_records(root, "expA"))
    assert (records["t_abort"].status, records["t_abort"].reason) == ("aborted", "action returned rc=2")
    assert records["t_quar"].status == "quarantined"


def test_scratch_flights_are_listed_with_their_slim_copies_and_never_count(root):
    for name, slim_dir in (("20261015T110000_scratch_a", "bags_scratch"),
                           ("20261015T120000_scratch_b", "bags_scratch_slim")):
        full = root / "expA" / "bags_scratch" / name
        full.mkdir(parents=True)
        (full / "x_0.mcap").write_bytes(b"x")
        (full / "trial_metadata.json").write_text(json.dumps(
            {"trial_id": name, "scratch": True, "aborted": False}), encoding="utf-8")
        slim = root / "expA" / slim_dir / f"{name}_slim"
        slim.mkdir(parents=True)
        (slim / "x_0.mcap").write_bytes(b"s")
    records = by_name(trec.load_records(root, "expA", campaigns=campaigns(root)))
    for name in ("20261015T110000_scratch_a", "20261015T120000_scratch_b"):
        r = records[name]
        assert (r.kind, r.status, r.counts, r.counts_for) == ("scratch", "scratch", False, [])
        assert r.slim_name == f"{name}_slim", "both slim locations are found"
    assert len(records) == 2, "a slim copy is never a record of its own"


def test_analyses_are_joined_by_metadata_subfolder_or_name_and_videos_stay_per_trial(root):
    bag(root, "t1")
    bag(root, "t2")
    results = root / "expA" / "results"
    cohort = results / "20261015T130000_cohort"
    cohort.mkdir(parents=True)
    (cohort / "summary.mp4").write_bytes(b"not this trial's video")
    (cohort / "analysis_metadata.json").write_text(json.dumps(
        {"analysis": "cohort", "bags": ["t1", "t2"], "generated_at": "20261015T130000",
         "selection": {"where": {}}, "failed_commands": 0}), encoding="utf-8")
    per_bag = results / "20261015T140000_video" / "t1"
    per_bag.mkdir(parents=True)
    (per_bag / "trial_video_h264.mp4").write_bytes(b"v")
    (per_bag / "trial_video.mp4").write_bytes(b"v")
    (results / "t2_video").mkdir()   # a folder named <bag>_<what>, made by hand
    (results / "t2_video" / "clip.mp4").write_bytes(b"v")
    bag(root, "t3")
    (results / "t3").mkdir()   # a folder named exactly like the bag (2026-10-19, by hand)
    (results / "t3" / "trial_video.mp4").write_bytes(b"v")

    records = by_name(trec.load_records(root, "expA"))
    t1 = {a["analysis"]: a for a in records["t1"].analyses}
    assert set(t1) == {"cohort", "video"}
    assert t1["cohort"]["per"] == "cohort" and t1["cohort"]["n_bags"] == 2
    assert t1["video"]["path"] == per_bag
    assert [v.name for v in records["t1"].videos] == ["trial_video_h264.mp4", "trial_video.mp4"], \
        "H.264 first (it plays in a browser), and a cohort folder's video is not the trial's"
    assert "cohort" in {a["analysis"] for a in records["t2"].analyses}
    named = [a for a in records["t2"].analyses if a["folder"].name == "t2_video"]
    assert named and named[0]["analysis"] == "video"
    assert [v.name for v in records["t2"].videos] == ["clip.mp4"]
    assert [a["analysis"] for a in records["t3"].analyses] == ["video"]
    assert [v.name for v in records["t3"].videos] == ["trial_video.mp4"], \
        "results/<bag>/ is that trial's folder too (the card showed no video, 2026-10-20)"


def test_filters_are_the_cli_flags(root):
    bag(root, "t_hover")
    bag(root, "t_circle", trajectory="circle")
    bag(root, "t_abort", aborted=True)
    ledger(root, trial_label="t_hover", campaign="other", status="ran")
    records = trec.load_records(root, "expA", campaigns=campaigns(root))
    names = lambda rs: sorted(r.name for r in rs)
    assert names(trec.filter_records(records, where={"trajectory": "circle"})) == ["t_circle"]
    assert names(trec.filter_records(records, status="aborted")) == ["t_abort"]
    assert names(trec.filter_records(records, status="excluded-any")) == ["t_abort"]
    # Flown by another campaign, but in a cell of c: the campaign filter keeps it.
    assert "t_hover" in names(trec.filter_records(records, campaign="c"))
    assert names(trec.filter_records(records, campaign="other")) == ["t_hover"]


def test_campaign_gate_outcomes_count_verdicts_and_failing_checks(root):
    for i, verdict in enumerate(["GO", "NO-GO", "NO-GO", "GO"]):
        gate_line(root, ts=f"2026-10-15T09:0{i}:00", campaign="c", trial_label=f"t{i}",
                  verdict=verdict, failed=[{"check": "topic_rate", "detail": "0 Hz"}]
                  if verdict == "NO-GO" else [])
    gate_line(root, ts="2026-10-15T10:00:00", campaign="other", trial_label="x", verdict="NO-GO")
    ledger(root, trial_label="t1", campaign="c", status="blocked")
    out = trec.campaign_gate_outcomes(root, "expA", "c")
    assert out["counts"] == {"GO": 2, "NO-GO": 2} and out["total"] == 4
    assert out["failing"] == [("topic_rate", 2)] and out["blocked_attempts"] == 1
    assert out["recent"][0]["trial_label"] == "t3", "newest first"


def test_records_serialize_to_json(root):
    """Regression (2026-10-16): the per-analysis video paths stayed Path objects, so
    `trials records --json` and the UI's records API failed on any trial with a video."""
    bag(root, "t1")
    d = root / "expA" / "results" / "20261015T140000_video" / "t1"
    d.mkdir(parents=True)
    (d / "v.mp4").write_bytes(b"v")
    json.dumps([r.to_dict(root) for r in trec.load_records(root, "expA")])


def test_the_cli_prints_the_same_records(root, capsys):
    bag(root, "t1", slim=True)
    ledger(root, trial_label="t_blocked", campaign="c", status="blocked")
    assert trec.main(["--experiments-root", str(root), "expA", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert sorted(r["name"] for r in rows) == ["t1", "t_blocked"]
    assert trec.main(["--experiments-root", str(root), "expA", "--status", "blocked"]) == 0
    out = capsys.readouterr().out
    assert "t_blocked" in out and "1 record(s): 1 attempt(s) without a trial (1 blocked)" in out
    assert trec.main(["--experiments-root", str(root), "expA", "--where", "nonsense"]) == 2
    assert trec.main(["--experiments-root", str(root), "nope"]) == 2


def test_trials_records_is_a_subcommand():
    from robofacts import cli
    assert "records" in cli.COMMANDS


def test_every_record_has_the_papers_class_or_none_for_an_attempt(root):
    """The paper's Equation 4: usable, unverified or excluded (aborted, quarantined or no
    data); an attempt the gate refused left no trial, so it has no class (2026-10-16)."""
    bag(root, "t_ok")
    bag(root, "t_abort", aborted=True)
    bag(root, "t_quar", folder="bags_issues")
    bag(root, "t_scratch_flag", scratch=True)
    ledger(root, trial_label="t_nogo", campaign="c", status="blocked")
    records = by_name(trec.load_records(root, "expA"))
    classes = {n: (r.trial_class, trec.class_label(r)) for n, r in records.items()}
    assert classes == {
        "t_ok": ("usable", "usable"),
        "t_abort": ("excluded", "excluded (aborted)"),
        "t_quar": ("excluded", "excluded (quarantined)"),
        "t_scratch_flag": ("excluded", "excluded (scratch)"),
        "t_nogo": (None, "attempt: blocked"),
    }
    names = lambda status: sorted(r.name for r in trec.filter_records(records.values(), status=status))
    assert names("excluded") == ["t_abort", "t_quar", "t_scratch_flag"]
    assert names("attempt") == ["t_nogo"]
    assert names("excluded-any") == ["t_abort", "t_nogo", "t_quar", "t_scratch_flag"]
    assert trec.summary_line(list(records.values())) == (
        "5 record(s): 1 usable, 3 excluded (1 aborted, 1 quarantined, 1 scratch), "
        "1 attempt(s) without a trial (1 blocked)")
    assert records["t_abort"].to_dict()["class"] == "excluded"


# --------------------------------------------------------------------------- #
# Poster frames of the card view (#137)
# --------------------------------------------------------------------------- #
needs_ffmpeg = pytest.mark.skipif(__import__("shutil").which("ffmpeg") is None,
                                  reason="poster frames need ffmpeg (present in the dev container)")


def make_video(path: Path, seconds: float = 4.0) -> Path:
    """A real, tiny H.264 test video, so the poster is made by the real ffmpeg."""
    import subprocess
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size=320x180:rate=10:duration={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_the_poster_path_mirrors_the_video_path_and_maps_back(root):
    exp = root / "expA"
    video = exp / "results" / "20261015T140000_video" / "t1" / "trial_video_h264.mp4"
    poster = trec.poster_path(exp, video)
    assert poster == exp / "runs" / "posters" / "results" / "20261015T140000_video" / "t1" \
        / "trial_video_h264.mp4.jpg", "two videos with one file name never share a poster"
    assert trec.video_of_poster(exp, poster) == video
    assert trec.video_of_poster(exp, exp / "runs" / "t1.log") is None
    assert trec.video_of_poster(exp, exp / "runs" / "posters" / "x.png.jpg") is None


@needs_ffmpeg
def test_a_poster_is_made_once_and_cached(root):
    exp = root / "expA"
    video = make_video(exp / "results" / "r" / "t1" / "v_h264.mp4")
    poster = trec.make_poster(exp, video)
    assert poster is not None and poster.read_bytes()[:2] == b"\xff\xd8", "a JPEG"
    stamp = poster.stat().st_mtime_ns
    assert trec.make_poster(exp, video, ffmpeg="/nonexistent/ffmpeg") == poster, \
        "the cached poster is served without running ffmpeg again"
    assert poster.stat().st_mtime_ns == stamp
    assert not list(poster.parent.glob("tmp*")), "no temporary file is left behind"


@needs_ffmpeg
def test_a_video_shorter_than_the_poster_time_falls_back_to_its_first_frame(root):
    exp = root / "expA"
    video = make_video(exp / "results" / "r" / "short.mp4", seconds=1.0)
    assert trec.make_poster(exp, video, at=3.0) is not None


@needs_ffmpeg
def test_a_re_rendered_video_gets_a_new_poster(root):
    import os
    exp = root / "expA"
    video = make_video(exp / "results" / "r" / "v.mp4")
    poster = trec.make_poster(exp, video)
    old = poster.stat().st_mtime - 100
    os.utime(poster, (old, old))
    assert trec.make_poster(exp, video).stat().st_mtime > old


def test_no_poster_without_ffmpeg_or_from_a_broken_video(root):
    exp = root / "expA"
    broken = exp / "results" / "r" / "broken.mp4"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a video")
    assert trec.make_poster(exp, broken, ffmpeg="/nonexistent/ffmpeg") is None
    assert trec.make_poster(exp, exp / "results" / "r" / "missing.mp4") is None
    if __import__("shutil").which("ffmpeg"):
        assert trec.make_poster(exp, broken) is None
    assert not trec.poster_path(exp, broken).exists()


def test_the_record_names_its_poster_and_the_json_carries_it(root):
    bag(root, "t1")
    bag(root, "t2")
    d = root / "expA" / "results" / "20261015T140000_video" / "t1"
    d.mkdir(parents=True)
    (d / "v_h264.mp4").write_bytes(b"v")
    records = by_name(trec.load_records(root, "expA"))
    assert records["t1"].poster(root) == trec.poster_path(root / "expA", d / "v_h264.mp4")
    assert records["t2"].poster(root) is None, "no video, no poster"
    as_json = {r["name"]: r for r in json.loads(json.dumps(
        [r.to_dict(root) for r in records.values()]))}
    assert as_json["t1"]["poster"] == "expA/runs/posters/results/20261015T140000_video/t1/v_h264.mp4.jpg"
    assert as_json["t2"]["poster"] is None


@needs_ffmpeg
def test_trials_records_posters_makes_them_ahead_of_the_ui(root, capsys):
    bag(root, "t1")
    video = make_video(root / "expA" / "results" / "20261015T140000_video" / "t1" / "v.mp4")
    assert trec.main(["--experiments-root", str(root), "expA", "--posters"]) == 0
    assert trec.poster_path(root / "expA", video).is_file()
    assert "posters: 1 of 1" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Stills, the camera video and the plot fallback of the card view (#138)
# --------------------------------------------------------------------------- #
from types import SimpleNamespace  # noqa: E402

from robofacts import trial_media as tm  # noqa: E402

needs_cv2 = pytest.mark.skipif(__import__("importlib").util.find_spec("cv2") is None,
                               reason="decoding camera frames needs cv2 (present in the dev container)")
needs_rosbag2 = pytest.mark.skipif(__import__("importlib").util.find_spec("rosbag2_py") is None,
                                   reason="writing a real bag needs ROS (the dev container)")

CAMERA_YAML = """
camera:
  topic: /drone/camera/image
  label: drone camera
  stills: 4
"""


def declare_camera(root: Path, text: str = CAMERA_YAML) -> None:
    (root / "expA" / "config" / "trial_media.yaml").write_text(text, encoding="utf-8")


def bag_metadata(bag_dir: Path, topic: str = "/drone/camera/image", count: int = 40,
                 msg_type: str = "sensor_msgs/msg/Image", seconds: float = 4.0) -> None:
    """The part of rosbag2's metadata.yaml that trial_media reads."""
    import yaml
    info = {"rosbag2_bagfile_information": {
        "duration": {"nanoseconds": int(seconds * 1e9)},
        "topics_with_message_count": [
            {"topic_metadata": {"name": "/tf", "type": "tf2_msgs/msg/TFMessage"}, "message_count": 9},
            {"topic_metadata": {"name": topic, "type": msg_type}, "message_count": count}]}}
    (bag_dir / "metadata.yaml").write_text(yaml.safe_dump(info), encoding="utf-8")


def mono_frame(value: int, w: int = 30, h: int = 24, pad: int = 2) -> SimpleNamespace:
    """A mono8 sensor_msgs/Image with a padded row (step > width), as drivers send them."""
    row = bytes([value]) * w + b"\x00" * pad
    return SimpleNamespace(encoding="mono8", width=w, height=h, step=w + pad,
                           is_bigendian=0, data=row * h)


def test_an_experiment_without_trial_media_yaml_declares_no_camera(root):
    assert tm.load_camera(root / "expA") is None
    declare_camera(root)
    spec = tm.load_camera(root / "expA")
    assert spec == tm.CameraSpec(topic="/drone/camera/image", label="drone camera", stills=4)


@pytest.mark.parametrize("text, why", [
    ("camera: {label: x}", "camera.topic"),
    ("camera: {topic: relative/topic}", "camera.topic"),
    ("camera: {topic: /a, colour: red}", "unknown key"),
    ("camera: {topic: /a, stills: 100}", "camera.stills"),
    ("camera: [", "does not load"),
])
def test_a_broken_trial_media_yaml_says_what_is_wrong(root, text, why):
    declare_camera(root, text)
    with pytest.raises(tm.MediaConfigError, match=why):
        tm.load_camera(root / "expA")
    assert tm.load_camera_or_none(root / "expA") is None, "readers never fail on it"


def test_stills_are_evenly_spaced_and_never_more_than_the_frames():
    assert tm.still_indices(80, 8) == [5, 15, 25, 35, 45, 55, 65, 75]
    assert tm.still_indices(3, 8) == [0, 1, 2]
    assert tm.still_indices(0, 8) == [] and tm.still_indices(10, 0) == []


def test_the_source_is_the_first_bag_that_records_the_topic(root):
    full = bag(root, "t1", slim=True)
    slim = root / "expA" / "bags_slim" / "t1_slim"
    bag_metadata(full)
    bag_metadata(slim, topic="/other")
    assert tm.source_bag([slim, full], "/drone/camera/image")[0] == full
    bag_metadata(slim)
    source, info = tm.source_bag([slim, full], "/drone/camera/image")
    assert source == slim and info == {"type": "sensor_msgs/msg/Image", "count": 40, "duration_s": 4.0}
    assert tm.source_bag([None, root / "missing"], "/drone/camera/image") == (None, None)


@needs_cv2
def test_frames_of_every_common_encoding_decode_to_bgr():
    import cv2
    import numpy as np
    assert tm.to_bgr(mono_frame(200), "sensor_msgs/msg/Image").shape == (24, 30, 3)
    rgb = SimpleNamespace(encoding="rgb8", width=2, height=1, step=6, is_bigendian=0,
                          data=bytes([255, 0, 0, 0, 0, 255]))
    assert tm.to_bgr(rgb, "sensor_msgs/msg/Image")[0].tolist() == [[0, 0, 255], [255, 0, 0]]
    deep = SimpleNamespace(encoding="16UC1", width=2, height=1, step=4, is_bigendian=0,
                           data=np.array([0, 1000], "<u2").tobytes())
    assert tm.to_bgr(deep, "sensor_msgs/msg/Image")[0, 1].tolist() == [255, 255, 255]
    ok, jpg = cv2.imencode(".jpg", np.full((10, 12, 3), 128, np.uint8))
    compressed = SimpleNamespace(format="jpeg", data=jpg.tobytes())
    assert tm.to_bgr(compressed, "sensor_msgs/msg/CompressedImage").shape == (10, 12, 3)
    odd = SimpleNamespace(encoding="yuv422", width=2, height=1, step=4, is_bigendian=0, data=b"\0" * 4)
    assert tm.to_bgr(odd, "sensor_msgs/msg/Image") is None, "an unknown encoding is skipped"


@needs_cv2
@needs_ffmpeg
def test_make_writes_the_stills_the_video_and_camera_json_once(root, monkeypatch):
    declare_camera(root)
    full = bag(root, "t1", slim=True)
    slim = root / "expA" / "bags_slim" / "t1_slim"
    bag_metadata(slim)
    reads = []

    def frames(source, topic):
        reads.append(source)
        for i in range(40):
            yield mono_frame(i * 6)
    monkeypatch.setattr(tm, "_read_frames", frames)
    spec = tm.load_camera(root / "expA")
    result = tm.make(root / "expA", "t1", [slim, full], spec)
    assert reads == [slim], "the slim copy is read when it carries the topic"
    assert result["stills"] == ["still_01.jpg", "still_02.jpg", "still_03.jpg", "still_04.jpg"]
    assert result["frames"] == 40 and result["fps"] == 10.0 and result["video"] == "camera.mp4"
    assert result["source"] == "bags_slim/t1_slim" and result["label"] == "drone camera"
    stills, video, info = tm.cached(root / "expA", "t1")
    assert [p.name for p in stills] == result["stills"] and video.stat().st_size > 0
    assert info["width"] == 30 and info["height"] == 24
    assert not [p for p in (root / "expA" / "runs" / "camera").iterdir() if p.name.startswith(".")], \
        "no temporary folder is left behind"
    tm.make(root / "expA", "t1", [slim, full], spec)
    assert len(reads) == 1, "a current cache is not made again"
    tm.make(root / "expA", "t1", [slim, full], spec, force=True)
    assert len(reads) == 2


@needs_cv2
def test_a_bag_without_the_topic_gets_nothing_and_stills_need_no_video(root, monkeypatch):
    declare_camera(root, CAMERA_YAML + "  video: false\n")
    full = bag(root, "t1")
    assert tm.make(root / "expA", "t1", [None, full], tm.load_camera(root / "expA")) is None
    bag_metadata(full)
    monkeypatch.setattr(tm, "_read_frames", lambda s, t: (mono_frame(100) for _ in range(40)))
    result = tm.make(root / "expA", "t1", [None, full], tm.load_camera(root / "expA"))
    assert len(result["stills"]) == 4 and result["video"] is None and result["video_error"] is None


@needs_rosbag2
@needs_cv2
@needs_ffmpeg
@pytest.mark.parametrize("compressed", [False, True])
def test_stills_and_video_come_from_a_real_bag(tmp_path, compressed):
    """End to end through rosbag2: a recorded Image or CompressedImage topic."""
    import cv2
    import numpy as np
    import rosbag2_py
    from rclpy.serialization import serialize_message
    from sensor_msgs.msg import CompressedImage, Image
    exp = tmp_path / "exp"
    bag_dir = exp / "bags" / "t1"
    bag_dir.parent.mkdir(parents=True)
    writer = rosbag2_py.SequentialWriter()
    writer.open(rosbag2_py.StorageOptions(uri=str(bag_dir), storage_id="mcap"),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    msg_type = "sensor_msgs/msg/CompressedImage" if compressed else "sensor_msgs/msg/Image"
    writer.create_topic(rosbag2_py.TopicMetadata(name="/cam", type=msg_type, serialization_format="cdr"))
    for i in range(30):
        pixels = np.full((24, 32), i * 8, np.uint8)
        if compressed:
            msg = CompressedImage(format="jpeg", data=cv2.imencode(".jpg", pixels)[1].tobytes())
        else:
            msg = Image(height=24, width=32, encoding="mono8", step=32, data=pixels.tobytes())
        writer.write("/cam", serialize_message(msg), int(1e9 + i * 1e8))
    del writer
    spec = tm.CameraSpec(topic="/cam", label="drone camera", stills=5)
    result = tm.make(exp, "t1", [bag_dir], spec)
    assert result["frames"] == 30 and result["decoded"] == 30 and len(result["stills"]) == 5
    assert result["video"] == "camera.mp4" and result["video_error"] is None
    stills, _video, _info = tm.cached(exp, "t1")
    means = [cv2.imread(str(p), cv2.IMREAD_GRAYSCALE).mean() for p in stills]
    assert means == sorted(means), "the stills are in time order, spread over the topic"


def test_the_record_carries_its_stills_camera_video_and_label(root):
    declare_camera(root)
    bag(root, "t1")
    folder = root / "expA" / "runs" / "camera" / "t1"
    folder.mkdir(parents=True)
    for i in (1, 2):
        (folder / f"still_0{i}.jpg").write_bytes(b"\xff\xd8")
    (folder / "camera.mp4").write_bytes(b"v")
    (folder / "camera.json").write_text(json.dumps({"label": "drone camera"}), encoding="utf-8")
    record = by_name(trec.load_records(root, "expA"))["t1"]
    assert [p.name for p in record.stills] == ["still_01.jpg", "still_02.jpg"]
    assert record.camera_video == folder / "camera.mp4" and record.camera_label == "drone camera"
    assert record.videos == [], "the camera video is not the trial video"
    as_json = record.to_dict(root)
    assert as_json["stills"] == ["expA/runs/camera/t1/still_01.jpg", "expA/runs/camera/t1/still_02.jpg"]
    assert as_json["camera_video"] == "expA/runs/camera/t1/camera.mp4"
    assert as_json["camera_label"] == "drone camera" and as_json["plots"] == []
    assert "stills=2" in trec.render([record]) and "drone_camera_video" in trec.render([record])


def test_a_trial_lists_its_own_plots_first_then_its_cohorts_plots(root):
    for name in ("t1", "t2", "t3"):
        bag(root, name)
    own = root / "expA" / "results" / "20261015T140000_vio_drift" / "t1"
    own.mkdir(parents=True)
    (own / "vio_drift.png").write_bytes(b"png")
    (own / "error.png").write_bytes(b"png")
    cohort = root / "expA" / "results" / "20261015T150000_registration_cohort"
    cohort.mkdir(parents=True)
    (cohort / "analysis_metadata.json").write_text(json.dumps(
        {"analysis": "registration_cohort", "bags": ["t1", "t2", "t3"]}), encoding="utf-8")
    (cohort / "cohort_error.png").write_bytes(b"png")
    (cohort / "t3_trajectory.png").write_bytes(b"png")
    records = by_name(trec.load_records(root, "expA"))
    assert [(p["path"].name, p["cohort"]) for p in records["t1"].plots] == [
        ("error.png", None), ("vio_drift.png", None), ("cohort_error.png", 3)], \
        "own plots first, the cohort's plot after them"
    assert [(p["path"].name, p["cohort"]) for p in records["t3"].plots] == [
        ("t3_trajectory.png", None), ("cohort_error.png", 3)], "a cohort plot named after the trial is its own"
    assert [p["analysis"] for p in records["t2"].plots] == ["registration_cohort"]
    assert records["t2"].to_dict(root)["plots"] == [{
        "path": "expA/results/20261015T150000_registration_cohort/cohort_error.png",
        "name": "cohort_error", "analysis": "registration_cohort", "cohort": 3}]
    older = root / "expA" / "results" / "20261015T130000_vio_drift" / "t1"
    older.mkdir(parents=True)
    (older / "vio_drift.png").write_bytes(b"png")
    (older / "speed.png").write_bytes(b"png")
    plots = by_name(trec.load_records(root, "expA"))["t1"].plots
    results = root / "expA" / "results"
    assert [(p["name"], str(p["path"].relative_to(results))) for p in plots] == [
        ("error", "20261015T140000_vio_drift/t1/error.png"),
        ("vio_drift", "20261015T140000_vio_drift/t1/vio_drift.png"),
        ("speed", "20261015T130000_vio_drift/t1/speed.png"),
        ("cohort_error", "20261015T150000_registration_cohort/cohort_error.png")], \
        "the older run's vio_drift is superseded, its other plot is kept"
    for i in range(6):
        (own / f"extra_{i}.png").write_bytes(b"png")
    assert len(by_name(trec.load_records(root, "expA"))["t1"].plots) == tm.MAX_PLOTS


def test_trials_records_stills_needs_a_declared_camera(root, capsys):
    bag(root, "t1")
    assert trec.main(["--experiments-root", str(root), "expA", "--stills"]) == 2
    assert "declares no camera" in capsys.readouterr().err


def test_trials_records_stills_makes_them_for_every_bag_with_the_topic(root, capsys, monkeypatch):
    declare_camera(root)
    bag_metadata(bag(root, "t1"))
    bag(root, "t2")
    made = []

    def fake_make(exp_dir, trial, candidates, spec, force=False):
        made.append(trial)
        return {"stills": ["still_01.jpg"], "video": None, "video_error": None,
                "source": f"bags/{trial}", "seconds": 0.1}
    monkeypatch.setattr(tm, "make", fake_make)
    assert trec.main(["--experiments-root", str(root), "expA", "--stills", "--json"]) == 0
    out = capsys.readouterr()
    assert made == ["t1"]
    assert "made 1, already current 0, failed 0, 1 trial(s) whose bag does not record it" in out.err
    assert {r["name"] for r in json.loads(out.out)} == {"t1", "t2"}, "--json stays clean"


def test_the_runner_hook_never_raises(root, monkeypatch, capsys):
    tm.make_after_trial(None)
    slim = root / "expA" / "bags_slim" / "t1_slim"
    slim.mkdir(parents=True)
    tm.make_after_trial(slim)
    assert capsys.readouterr().out == "", "no camera declared, nothing said"
    declare_camera(root)
    monkeypatch.setattr(tm, "make", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    tm.make_after_trial(slim)
    assert "[media] skipped (boom)" in capsys.readouterr().out
    declare_camera(root, CAMERA_YAML + "  after_trial: false\n")
    tm.make_after_trial(slim)
    assert capsys.readouterr().out == ""


def test_the_first_alternative_a_bag_records_wins_with_its_own_label(root, monkeypatch):
    declare_camera(root, """
camera:
  label: drone camera
  stills: 2
  video: false
  topic:
    - {topic: /cam/overlay/compressed, label: detector overlay}
    - /cam/image_raw
""")
    spec = tm.load_camera(root / "expA")
    assert spec.alternatives == (("/cam/overlay/compressed", "detector overlay"),
                                 ("/cam/image_raw", "drone camera"))
    assert (spec.topic, spec.label) == ("/cam/overlay/compressed", "detector overlay")
    full = bag(root, "t1", slim=True)
    slim = root / "expA" / "bags_slim" / "t1_slim"
    bag_metadata(slim, topic="/cam/image_raw")
    bag_metadata(full, topic="/cam/overlay/compressed", msg_type="sensor_msgs/msg/CompressedImage")
    assert tm.find_source([slim, full], spec)[2:] == ("/cam/overlay/compressed", "detector overlay"), \
        "the declared order wins over the slim copy"
    bag_metadata(full, topic="/other")
    source, _info, topic, label = tm.find_source([slim, full], spec)
    assert (source, topic, label) == (slim, "/cam/image_raw", "drone camera")
    monkeypatch.setattr(tm, "_read_frames", lambda s, t: (mono_frame(9) for _ in range(40)))
    if __import__("importlib").util.find_spec("cv2") is not None:
        result = tm.make(root / "expA", "t1", [slim, full], spec)
        assert result["topic"] == "/cam/image_raw" and result["label"] == "drone camera"
        assert by_name(trec.load_records(root, "expA"))["t1"].camera_label == "drone camera"


@pytest.mark.parametrize("topic", ["[relative/x]", "[{topic: /a, colour: red}]", "[]"])
def test_a_broken_alternative_is_refused(root, topic):
    declare_camera(root, f"camera: {{topic: {topic}}}\n")
    with pytest.raises(tm.MediaConfigError):
        tm.load_camera(root / "expA")
