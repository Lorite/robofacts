# Run on hardware

This page states the rules that apply when a trial moves a real robot. The [[runner]] enforces each rule before anything of the trial runs, and the [[web-ui|web interface]] refuses the same cases.

!!! warning "The software is not the primary stop"
    Keep the physical emergency stop of every robot within reach during every hardware trial. The software stop travels from your machine over ssh and a container to the robot's link, and every step can fail. See [[stopping-robots]].

## Which trial is a hardware trial

Every trial belongs to one domain: simulation or hardware. The domain decides which rules apply, and the trial records it as `domain` in its `trial_metadata.json`.

- A trial is a simulation when the experiment's `runner.yaml` says `simulation: true`. Otherwise it is a hardware trial. An experiment that says `simulation: false`, or that says nothing, runs hardware trials. Lint rule L14 requires every `runner.yaml` to say it explicitly.
- A factor can decide the domain per level, with `domain:` in `factors.yaml`, for example a level that runs a simulated robot inside an experiment that otherwise moves real ones. A level that `domain:` does not name follows `runner.yaml`. See [[factors-yaml]].

## Before the first hardware trial

1. Run `trials validate experiments/<exp>` until it says READY (see [[new-experiment]]).
2. Give the experiment a T-0 gate checklist, `config/preflight_checklist.json`, or a campaign checklist named by `preflight_checklist`. Add the `stop_latch` check, so that a recent stop refuses the next trial. See [[checklist-json]] and [[gate-checks]].
3. Run the campaign without `--execute`, and read the commands it prints.
4. Check where a stop goes: `trials tool estop.sh --list` prints each robot's host and the reason.
5. Start the run in a terminal that you watch. On another machine, start it inside a terminal multiplexer such as tmux, so that a dropped network connection does not end the run (see [[other-machines]]).

## The rules

**A hardware trial needs a T-0 gate.** The gate is the campaign's checklist. When the file does not exist, `trials run --execute` refuses the hardware trial before anything runs. A simulation without a checklist still runs, with an UNGATED warning.

**The gate runs before every trial.** A check that passed minutes ago proves nothing about now, so the runner runs the gate again seconds before each trial. Each verdict goes into the trial's metadata and into the gate log, `runs/preflight_gate.jsonl`. A gate that cannot run has not passed.

**The runner waits until the operator types `fly`.** After the gate passes and before the recorder starts, the runner stops at the pose pause, so that you can pose the robot without a race. On an experiment whose `runner.yaml` does not say `simulation: true`, type `fly` and Enter to record and act, or `no` to abort. Any other answer asks again, and after three tries the trial aborts. The recorder and the action do not start before `fly`, and an aborted trial records nothing. In a simulated experiment, Enter is enough.

**A hardware run needs a terminal on stdin.** When stdin is not a terminal, nobody could type `fly`, so `trials run --execute` refuses a hardware trial. This also holds for `--scratch`.

**`--no-pause` skips the confirmation.** With `--no-pause`, the runner does not stop at the pose pause, and it accepts a hardware run without a terminal. Without a terminal, the runner prints the operator steps with a line that says that nobody waited for them. Use `--no-pause` only when another person or system confirms that the robot is ready.

**The override never passes a hard failure.** `--gate-override "<reason>"` lets a trial proceed past a failed gate, and the reason and the failed checks go into its `trial_metadata.json`. The override never passes these two failures:

- a failed `[HARD]` item: a checklist entry whose text starts with `[HARD]`, or that says `"hard": true`.
- a factor check that did not pass, because the trial's declared level was then not confirmed.

Such a trial is blocked with or without the override: fix the cause, then run again. A failed soft item may still be overridden, with its reason.

**A shadow gate is for simulation only.** A campaign that names a factor of `maps_to.kind: gate` (a gate that records its verdict but never blocks, or no gate) runs only when `runner.yaml` says `simulation: true`. `--gate-override` cannot be combined with such a factor.

## In the web interface

The web interface refuses the same cases before the run starts:

- EXECUTE runs only when the arm checkbox next to it is ticked.
- "Skip the gate" is refused for a hardware trial, and so is a campaign without a checklist.
- The browser answers the pose pause, so no terminal is needed. Keep "pause before recording" ticked (the default): the run then waits after the gate passes until you confirm on the run page.
- The run waits before anything launches until you press "Steps done" for the operator steps.
- Abort ends the campaign after the current trial and never interrupts a trial in progress. To stop a robot, use the stop bar.

## What the operator sees and hears

At the start of every hardware trial, the run log names where `estop.sh` on this machine reaches each robot. On a NO-GO, the runner prints each failed check, and with [[spoken-cues]] the voice says up to three of them. On a GO, the voice says by default "The gate passed. Type fly when you are ready."

## Related pages

- [[gate]] explains the gate, and [[checklist-json]] its file.
- [[explore]] brings up a live stack without a gate. On real robots it needs an explicit list of writable parameters and an arm checkbox.
- [[scratch-trials]] gives a diagnostic run the same gate and pause.
