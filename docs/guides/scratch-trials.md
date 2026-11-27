# Scratch trials

A diagnostic question such as "is the drift caused by the battery?" or "did this morning's fix work?" needs one recorded run, not a cell of a [[campaigns|campaign]]. A scratch trial is that run: it goes through the campaign's stack, [[gate|T-0 gate]] and operator steps, and it never counts as coverage.

## Run one

```bash
# one diagnostic run with the campaign's own shape
trials run experiments/<exp>/campaigns/<c>.yaml --scratch --label battery-check --execute

# another shape: pin factors, or replace the whole action command
trials run experiments/<exp>/campaigns/<c>.yaml --scratch --set speed=slow --label battery-check --execute
trials run experiments/<exp>/campaigns/<c>.yaml --scratch --action "python3 my_action.py --hold 45" --execute
```

Without `--execute`, the command is a dry run: it prints the commands and records nothing.

Run a diagnostic through the framework even when a manual run looks faster. A manual run skips the gate, so it also skips the check that would have caught the fault. And a recorder that you start by hand can write to the wrong place, or record the wrong minute.

## The same as a real trial

- The full T-0 gate runs before anything moves.
- The runner shows the operator steps and waits for them, and then prints the live steps.
- The stack and its auxiliary launches come up as for a trial.
- The recorder always runs, and a `trial_metadata.json` lands beside the recording.
- The rules of [[hardware-runs]] apply: a scratch trial on a real robot needs a gate and a terminal, as any other trial.

## Different from a real trial, on purpose

- The recording goes to the experiment's scratch folder, as `<stamp>_scratch_<label>/`. The folder is `recordings_scratch/` on the [[process-backend|process backend]] and `bags_scratch/` on the [[ros2-backend|ROS 2 backend]]. The [[coverage|coverage index]] and the planner never read this folder.
- The metadata says `scratch: true`. This is a second lock: if someone moves a scratch recording into the counted folder by hand, it still counts as excluded.
- The verify steps do not run. They check the campaign's claim, and a scratch trial answers your own question instead.
- The runner makes no slim copy, unless you pass `--slim`. The copy then goes to the scratch slim folder, which the index never reads either.
- The runner chooses the name of the recording at run time and makes it unique on the disk. Never type a recording name yourself. `--label` only adds a word to the generated name.
- The closing line says SCRATCH, so a session log never mixes a scratch trial with coverage.

## The shape of a scratch trial

Without other options, a scratch trial uses the campaign's own shape: the default of each factor, the campaign's `fixed` block, and the first level of each swept factor.

| Option | Effect |
| --- | --- |
| `--set name=level` | pins a factor to another level (repeatable) |
| `--action "CMD"` | replaces the action command of `runner.yaml`. `{factor}` placeholders are filled as usual |
| `--label WORD` | a word for the recording's name |
| `--slim` | also leaves a slim copy, in the scratch slim folder |

`--action`, `--label` and `--slim` are options of `--scratch` only. Without `--scratch`, the command refuses them.

## Where you find them

`trials records <exp> --scratch-only` lists the scratch recordings of an experiment, and `--no-scratch` leaves them out. The [[web-ui|web interface]] lists them in a table of their own, marked as never coverage. The web interface has no button for a scratch trial: the campaign page shows the command, with a button that copies it.

A scratch trial is also the tool to measure the band of a factor check before a campaign checks against it. A campaign with `factor_checks: calibration` measures the condition of each factor check instead of checking it, and it runs only with `--scratch`. See [[campaign-yaml]].
