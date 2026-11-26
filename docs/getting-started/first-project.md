# Your first project

This page creates a project of your own, runs its first trials, and shows which files you edit to describe your own system.

## Create the project

```bash
trials init my_project --experiment my_experiment
cd my_project
```

`trials init` writes a [[projects|project]]: the file `trials.yaml`, the shared configuration under `experiments/common/config/`, and with `--experiment` a first [[experiments|experiment]] under `experiments/my_experiment/`. The project runs on the built-in `process` backend unless you pass `--backend` (for example `--backend ros2`, see [[backends|Backends]]).

## Five steps from nothing to trials

```bash
trials lint                                                                    # every declaration against the rules
trials plan experiments/my_experiment/campaigns/baseline.yaml                  # what the campaign still needs
trials run  experiments/my_experiment/campaigns/baseline.yaml --limit 1        # dry run: the exact commands
trials run  experiments/my_experiment/campaigns/baseline.yaml --execute --no-pause   # the trials
trials index --experiment my_experiment --summary                              # the coverage index
```

The generated experiment works as it is. On the `process` backend, a small script (`scripts/system.py`) stands in for the system and writes a log file, the recorder stores what it writes during a trial, the action (`scripts/run_trial.py`) is the trial itself, and a verify step (`scripts/verify_log.py`) reads the recording. Its campaign `baseline` sweeps one factor, `rate`, over two levels with two repetitions, so the planner wants four trials:

```text
Campaign: baseline  (my_experiment)
  wanted 4 · covered 0 · STILL MISSING 4

  (* marks a non-default value)

  STILL MISSING (2 cell(s)):
    x2  execution_host=laptop rate=fast* record_profile=custom   [none yet, want 2]
    x2  execution_host=laptop rate=slow record_profile=custom   [none yet, want 2]
```

`--no-pause` skips the pause after the gate of each trial. Without it, every trial waits so that you can pose the setup: for Enter in a simulation, and for the word `fly` on hardware.

!!! warning "Hardware trials"
    A hardware run needs a terminal for the pose pause, so `trials run` refuses it without one unless you pass `--no-pause`. A hardware trial also runs only after a [[gate|T-0 gate]], so its experiment needs a gate checklist. See [[runner|The runner]] and [[hardware-runs|Run on hardware]].

## Describe your own system

Edit the five files of the experiment in this order. The linter and the planner then check the declarations before any trial runs.

1. `config/factors.yaml`: the [[factors|Factors]] (the variables the experiment controls) and how the runner realizes each level.
2. `config/trial_metadata_schema.yaml`: the [[metadata-contract|metadata contract]], the fields every trial records, with the coverage axes.
3. `config/preflight_checklist.json`: the [[gate|T-0 gate]], the checks seconds before every trial.
4. `config/runner.yaml`: the stack, the action of one trial, the verify steps and the computers it may run on.
5. `campaigns/baseline.yaml`: the [[campaigns|campaign]], with the factors to sweep, the fixed levels and the repetitions.

Then replace the three scripts with your own, and write the design of the experiment into its `README.md`. [[experiments|Experiments]] explains each file, and [[new-experiment|Create an experiment]] is the full procedure.

## Start from a similar experiment

When a similar experiment exists, copy it instead of the template:

```bash
trials new my_variant --from my_experiment --campaign baseline:my_claim   # copy one campaign, renamed
```

`trials new <name> --from <existing>` copies the declarations of that experiment (`config/`, the campaigns, `scripts/`, the runbook and the other text files) with its name replaced. It does not copy the recordings, the run logs, the results, the notebooks, the tests and large files. The new `README.md` is a short stub that names the source. Without `--campaign`, it copies every campaign with its name unchanged. `trials new <name>` without `--from` writes a new experiment from the commented template.

## Ask whether it is ready

```bash
trials validate experiments/my_experiment
```

`trials validate` takes an experiment or one campaign file. It runs every stage that a first trial depends on and prints one line per stage, with its time:

```text
trials validate my_experiment  (campaigns: baseline)

  ok    check      0.0 s  baseline.yaml: loads, 4 trial(s)
  ok    plan       0.0 s  baseline.yaml: wanted 4 · covered 0 · STILL MISSING 4
  ok    gate       0.0 s  config/preflight_checklist.json: 3 items (2 machine-checked, 1 manual) - coherent.
  ok    lint       0.4 s  my_experiment: 13 passed, 1 skipped in 0.13s
  skip  tests             no test_*.py in the experiment, so 3 script(s) run untested: run_trial.py, system.py, verify_log.py
  ok    dry_run    0.0 s  baseline.yaml @ laptop: 1 trial(s) planned, every command resolved

READY: my_experiment passed every stage in 0.4 s. The next step is a real trial, which needs its own approval: trials run experiments/my_experiment/campaigns/baseline.yaml --limit 1 --execute
```

It shows the output of a failed stage and exits 0 only when every stage passed. Every stage is scoped to that experiment, so it runs in seconds. The full test suite of a project is for CI.

## Next steps

- [[daily-loop|The daily loop]]: the commands you use every day.
- [[new-campaign|Write a campaign]]: a second campaign for the same experiment.
- [[words|The words]]: the terms these pages use.
