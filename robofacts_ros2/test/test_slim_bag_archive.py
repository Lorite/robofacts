"""The slim copy of a recorded bag (slim_bag_archive.py), on this machine and on a remote rig.

The remote case runs the command the rig would run here: run_foreground and rsync_back are
replaced, so nothing leaves this machine. A light bag (no heavy topic) is copied whole, so no
`ros2 bag convert` is needed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from robofacts import remote_exec_utils as rx
from robofacts_ros2 import slim_bag_archive as sba

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PACKAGE_ROOT.parent / "robofacts"


def light_bag(bag: Path) -> Path:
    """A bag directory with one light topic, which the slim step copies whole."""
    bag.mkdir(parents=True)
    (bag / "metadata.yaml").write_text(
        "rosbag2_bagfile_information:\n  topics_with_message_count:\n"
        "    - topic_metadata:\n        name: /drone/pose\n", encoding="utf-8")
    (bag / f"{bag.name}_0.mcap").write_bytes(b"MCAP")
    (bag / "trial_metadata.json").write_text("{}", encoding="utf-8")
    return bag


def rig_here(monkeypatch, tmp_path: Path) -> dict:
    """The rig is this machine: the remote command runs here with this interpreter, without
    the project's shim, and rsync_back has nothing to copy."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python3 = bin_dir / "python3"
    python3.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n", encoding="utf-8")
    python3.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}",
               PYTHONPATH=os.pathsep.join([str(CORE_ROOT), str(PACKAGE_ROOT),
                                           os.environ.get("PYTHONPATH", "")]))
    seen: dict = {"pulled": []}

    def run_foreground(cmd, target, *, cwd, check=True):
        seen["cmd"] = cmd
        return subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True)

    def rsync_back(target, paths):
        seen["pulled"] += [Path(p) for p in paths]
        return {Path(p): 0 for p in paths}

    monkeypatch.setattr(rx, "run_foreground", run_foreground)
    monkeypatch.setattr(rx, "rsync_back", rsync_back)
    return seen


def target(workspace: Path) -> rx.RemoteTarget:
    return rx.RemoteTarget(host="rig", container="c", workspace=workspace,
                           ros_setup="/x", ws_setup="/y")


def test_the_remote_slim_copy_runs_the_module_of_the_ros2_package(tmp_path, monkeypatch):
    """Third review of 2026-11-25: the command named robofacts.slim_bag_archive, which does not
    exist (the module is in robofacts_ros2), so a remote trial of a project without the shim
    got no slim copy."""
    project = tmp_path / "project"
    bag = light_bag(project / "experiments" / "e" / "bags" / "t1")
    seen = rig_here(monkeypatch, tmp_path)
    out = sba.archive_and_sync(bag, target(project), project_root=project)
    assert "python3 -m robofacts_ros2.slim_bag_archive" in seen["cmd"][-1], seen["cmd"]
    assert out == project / "experiments" / "e" / "bags_slim" / "t1_slim", out
    assert (out / "t1_0.mcap").read_bytes() == b"MCAP"


def external_bag(tmp_path: Path, heavy: bool = False) -> tuple:
    """A bag recorded on a scratch "disk", linked into the repo's bags/ as the runner links an
    externally recorded bag (trial_runner: ln -sfn <disk bag> <repo>/bags/<name>)."""
    disk_bag = light_bag(tmp_path / "disk" / "e" / "bags" / "t1")
    if heavy:
        (disk_bag / "metadata.yaml").write_text(
            "rosbag2_bagfile_information:\n  topics_with_message_count:\n"
            "    - topic_metadata:\n        name: /cam/image_raw\n", encoding="utf-8")
    project = tmp_path / "project"
    bags = project / "experiments" / "e" / "bags"
    bags.mkdir(parents=True)
    link = bags / "t1"
    link.symlink_to(disk_bag, target_is_directory=True)
    return project, link, disk_bag


def test_the_slim_copy_of_an_external_bag_stays_in_the_repo(tmp_path):
    """Third review of 2026-11-25: the link was resolved first, so the slim copy landed on the
    external disk, and the path that archive_and_sync returned did not exist."""
    project, link, disk_bag = external_bag(tmp_path)
    res = sba.archive_bag(link, copy_if_light=True)
    expected = project / "experiments" / "e" / "bags_slim" / "t1_slim"
    assert res["status"] == "copied" and res["slim"] == str(expected), res
    assert (expected / "t1_0.mcap").read_bytes() == b"MCAP", "the bag was read through the link"
    assert not (disk_bag.parent.parent / "bags_slim").exists(), "a slim copy on the external disk"


def test_a_slimmed_external_bag_too(tmp_path, monkeypatch):
    """A bag with a heavy topic goes through slim(): the copy is in the repo, and slim() reads
    the bag through the repo's link."""
    project, link, _ = external_bag(tmp_path, heavy=True)
    read = []

    def fake_slim(bag_dir, *, out_dir, compress):
        read.append(Path(bag_dir))
        out_dir.mkdir(parents=True)
        (out_dir / "slim_0.mcap").write_bytes(b"S")
        return out_dir

    monkeypatch.setattr(sba, "slim", fake_slim)
    out = sba.archive_and_sync(link)
    assert out == project / "experiments" / "e" / "bags_slim" / "t1_slim" and out.is_dir(), out
    assert read == [link]
    assert (out / "trial_metadata.json").is_file(), "the sidecars come with it"


def test_a_remote_external_bag_is_slimmed_where_the_rsync_pulls_from(tmp_path, monkeypatch):
    project, link, disk_bag = external_bag(tmp_path)
    seen = rig_here(monkeypatch, tmp_path)
    out = sba.archive_and_sync(link, target(project), project_root=project)
    assert seen["pulled"] == [project / "experiments" / "e" / "bags_slim"]
    assert out is not None and (out / "t1_0.mcap").is_file(), "the returned slim copy does not exist"
    assert not (disk_bag.parent.parent / "bags_slim").exists()
