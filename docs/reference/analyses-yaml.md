# analyses.yaml

`analyses.yaml` declares the named [[analyses]] of an experiment: commands that run over a cohort of usable trials and record which trials each result came from. This page lists every key of the file.

## Where the file is

Each experiment may have one at `experiments/<name>/config/analyses.yaml`. `trials new` writes it with one example analysis. `analyses: {}` is a valid file: the experiment has no analysis yet.

## A minimal example

```yaml
analyses:
  line_count:
    describe: "How many lines each recording holds, as a JSON file per recording."
    per: bag
    command: >-
      python3 experiments/my_experiment/scripts/verify_log.py {recording}
      --min-lines 0 --json {results}/line_count.json
```

## Keys of the file

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `analyses` | mapping | yes | none | One entry per analysis, by name. The name is what `trials analyze <experiment> <name>` takes. |

## Keys of an analysis

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `command` | string | yes | none | The command line, with placeholders. |
| `per` | string | no | `bag` | `bag` runs the command once per selected trial. `cohort` runs it once over all of them. |
| `describe` | string | no | empty | What the analysis computes. `trials analyze --list` shows it, and `analysis_metadata.json` keeps it. |
| `requires_topics` | list | no | empty | The topics that the analysis reads, as a note for the reader. The loader accepts the key, and nothing acts on it: `trials analyze`, the web interface and Explore run the analysis over every selected recording, also one that lacks these topics, and `analysis_metadata.json` does not record it. To leave out such trials, select the cohort with `--where`. |

## Placeholders

The placeholders depend on `per`. Each value stays one argument of the command.

| Placeholder | `per: bag` | `per: cohort` |
| --- | --- | --- |
| `{recording}` or `{bag}` | the directory of the one trial | not available |
| `{bags}` | not available | the directories of all selected trials |
| `{results}` | a directory named after the trial, under the run's results directory | the run's results directory |
| `{experiment}` | the experiment name | the experiment name |

`{bags}` as a whole argument becomes one argument per trial. Inside a larger argument, the directories are joined with spaces. A placeholder that the table does not list is an error, and the command does not run.

## What a run writes

Each executed run writes into `experiments/<name>/results/<time stamp>_<analysis>/`. The runner creates that directory, but not the per trial directories under it, so a command that writes into `{results}` of `per: bag` creates its own directory. When the commands finish, the run writes `analysis_metadata.json` with the analysis, the selection filter, the exact list of trials and the commands. A command that exits with an error counts as failed, and `trials analyze` then exits with code 1.

## Selecting the cohort

`trials analyze` selects the usable trials of the experiment from the [[coverage|coverage index]]. `--where factor=level` narrows the selection (repeatable). `--include-unverified` and `--include-excluded` widen it. Without `--execute`, the command only prints what it would run.

```bash
trials analyze my_experiment --list
trials analyze my_experiment line_count --where rate=fast
trials analyze my_experiment line_count --where rate=fast --execute
```

!!! tip
    The commands run in the working directory of `trials analyze`. The templates use paths relative to the project root, so start `trials analyze` from the project root.
