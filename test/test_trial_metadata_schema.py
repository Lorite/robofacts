"""Tests for the trial-metadata contract (#96 Phase 0)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

from declarative_robot_trials import trial_metadata_schema as tms
EXPERIMENTS_ROOT = HERE.parents[1]

GOOD = {"trial_id": "t1", "execution_host": "orin", "aborted": False}


def write_schema(root: Path, rel: str, fields: dict) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["version: 1", "fields:"]
    for name, spec in fields.items():
        lines.append(f"  {name}:")
        for k, v in spec.items():
            lines.append(f"    {k}: {json.dumps(v)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_schema(tmp_path, "common/config/trial_metadata_core.yaml", {
        "trial_id": {"required": True, "type": "str"},
        "execution_host": {"required": True, "type": "str", "factor": True},
        "aborted": {"required": True, "type": "bool"},
        "abort_reason": {"required": False, "type": "str"},
        "record_profile": {"required": False, "type": "str", "factor": True,
                           "enum": ["light", "full"]},
    })
    return tmp_path


# --------------------------------------------------------------------------- #
# The real shipped schema
# --------------------------------------------------------------------------- #
def test_shipped_core_schema_loads_and_is_sane():
    schema = tms.load_schema()
    assert "trial_id" in schema.required
    assert "execution_host" in schema.required
    assert "aborted" in schema.required, "an unlabelled abort makes a bag ambiguous forever"
    assert "execution_host" in schema.factors


def test_code_version_is_optional_provenance_not_a_factor():
    """Required would retroactively fail every pre-2026-08-04 trial, which the
    index counts `unverified`, so campaign_plan would ask for flights that
    already happened. Factor would open a new coverage level on every commit."""
    schema = tms.load_schema()
    assert "code_version" in schema.fields
    assert "code_version" not in schema.required
    assert "code_version" not in schema.factors


def test_a_code_version_stamp_satisfies_the_contract():
    problems = tms.validate(dict(GOOD, code_version={"commit": "abc", "dirty": False}))
    assert problems == []
    assert any("code_version" in p for p in tms.validate(dict(GOOD, code_version="abc123"))), \
        "a bare string loses the dirty flag, which is the part that matters"


def test_every_experiment_schema_override_loads():
    """A malformed per-experiment schema must fail here, not at record time."""
    for path in EXPERIMENTS_ROOT.glob(f"*/config/{tms.PER_EXPERIMENT_NAME}"):
        schema = tms.load_schema(path.parents[1].name)
        assert schema.fields, f"{path} produced an empty schema"


# --------------------------------------------------------------------------- #
# Required / type / enum
# --------------------------------------------------------------------------- #
def test_a_complete_trial_passes(root):
    assert tms.validate(GOOD, experiments_root=root) == []


@pytest.mark.parametrize("missing", ["trial_id", "execution_host", "aborted"])
def test_missing_required_field_is_reported(root, missing):
    meta = {k: v for k, v in GOOD.items() if k != missing}
    problems = tms.validate(meta, experiments_root=root)
    assert any(p.startswith(missing) for p in problems)


@pytest.mark.parametrize("nulled", ["trial_id", "execution_host", "aborted"])
def test_explicit_null_is_as_bad_as_missing(root, nulled):
    """This is the actual failure mode in the existing bags: the key is there
    and its value is null, which silently reads as 'no data' downstream."""
    meta = dict(GOOD, **{nulled: None})
    problems = tms.validate(meta, experiments_root=root)
    assert any(p.startswith(nulled) and "null" in p for p in problems)


def test_wrong_type_is_reported(root):
    problems = tms.validate(dict(GOOD, execution_host=42), experiments_root=root)
    assert any("execution_host" in p and "expected str" in p for p in problems)


def test_bool_does_not_satisfy_an_int_field(tmp_path):
    write_schema(tmp_path, "common/config/trial_metadata_core.yaml",
                 {"n": {"required": True, "type": "int"}})
    assert tms.validate({"n": 3}, experiments_root=tmp_path) == []
    problems = tms.validate({"n": True}, experiments_root=tmp_path)
    assert any("expected int" in p for p in problems), "bool is an int subclass in Python"


def test_enum_is_enforced(root):
    assert tms.validate(dict(GOOD, record_profile="light"), experiments_root=root) == []
    problems = tms.validate(dict(GOOD, record_profile="enormous"), experiments_root=root)
    assert any("record_profile" in p for p in problems)


def test_aborted_without_a_reason_is_a_dead_end(root):
    problems = tms.validate(dict(GOOD, aborted=True), experiments_root=root)
    assert any("abort_reason" in p for p in problems)
    assert tms.validate(dict(GOOD, aborted=True, abort_reason="watchdog tripped"),
                        experiments_root=root) == []


# --------------------------------------------------------------------------- #
# Per-experiment overrides (the common/ + <name>/ split)
# --------------------------------------------------------------------------- #
def test_experiment_fields_are_merged_on_top(root):
    write_schema(root, "expA/config/trial_metadata_schema.yaml",
                 {"cf_name": {"required": True, "type": "str", "factor": True}})
    assert tms.validate(GOOD, experiment="expA", experiments_root=root)
    ok = dict(GOOD, cf_name="crazylori_brushless_1")
    assert tms.validate(ok, experiment="expA", experiments_root=root) == []
    assert "cf_name" in tms.load_schema("expA", root).factors


def test_an_experiment_can_tighten_a_core_field(root):
    """Overriding by name lets an experiment require something the core leaves
    optional, without forking the core file."""
    write_schema(root, "expB/config/trial_metadata_schema.yaml",
                 {"record_profile": {"required": True, "type": "str"}})
    assert any("record_profile" in p
               for p in tms.validate(GOOD, experiment="expB", experiments_root=root))
    assert tms.validate(GOOD, experiments_root=root) == [], "core stays unaffected"


def test_unknown_experiment_falls_back_to_core(root):
    assert tms.validate(GOOD, experiment="nope", experiments_root=root) == []


# --------------------------------------------------------------------------- #
# Malformed schemas fail loudly
# --------------------------------------------------------------------------- #
def test_unknown_type_in_a_schema_raises(tmp_path):
    write_schema(tmp_path, "common/config/trial_metadata_core.yaml",
                 {"x": {"required": True, "type": "quaternion"}})
    with pytest.raises(tms.SchemaError, match="unknown type"):
        tms.load_schema(experiments_root=tmp_path)


def test_missing_core_schema_raises(tmp_path):
    with pytest.raises(tms.SchemaError, match="core schema not found"):
        tms.load_schema(experiments_root=tmp_path)


# --------------------------------------------------------------------------- #
# Record-time annotation must never destroy a finished trial
# --------------------------------------------------------------------------- #
def test_annotate_records_problems_into_the_metadata(root, capsys):
    meta = {"trial_id": "t1"}
    problems = tms.annotate(meta, experiments_root=root)
    assert problems and meta["_schema_problems"] == problems
    assert "schema problem" in capsys.readouterr().out


def test_annotate_clears_the_flag_when_fixed(root):
    meta = dict(GOOD, _schema_problems=["stale"])
    assert tms.annotate(meta, experiments_root=root) == []
    assert "_schema_problems" not in meta


def test_annotated_metadata_stays_json_serialisable(root):
    meta = {"trial_id": "t1"}
    tms.annotate(meta, experiments_root=root)
    json.loads(json.dumps(meta))


# --------------------------------------------------------------------------- #
# Audit over recorded bags
# --------------------------------------------------------------------------- #
def test_audit_reports_per_bag(root):
    bag = root / "expA" / "bags" / "t1"
    bag.mkdir(parents=True)
    (bag / "trial_metadata.json").write_text(json.dumps(GOOD), encoding="utf-8")
    bad = root / "expA" / "bags" / "t2"
    bad.mkdir(parents=True)
    (bad / "trial_metadata.json").write_text(json.dumps({"trial_id": "t2"}), encoding="utf-8")

    results = tms.audit(root)
    assert results[str(bag)] == []
    assert results[str(bad)], "the incomplete trial should be flagged"


def test_audit_ignores_slim_copies(root):
    """Same `bags*` glob bug as the index: a slim bag's metadata is a copy, not
    a second trial, and auditing it would double every reported problem."""
    bag = root / "expA" / "bags" / "t1"
    bag.mkdir(parents=True)
    (bag / "trial_metadata.json").write_text(json.dumps(GOOD), encoding="utf-8")
    slim = root / "expA" / "bags_slim" / "t1_slim"
    slim.mkdir(parents=True)
    (slim / "trial_metadata.json").write_text(json.dumps(GOOD), encoding="utf-8")
    assert len(tms.audit(root)) == 1


def test_audit_handles_unreadable_metadata(root):
    bag = root / "expA" / "bags" / "broken"
    bag.mkdir(parents=True)
    (bag / "trial_metadata.json").write_text("{not json", encoding="utf-8")
    assert any("unreadable" in p for p in tms.audit(root)[str(bag)])
