"""A trial id must fit the filesystem, however many factors an experiment grows.

WHY. On 2026-08-19 three factors were added to the spot experiment (arm_aim,
max_pose_age_s, ext_pos_std_dev) and a markerless trial id reached 260 bytes.
ext4 allows 255 per path component, so the trial died mid-run with

    OSError: [Errno 36] File name too long

*after* the stack was up, the T-0 gate had passed and Spot had been readied - the
most expensive moment to discover it. Every factor added to an experiment
lengthens every id that varies it, so the ceiling belongs in a test rather than
in the memory of whoever adds the next axis.
"""
import sys
from pathlib import Path

import pytest

from robofacts import trial_runner as tr
class _Trial:
    def __init__(self, values, repetition=1):
        self.values = values
        self.non_defaults = set(values)
        self.repetition = repetition


def _name(n_factors, value_len=24, rep=1):
    vals = {f"factor_number_{i:02d}": "v" * value_len for i in range(n_factors)}
    return tr.bag_name(_Trial(vals, rep), {}, "20260819T112442")


def test_short_id_is_untouched():
    """The common case must keep its fully readable name."""
    name = _name(3, value_len=10)
    assert name.startswith("20260819T112442_")
    assert name.endswith("_rep01")
    assert "_h" not in name.rsplit("_rep", 1)[-1]


@pytest.mark.parametrize("n", [1, 4, 8, 20, 60])
def test_never_exceeds_the_cap(n):
    """However many factors vary, the id fits - with room for _0.mcap and _slim."""
    name = _name(n)
    assert len(name.encode()) <= tr.MAX_TRIAL_ID_BYTES
    assert len(f"{name}_0.mcap".encode()) <= 255
    assert len(f"{name}_slim".encode()) <= 255


def test_truncated_ids_stay_unique():
    """Two trials differing ONLY in a truncated-away factor must not collide."""
    base = {f"factor_number_{i:02d}": "v" * 24 for i in range(20)}
    a, b = dict(base), dict(base)
    a["factor_number_19"] = "value_alpha_" + "v" * 12
    b["factor_number_19"] = "value_beta__" + "v" * 12
    na = tr.bag_name(_Trial(a), {}, "20260819T112442")
    nb = tr.bag_name(_Trial(b), {}, "20260819T112442")
    assert na != nb, "truncation collapsed two distinct trials onto one directory"


def test_truncated_ids_keep_stamp_and_repetition():
    """The two things a human sorts and pairs by must survive truncation."""
    name = _name(20, rep=7)
    assert name.startswith("20260819T112442_")
    assert "_rep07_h" in name


def test_the_real_2026_08_19_id_now_fits():
    """The exact factor set that failed at the rig."""
    values = {
        "arm_aim": "origin", "command_mode": "live",
        "depth_aggregation": "nearest_cluster", "detector_model": "depthseed_v1",
        "drone_state": "flying", "max_pose_age_s": "2.5", "observer": "mpc",
        "observer_tuning": "bodylag_20260819", "pose_source": "markerless",
        "record_profile": "spot_observer_handcam",
    }
    name = tr.bag_name(_Trial(values), {}, "20260819T112442")
    assert len(name.encode()) <= tr.MAX_TRIAL_ID_BYTES
    assert len(f"{name}_0.mcap".encode()) <= 255


def test_a_long_cjk_level_is_cut_on_bytes_without_splitting_a_character():
    """2026-11-25: the cut counted characters, so a CJK level kept 200 characters, 524 bytes,
    and the recorder failed with "File name too long". The cut now counts UTF-8 bytes and
    never ends inside a character."""
    name = tr.bag_name(_Trial({"site": "東京駅前の実験室" * 12}), {}, "20261125T195710")
    assert len(name.encode("utf-8")) <= tr.MAX_TRIAL_ID_BYTES
    assert name.encode("utf-8").decode("utf-8") == name        # whole characters only
    assert name.startswith("20261125T195710_site-東京駅前の実験室") and "_rep01_h" in name


def _old_cut(full_values, stamp, rep=1):
    """The character cut of before 2026-11-25, which ASCII names must keep exactly."""
    import hashlib
    parts = [stamp] + [f"{k}-{v}" for k, v in sorted(full_values.items())] + [f"rep{rep:02d}"]
    full = "_".join(str(p).replace("/", "-").replace(" ", "") for p in parts)
    if len(full.encode("utf-8")) <= tr.MAX_TRIAL_ID_BYTES:
        return full
    digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:8]
    head, tail = f"{stamp}_", f"_rep{rep:02d}_h{digest}"
    budget = tr.MAX_TRIAL_ID_BYTES - len(head.encode()) - len(tail.encode())
    return f"{head}{full[len(head):full.rindex('_rep')][:max(0, budget)].rstrip('_-')}{tail}"


@pytest.mark.parametrize("n", [3, 8, 20, 60])
def test_an_ascii_name_is_cut_exactly_as_before(n):
    values = {f"factor_number_{i:02d}": "v" * 24 for i in range(n)}
    assert tr.bag_name(_Trial(values), {}, "20260819T112442") == _old_cut(values, "20260819T112442")
