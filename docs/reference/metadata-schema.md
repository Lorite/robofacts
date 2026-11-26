# trial_metadata_schema.yaml

The metadata contract says which fields every `trial_metadata.json` must hold, and which of them are coverage axes. This page lists the keys of the two contract files and the fields of the core contract that `trials init` writes.

For the idea behind the contract, see [[metadata-contract]].

## Where the files are

- `experiments/common/config/trial_metadata_core.yaml` is the core contract of the project. It must exist: its presence also marks the directory as the experiments root.
- `experiments/<name>/config/trial_metadata_schema.yaml` adds the fields of one experiment. It is optional.

The loader merges the experiment's fields on top of the core fields. A field with the same name replaces the core field, so an experiment can narrow an `enum` or make a field required.

## A minimal example

```yaml
version: 1
fields:
  rate:
    required: true
    type: str
    factor: true
    enum: [slow, fast]
    describe: "The level of the rate factor this trial ran at."
```

## Keys of the file

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `fields` | mapping | yes | none | One entry per metadata field, by name. |
| `version` | integer | no | none | The templates write `version: 1`. The loader does not read it. |

## Keys of a field

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `required` | boolean | no | `false` | The field must be present and not `null`. |
| `type` | string | no | none | One of `str`, `int`, `float`, `bool`, `list` and `dict`. A value of another type breaks the contract. `float` also accepts an integer, and `int` rejects a boolean. |
| `enum` | list | no | none | The allowed values. |
| `factor` | boolean | no | `false` | The field is a coverage axis: the [[coverage|coverage index]] groups trials by it. A field without it is provenance or outcome. |
| `describe` | string | no | empty | Why the field matters. A validation error shows it. |

The contract checks only the fields that it declares. A trial may hold more fields, and the contract ignores them.

## How a trial is checked

The runner validates the metadata of every trial when it writes `trial_metadata.json`. A problem never deletes the trial: the runner prints a warning and writes the problems into the metadata under `_schema_problems`. A trial that breaks the contract counts as unverified, not usable, so the [[campaigns|campaign]] still asks for its cell.

Besides the declared fields, one rule always holds: a trial with `aborted: true` needs a non-empty `abort_reason`.

`trials metadata` audits every recorded trial against the contract. `trials metadata --show-schema` prints the merged contract, and `--strict` makes the command exit with an error when a trial breaks it.

## The contract and the factors

Every factor that a campaign sweeps needs a `metadata_key` in [[factors-yaml|factors.yaml]], and the contract must declare that field with `factor: true`. Otherwise the planner and the runner refuse the campaign, because its coverage could never be measured. [[lint-rules|Lint rule]] `L9` also requires every record profile to be a value of the `record_profile` field's `enum`.

## The fields of the core contract

`trials init` writes a core contract with these fields:

| Field | Required | Type | Factor | Meaning |
| --- | --- | --- | --- | --- |
| `trial_id` | yes | `str` | no | The identifier that ties the recording, the metadata and the results together. |
| `execution_host` | yes | `str` | yes | The machine that ran the trial. |
| `execution_container` | no | `str` | no | The container or image that the trial ran in. |
| `code_version` | no | `dict` | no | The code that produced the trial: commit, branch, whether the checkout had changes, and the state of the build workspaces. |
| `experiment_variant` | no | `str` | yes | A named variant within the experiment. |
| `record_profile` | no | `str` | yes | The record profile of the trial. Its `enum` is `custom` and `unknown`. Add every profile of the project here. |
| `aborted` | yes | `bool` | no | Whether the trial failed to complete. It is always explicit. |
| `abort_reason` | no | `str` | no | Why the trial aborted. |
| `preflight` | no | `dict` | no | The result of the gate, with any override and its reason. |
| `domain` | no | `str` | no | `simulation` or `hardware`. |
| `domain_basis` | no | `str` | no | Why the trial has its domain: the `simulation` key of `runner.yaml`, or the factor level that decided it. |

!!! note
    A field that you make required applies to every trial ever recorded, also to trials recorded before the field existed. Those trials then count as unverified. Add a new field as `required: false` when older trials cannot have it.
