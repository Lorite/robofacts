# The T-0 gate

The T-0 gate runs seconds before every trial and decides whether it may start. It runs the machine checks, lists the confirmations only a person can give, and returns one verdict: GO or NO-GO.

## Why a gate before every trial

A check that passed minutes ago says nothing about now: a battery drains, a process dies, a disk fills. So the runner runs the gate again before every trial. The checks run after the stack has started, on the computer where the trial runs, so they measure the real processes of this trial.

A gate that cannot run has not passed.

## The checklist

The gate reads `config/preflight_checklist.json` of the experiment. A [[campaigns|campaign]] may name its own checklist with `preflight_checklist:`. Each entry is one of two kinds:

- **a machine check**: an object with the entry's `text`, the `check` script that verifies it and its `args`,
- **an operator confirmation**: a plain string. No script can verify it: `trials gate` prints it as a box to tick, and the web interface lists it as yours to confirm. Only the machine checks decide the verdict.

```json
{
  "variables": {},
  "checklist": [
    {"text": "Enough disk for the recording.", "check": "disk_space", "args": {"path": ".", "min_gb": "0.1"}},
    {"text": "[HARD] The recording directory is writable on the machine that records.",
     "check": "recording_dir_writable", "args": {"path": "experiments/my_experiment/recordings", "label": "recordings"}},
    "Operator: the system and the recording are the ones this trial declares."
  ]
}
```

The gate fills `${name}` in an argument from the checklist's `variables`, from the trial's factor levels and from its launch arguments (as `${arg_<name>}`). [[checklist-json|preflight_checklist.json]] lists every key of an entry.

## A check

A check is a bash script that prints one line starting with `PASS:`, `FAIL:` or `SKIP:` and exits with 0, 1 or 3. A value that cannot be read is a FAIL, a usage error (exit code 2) is a NO-GO too, and a SKIP means "not applicable to this experiment". The gate looks for a check in the project's own checks (`experiments/common/preflight/checks/`) first, then in the enabled robot adapters, the backend, and the general checks of the package. A project check with the same name as a packaged one wins. See [[gate-checks|Gate checks]] for the shipped checks and [[own-gate-checks|Write your own gate checks]] to write your own.

## Hard and soft entries

Every failed machine check makes the verdict NO-GO, and a NO-GO blocks the trial. What differs is whether an override may pass it. An entry is **hard** when its text starts with `[HARD]`, or when it says `"hard": true`. An explicit `"hard"` field wins over the text. Every other machine check is **soft**.

## The override

`trials run --gate-override "<reason>"` lets a trial proceed past a NO-GO, but only within strict limits:

- It **never** passes a failed `[HARD]` item.
- It **never** passes a factor check that did not pass.
- It **can** pass a failed soft item. The reason and the failed checks are then stamped into the trial's `trial_metadata.json` (in its `preflight` block: `overridden`, `override_reason`, `failed_checks`) and into the gate log.

When a hard item or a factor check failed, the gate blocks the trial with or without the override, and the message says to fix the cause. The override cannot be combined with a `gate` factor, because that factor already decides per trial whether a NO-GO blocks.

!!! warning
    An overridden trial runs and is recorded like any other, and it can count as coverage. The override stays visible in its metadata, so a person can decide later that it must not count (see [[decisions|A person's decision about a trial]]).

## A hardware trial runs only after a gate

!!! warning "No gate, no hardware trial"
    A trial in the hardware domain (`simulation: false` in `runner.yaml`) runs only after a T-0 gate. `trials run --execute` refuses it when the experiment has no gate checklist, and the web interface refuses its "skip the gate" option for a hardware experiment.

A simulation without a checklist still runs, and the runner prints an `UNGATED` warning for it.

## Pre-launch checks

An entry with `"stage": "pre_launch"` runs before the stack starts, not at T-0. A pre-launch NO-GO stops the trial in seconds, before a slow launch or a robot reset. The other entries run at T-0, after the stack is up. `trials gate` runs both kinds.

## Checks that a factor adds

A [[factors|factor]] that a person realizes may declare a `check:`. When a campaign names that factor, the runner adds the check to the trial's T-0 gate with the trial's level, so a declared condition that the measurement does not show blocks the trial. The result, with the measured value, goes into the trial's metadata under `preflight.factor_checks`.

## The gate as a factor, in simulation only

A factor with `maps_to: {kind: gate}` selects the gate's mode per trial, so that one campaign can measure the effect of the gate itself: `binding` (the normal gate), `shadow` (every check runs and is recorded, but a NO-GO never blocks) or `disabled` (nothing runs). The runner and lint rule L12 allow it only when `runner.yaml` declares `simulation: true`, because a shadow gate on a robot would run a trial that the checks refused. [[factors-yaml|factors.yaml]] shows how its levels map to the modes.

## What a verdict leaves behind

Every verdict (GO, NO-GO, overridden, shadow, disabled) adds one JSON line to the gate log, `runs/preflight_gate.jsonl`, with the failed items and the gate's duration. A NO-GO leaves no recording, so this line is its only record besides the run log. A trial that ran carries its gate result in the `preflight` block of `trial_metadata.json`.

## Run a gate by hand

```bash
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json --list       # the plan, nothing runs
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json --validate   # is the checklist coherent
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json              # run it
```

`--var name=value` fills a `${name}` placeholder. Consecutive checks that only read run together, which keeps the gate short, and `--concurrency sequential` runs them one after the other with the same verdict. An entry may also carry `"speak"`, the sentence the operator voice says when it fails (see [[spoken-cues|Spoken operator cues]]).
