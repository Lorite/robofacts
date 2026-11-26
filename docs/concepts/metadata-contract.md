# The metadata contract

The metadata contract lists the fields every trial must record. A coverage index is only as trustworthy as the metadata under it, so a trial that breaks the contract never counts.

## Where it is declared

The contract has two parts:

- the core, `experiments/common/config/trial_metadata_core.yaml`, which every trial of the project must satisfy,
- the experiment's own fields, `experiments/<name>/config/trial_metadata_schema.yaml`, merged on top.

A field of the experiment replaces the core field of the same name. So an experiment can tighten a core field, for example make it required or narrow its `enum`, without a copy of the core file. `trials metadata --experiment <name> --show-schema` prints the merged contract.

## A field

```yaml
fields:
  rate:
    required: true
    type: str
    factor: true
    enum: [slow, fast]
    describe: "The level of the rate factor this trial ran at."
```

| Key | Meaning |
| --- | --- |
| `required` | the field must be present and not null |
| `type` | `str`, `int`, `float`, `bool`, `list` or `dict`, checked when given |
| `enum` | the allowed values, checked when given |
| `factor` | the field is a coverage axis: the coverage index may group the trials by it |
| `describe` | why the field matters. Validation errors show this text |

A field without `factor: true` is provenance or outcome, not an axis. The runner records `code_version` with every trial, for example, but it is never a factor: as an axis, every commit would open a new level, and every cell of every campaign would ask for its trials again.

The validator ignores keys that start with an underscore, and the index never offers them as a coverage axis. [[metadata-schema|trial_metadata_schema.yaml]] is the full reference.

## The core fields

The core contract that `trials init` writes declares these fields:

| Field | Required | Factor | Meaning |
| --- | --- | --- | --- |
| `trial_id` | yes | no | the identifier of the trial, the name of its recording folder |
| `execution_host` | yes | yes | the computer that ran the trial |
| `aborted` | yes | no | whether the trial did not complete. Never left empty |
| `abort_reason` | no | no | why it aborted |
| `record_profile` | no | yes | which set of data was recorded |
| `experiment_variant` | no | yes | a named variant within the experiment |
| `execution_container` | no | no | the container or image the trial ran in |
| `code_version` | no | no | which code produced the trial, probed on the host that ran it |
| `preflight` | no | no | the gate's result, with any override and its reason |
| `domain` | no | no | `simulation` or `hardware` |
| `domain_basis` | no | no | why the trial has its domain |

The project owns this file, so you can add fields that every experiment of the project must record.

## When the contract is checked

The runner writes `trial_metadata.json` once, after the verify steps, and checks it against the contract at that moment. A problem never destroys a finished trial: the runner prints a warning, writes the problems into the metadata under `_schema_problems`, and `trials run` reports the trial as recorded but unverified.

The [[coverage|coverage index]] checks every trial again when it reads it, against the contract as it is now. So a change of the contract also applies to the trials recorded before it. When you make a field required, every older trial without it becomes unverified, and the planner asks for those cells again. `trials metadata` audits every recorded trial against the current contract.

```bash
trials metadata --experiment my_experiment            # every trial against the contract
trials metadata --experiment my_experiment --strict   # exit non-zero when a trial breaks it
```

## The invariant

A factor that a [[campaigns|campaign]] sweeps must declare a `metadata_key`, and the contract must declare that key with `factor: true`. This closes the loop from the declared [[factors|factors]] to the recorded metadata to the coverage axes.

Every command that plans or runs a campaign enforces it: `trials check`, `trials plan` and `trials run` exit with code 2 and name the factor, and the web interface refuses before any trial starts. Lint rule L10 fails the build on the same gap. The refusal names one of three causes:

- the swept factor declares no `metadata_key`,
- its key is not a field of the contract,
- its key is a field, but not a coverage axis (`factor: true` is missing).

The reason is practical. A factor that is never recorded produces trials whose coverage can never be measured. Without the key, the planner cannot tell the levels apart, so every trial would run at the first level while the campaign claimed a sweep.

## Keep the record profiles in the enum

`record_profile` has an `enum`. When you add a record profile, add it to the `enum` in the same change. A trial recorded on a profile missing from the list breaks the contract, counts as unverified, and its cell asks for a trial that already happened. Lint rule L9 fails the build on this gap (see [[record-profiles|Record profiles]] and [[lint-rules|Lint rules]]).

## Never edit a trial's metadata

The runner writes `trial_metadata.json` from the plan and the checks of that moment, and nobody edits it afterwards. When a person later knows better, the decision goes into a separate file beside the recording, with a reason. See [[decisions|A person's decision about a trial]].
