# Experiments

An experiment is a physical setup with its software: the system under test, the stack that runs it, and the procedure of one trial. You declare it once in five files, and the framework plans, runs and counts its trials from them.

## The folder of an experiment

Every experiment is one folder under the experiments root of its [[projects|project]]:

```text
experiments/my_experiment/
  README.md                        the design: question, hypotheses, protocol
  config/
    factors.yaml                   the factors and their levels
    trial_metadata_schema.yaml     the metadata contract of this experiment
    preflight_checklist.json       the T-0 gate
    runner.yaml                    how to start the stack and run one trial
    analyses.yaml                  the named analyses
  campaigns/
    baseline.yaml                  which factors to sweep, to fix and to repeat
  scripts/                         the action, the verify steps, helpers
  recordings/                      the recorded trials
  runs/                            run logs, the attempt ledger, the gate log
  results/                         the outputs of the named analyses
```

`trials new <name>` creates this folder from a commented template, and `trials new <name> --from <existing>` copies a similar experiment (see [[new-experiment|Create an experiment]]).

## The five files

| File | Says | Reference |
| --- | --- | --- |
| `config/factors.yaml` | the factors, their levels and defaults, the metadata key each one is recorded under, and how the runner realizes a level | [[factors-yaml|factors.yaml]] |
| `config/trial_metadata_schema.yaml` | the fields every trial must record, and which of them are coverage axes (`factor: true`) | [[metadata-schema|trial_metadata_schema.yaml]] |
| `config/preflight_checklist.json` | the default T-0 gate: machine checks and operator confirmations | [[checklist-json|preflight_checklist.json]] |
| `config/runner.yaml` | how to start the stack, how to run and verify one trial, what to record, and on which computers | [[runner-yaml|runner.yaml]] |
| `config/analyses.yaml` | the named, repeatable analyses over a cohort of usable trials | [[analyses-yaml|analyses.yaml]] |

Two of the files extend a shared core in `experiments/common/config/`. The framework merges the experiment's factors over `factors.yaml`, so every experiment has the core factors `execution_host` and `record_profile`. It merges the experiment's contract over `trial_metadata_core.yaml`, so every trial records the core fields such as `trial_id` and `aborted`. An entry of the experiment replaces the core entry of the same name.

The concept pages explain what each file means: [[factors|Factors]], [[metadata-contract|The metadata contract]], [[gate|The T-0 gate]], [[runner|The runner]] and [[analyses|Analyses]].

## Campaigns

The campaigns of an experiment live in `campaigns/*.yaml`. A [[campaigns|campaign]] says which factors to sweep, which to fix and how many repetitions each cell gets. Because a campaign is "which set of trials, under which conditions", it may also own conditions of its own:

- `operator_steps` and `operator_live_steps`: what a person does before and during each trial,
- `setup_steps`: commands before the stack starts, after those of `runner.yaml`,
- `verify_steps`: checks of the recording, added to those of `runner.yaml`,
- `preflight_checklist`: its own T-0 gate instead of the experiment's.

Reference: [[campaign-yaml|Campaign files]].

## The rule that ties the files together

A factor that a campaign sweeps must be recorded under a metadata key that the contract declares with `factor: true`. The runner writes each level under that key, and the planner reads it back to count the trial in its cell. When the rule is broken, `trials plan`, `trials check` and `trials run` refuse the campaign, and the linter fails. See [[metadata-contract#The invariant|the invariant]].

## The folders of recorded data

The [[backends|backend]] names the folders where an experiment keeps its trials:

| Role | `process` backend | ROS 2 backend |
| --- | --- | --- |
| the trials that can count | `recordings/` | `bags/` |
| the trials a person excluded | `recordings_issues/` | `bags_issues/` |
| scratch recordings, never counted | `recordings_scratch/` | `bags_scratch/` |
| reduced copies | `recordings_slim/` and the other `*_slim/` folders | `bags_slim/` and the other `*_slim/` folders |

Only a trial in the first folder (or its reduced copy) can count. The coverage index lists the trials of the issues folder as excluded, and it never reads the scratch folder. Move a trial into the issues folder only with `trials exclude` (see [[decisions|A person's decision about a trial]]).

The folder `runs/` holds what each attempt leaves behind, also when it was blocked: a run log per trial (`runs/<trial>.log`), the attempt ledger (`runs/attempts.jsonl`, one line per executed attempt) and the gate log (`runs/preflight_gate.jsonl`, one line per gate verdict). The coverage index never reads them, so they cannot change what counts.

## Check an experiment

```bash
trials check experiments/my_experiment/campaigns/baseline.yaml   # does the campaign load, and what does it expand to
trials check --factors my_experiment                             # the factor space of the experiment
trials lint --experiment my_experiment                           # every declaration against the lint rules
trials validate experiments/my_experiment                        # every stage of a first trial: READY or NOT READY
```

See [[lint-rules|Lint rules]] and [[commands|Commands]].
