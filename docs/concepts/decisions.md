# A person's decision about a trial

Sometimes a person knows more about a trial than the runner did. This page shows how to record that decision, with a reason, so that every reader of the data applies it.

## The rule: never edit, never move by hand

The runner writes `trial_metadata.json` once, from the plan and the checks of that moment, and nobody edits it afterwards. A hand edit destroys the recorded value, and a hand move of a recording leaves no reason for anyone to read. So each decision has a command, and each command writes a file with a reason beside the recording and beside its reduced copy:

| Decision | Command | File |
| --- | --- | --- |
| the trial must not count | `trials exclude` | `exclusion.json` |
| an excluded trial counts again | `trials restore` | `exclusion_restored_<time>.json` |
| a recorded level was wrong | `trials amend` | `corrections.json` |
| a verify rule was wrong | `trials reverify` | `reverification.json` |

The [[coverage|coverage index]], the planner, `trials records` and the web interface read these files, so they all follow the decision. Each command takes the trial by the name of its recording folder, as `trials records` lists it.

## It must not count: exclude

```bash
trials exclude my_experiment 20261126T101500_rate-fast_rep01 \
    --reason "The disk filled during the trial." --category infrastructure
```

`trials exclude` moves the recording and its reduced copy into the issues folder (`recordings_issues/` on the `process` backend, `bags_issues/` on ROS 2) and writes `exclusion.json` beside each: the date, who decided, the category, the reason, and optional evidence (`--evidence <file>`, repeatable). Nothing is deleted. The categories are:

- `infrastructure`: the network, the power or the disk failed,
- `operator`: the trial was not run as planned, and a correction cannot fix it,
- `estimator`: the method under test failed. This is a result, so exclude it only with a strong reason,
- `other`.

`trials records` then shows the trial as `excluded (quarantined)` with the reason:

```text
2026-01-01 10:01:00 UTC  excluded (quarantined)  20260101T100100_rate-fast_rep01
      why: infrastructure: The disk filled during the trial. (tester, 2026-11-26)
```

`exclude` refuses a trial that the runner may still be writing: a recording without metadata yet, a recording with a file younger than 120 s, or one that a run still in progress created. `amend` and `reverify` refuse such a trial too. For a trial already in the issues folder without a reason, it writes the reason in place.

## It counts again: restore

```bash
trials restore my_experiment 20261126T101500_rate-fast_rep01 --reason "The stall was outside the analyzed window."
```

`trials restore` moves the trial back and keeps its old `exclusion.json` as `exclusion_restored_<time>.json`, so the history stays.

## A recorded level is wrong: amend

```bash
trials amend my_experiment 20261126T101000_rep01 duration=long --reason "The run log shows the long duration."
```

`trials amend` writes `corrections.json` beside the recording and its reduced copy: one entry per change, with the recorded value, the corrected one, who, when and why. The level must be one the experiment declares. The index reads the corrected value on top of the recorded one, so the trial counts in its true cell. `trials plan` and `trials index` name the corrected trials, `trials records` prints a `corrected:` line, and the web interface shows a badge and a table of recorded and corrected values:

```text
2026-01-01 10:00:00 UTC  usable                  20260101T100000_rep01  corrected=duration
      corrected: duration: short -> long (The run log shows the long duration.)
```

Prefer `amend` over `exclude` when the trial is good and only its label is wrong. This happens most often with a level that a person realizes: a [[factors|factor]] of kind `operator` without a `check:` is not verified at T-0, so its recorded value is the plan's value.

## A verify rule was wrong: reverify

A verify step may judge a recording wrongly, for example a rule written for one mode that misfires in another. The trial then stays aborted, although its data is valid. After you fix the rule, run the current verify steps again instead of running the trial again:

```bash
trials reverify my_experiment 20261126T102000_rep01 --reason "The minimum line count was too strict." --dry-run
trials reverify my_experiment 20261126T102000_rep01 --reason "The minimum line count was too strict."
```

`trials reverify` finds the trial's campaign and levels in the attempt ledger (`runs/attempts.jsonl`), with any corrections on top. It renders the current verify steps of `runner.yaml` and the campaign as the runner does, runs every step (also after a failure), and writes `reverification.json` with each step's command, return code and output tail, the code version, who, when and why. It reads the full recording when it holds data, else its reduced copy. `--dry-run` prints the rendered steps and runs nothing.

The index lifts the abort only when all three hold:

1. the runner's abort came from a verify step alone (the action itself succeeded),
2. every current step passes,
3. the record was written against that same abort reason.

An abort for any other reason is never lifted. When a re-verify fails on a usable trial, the command records and reports the failure, but it does not exclude the trial: exclusion stays a person's decision. Run `reverify` on the computer that recorded the trial, in the environment where its verify steps can read the recording.

## Who decided

Every command takes `--by <name>`. Without it, the name comes from `$TRIALS_BY`, else from your login name.

## With the ROS 2 backend

On ROS 2, the issues folders are `bags_issues/` and `bags_issues_slim/`. A slim copy that `trials slim` makes later copies both decision files, so the copy counts exactly as its bag does. See [[ros2-backend|The ROS 2 backend]].
