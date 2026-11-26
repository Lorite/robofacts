# Campaign files

A campaign file says which trials an experiment needs: which [[factors]] to sweep, which to fix, and how many repetitions each cell gets. This page lists every key of the file. For the idea, see [[campaigns]].

## Where the files are

Campaigns live in `experiments/<name>/campaigns/<campaign>.yaml`, one file per campaign. `trials check <file>` validates one file, `trials plan <file>` lists the cells that still need trials, and `trials run <file>` runs them.

## A minimal example

```yaml
name: baseline
experiment: my_experiment
describe: >-
  Sweep the rate factor with two repetitions per level.
repetitions: 2
sweep:
  rate: [slow, fast]
fixed:
  execution_host: laptop
```

## Keys

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `experiment` | string | yes | none | The experiment that the campaign belongs to. Its factors are the ones the campaign may name. |
| `name` | string | no | the file name without `.yaml` | The name of the campaign. |
| `describe` | string | no | empty | What the trials will show, and what is critical for safety. The operator reads it before the first trial. |
| `sweep` | mapping | no | empty | A factor to the list of levels to sweep. A single value counts as a list of one. Each level may appear only once. |
| `fixed` | mapping | no | empty | A factor to the one level that every trial uses. A factor may not be both swept and fixed. |
| `repetitions` | integer | no | `1` | Trials per cell, at least 1. |
| `repetitions_where` | list | no | empty | Other repetition counts for some cells, each `{where: {factor: level}, repetitions: N}`. The first entry whose `where` matches a cell sets its count. |
| `order` | mapping | no | the planner's order | A randomized order: `shuffle` (boolean), `seed` (an integer, required with `shuffle: true`) and `blocks` (`true`, `false` or a positive integer). It changes when a trial runs, never what is wanted. |
| `preflight_checklist` | path | no | `config/preflight_checklist.json` | The [[checklist-json|gate checklist]] of this campaign, relative to the experiment directory. |
| `factor_checks` | string | no | `binding` | `binding` adds the [[factors-yaml|factor checks]] to the gate. `calibration` measures them instead, and such a campaign runs only with `trials run --scratch`. |
| `operator_steps` | list | no | empty | Steps that the operator does before the trial. See below. |
| `operator_live_steps` | list | no | empty | Steps that the operator does while the trial runs. They take no `when`. |
| `setup_steps` | list | no | empty | Commands that run after the setup steps of `runner.yaml` and the robots' resets, and before the stack starts. Each is `{name, command, required, when}`, as in [[runner-yaml|runner.yaml]]. |
| `verify_steps` | list | no | empty | More checks of the recording, each `{name, command}`. The runner runs them after the verify steps of `runner.yaml`. |
| `robot_reset` | boolean | no | `true` | `false` skips the `reset` steps of the robots for this campaign. |
| `voice_cues` | list | no | empty | [[spoken-cues|Spoken cues]] during the action, each `{at_s, text}`. |
| `voice_announce` | `false` or mapping | no | the runner's defaults | The runner's own spoken lines. See below. |

Every name under `sweep`, `fixed` and `repetitions_where` must be a factor of the experiment, and every level must be a declared level. A level that the factor marks as `blocked` refuses the whole campaign.

## Operator steps

An operator step is a string, or a mapping:

| Field | Default | Meaning |
| --- | --- | --- |
| `text` | empty | The instruction. |
| `run` | empty | A command that the web interface runs with one click, on the host of the campaign's fixed `execution_host`. Only the fixed factors fill its placeholders. |
| `when` | `always` | `first`, `repeat` or `always`, as for setup steps. |

```yaml
operator_steps:
  - "Clear the test area."
  - {text: "Power the robot on.", when: first}
operator_live_steps:
  - "Dim the lights when the robot reaches the first waypoint."
```

## Placeholders

The commands of `setup_steps` and `verify_steps` take the same placeholders as in `runner.yaml`: `{factor}` and `{arg_<argument>}`. Only `verify_steps` also take `{recording}` or `{bag}`, because a setup step runs before the trial has a recording. [[lint-rules|Lint rule]] `L4b` checks the setup steps.

## Spoken cues

`voice_cues` holds `{at_s, text}` entries. `at_s` is a number of seconds, at least 0, after the start of the recording. `text` is a sentence of at most 200 characters, with `{factor}` placeholders. Lint rule `L15` refuses a cue at or after `action_timeout_s` of `runner.yaml`.

`voice_announce` is absent (the runner's defaults, spoken for hardware experiments only), `false` (no lines at all), or a mapping of moments to a text. The moments are `before_launch`, `record_start`, `record_end`, `gate_blocked` and `gate_passed`. `null` or an empty text silences one moment, and an absent moment keeps its default.

## Overrides on the command line

`trials plan`, `trials run` and `trials check` take `--set name=value`. One value fixes the factor, and several values separated by commas sweep it, for example `--set rate=slow,fast`. An override replaces what the file says about that factor.
