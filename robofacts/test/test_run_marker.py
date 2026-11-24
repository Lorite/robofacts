"""The run marker (run_marker.py, 2026-10-22): present while a trial or a
campaign runs, shared by nested blocks, gone afterwards, stale after 12 h."""
import os
import time

from robofacts import run_marker


def test_nested_blocks_share_one_marker_and_remove_it_at_the_end(tmp_path):
    path = run_marker.marker_path(tmp_path)
    with run_marker.running(tmp_path, experiment="expA", campaign="c"):
        assert run_marker.read(tmp_path)["campaign"] == "c"
        with run_marker.running(tmp_path, experiment="expA", label="t1"):
            assert run_marker.read(tmp_path)["campaign"] == "c", "the outer marker stays"
        assert path.exists(), "the gap between two trials is still covered"
    assert not path.exists()


def test_the_marker_is_removed_when_the_trial_raises(tmp_path):
    try:
        with run_marker.running(tmp_path, experiment="expA"):
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    assert not run_marker.marker_path(tmp_path).exists()


def test_a_marker_left_by_a_killed_runner_goes_stale(tmp_path):
    path = run_marker.marker_path(tmp_path)
    path.write_text('{"pid": 1}')
    assert run_marker.read(tmp_path) == {"pid": 1}
    old = time.time() - run_marker.STALE_AFTER_S - 60
    os.utime(path, (old, old))
    assert run_marker.read(tmp_path) is None


def test_an_executed_trial_holds_the_marker_and_a_dry_run_does_not(tmp_path, monkeypatch):
    from robofacts import campaign_spec as cs
    from robofacts import trial_runner as tr
    bags = tmp_path / "expA" / "bags"
    seen = []
    monkeypatch.setattr(tr, "_run_trial_logged",
                        lambda *a, **k: seen.append(run_marker.read(tmp_path)))
    trial = cs.TrialSpec("expA", {}, [])
    tr.run_trial(trial, {}, None, bags, campaign_name="c", dry_run=False)
    tr.run_trial(trial, {}, None, bags, campaign_name="c", dry_run=True)
    assert seen[0]["experiment"] == "expA" and seen[0]["campaign"] == "c"
    # The stop resolver reads where the trial runs (host_registry.robot_targets).
    assert "execution_host" in seen[0] and seen[0]["simulation"] is False
    assert seen[1] is None
    assert not run_marker.marker_path(tmp_path).exists()
