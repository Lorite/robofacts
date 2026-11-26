# Analyses

A named analysis is a declared computation over a cohort of usable trials. You declare it once, run it with one command, and every run records the exact list of recordings it read.

## Why declare an analysis

A results folder that holds only a number cannot say which trials it came from. Months later, nobody can rebuild the list from memory. A named analysis fixes both sides:

- it **selects** the recordings from the [[coverage|coverage index]], by factor levels, and only the usable trials by default, so an aborted or unverified recording never enters a mean by accident,
- it **records** the analysis, the selection, the exact list of recordings and the commands in `analysis_metadata.json`, next to the results.

## Declare it in analyses.yaml

Each experiment declares its analyses in `config/analyses.yaml`:

```yaml
analyses:
  line_count:
    describe: "How many lines each recording holds, as a JSON file per recording."
    per: bag
    command: >-
      python3 experiments/demo_log_recorder/scripts/verify_log.py {recording}
      --min-lines 0 --json {results}/line_count.json
```

| Key | Meaning |
| --- | --- |
| `command` | the command to run, with placeholders. Required |
| `per` | `bag`: the command runs once per recording (the default). `cohort`: it runs once for all of them |
| `describe` | what the analysis computes. `trials analyze <experiment>` shows it |

The placeholders depend on `per`:

| Placeholder | `per: bag` | `per: cohort` |
| --- | --- | --- |
| `{recording}` (or `{bag}`) | the recording directory of one trial | not available |
| `{bags}` | not available | every selected recording directory |
| `{results}` | `results/<stamp>_<analysis>/<trial>/` | `results/<stamp>_<analysis>/` |
| `{experiment}` | the experiment's name | the experiment's name |

When `{bags}` is a whole argument, it becomes one argument per recording. Inside a larger argument, the recordings are joined with spaces. The runner creates the results folder of the run, and a per-recording command creates its own subfolder when it writes there. An experiment without analyses may declare `analyses: {}`. [[analyses-yaml|analyses.yaml]] is the full reference.

## Run it with trials analyze

```bash
trials analyze demo_log_recorder                                  # the declared analyses
trials analyze demo_log_recorder line_count                       # dry run: the cohort and the commands
trials analyze demo_log_recorder line_count --where rate=fast     # only one cell
trials analyze demo_log_recorder line_count --execute             # run it
```

Without `--execute`, the command prints the cohort and the commands, and runs nothing (the paths are shortened here):

```text
analysis: line_count   bags: 1   -> experiments/demo_log_recorder/results/20261126T114611_line_count
    20260101T100000_rep01

=== DRY RUN — nothing executed ===
  $ python3 experiments/demo_log_recorder/scripts/verify_log.py <recording> --min-lines 0 --json <results>/line_count.json
```

The cohort options:

- `--where factor=value`: restrict the cohort to trials with that recorded value. Repeat it for several factors. It uses the names of the metadata keys.
- `--include-unverified`: also feed in unverified trials.
- `--include-excluded`: also feed in excluded trials.

When the selection is empty, the command stops with an error and suggests `--include-unverified`. With `--execute`, it exits with 1 when a command failed.

## What a run leaves behind

Every executed run writes a new folder, `results/<stamp>_<analysis>/`, with the outputs of the commands and `analysis_metadata.json`:

- the name and the description of the analysis,
- the experiment and the time of the run,
- the selection: the `--where` filter and the number of recordings,
- the exact list of recordings,
- the commands, and how many of them failed.

This file is the analysis-side counterpart of `trial_metadata.json`. `trials records` and the web interface join it back to each trial, so every trial lists the analyses that used it, and every result names the trials it came from.

## The daily use

A typical loop runs the campaign until the plan reports nothing missing, then runs an analysis over the cohort:

```bash
trials plan experiments/my_experiment/campaigns/baseline.yaml
trials analyze my_experiment my_analysis --where rate=fast --execute
```

See [[daily-loop|The daily loop]] for the whole loop.
