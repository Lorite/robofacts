# The words

RoboFACTS uses the terms of the design of experiments. This page defines each term in one or two sentences and links to the page that explains it.

Some files and keys keep an older name, because projects depend on them. The ROS 2 backend, for example, calls a recording a bag, and several options and record fields still say `bag`.

## The setup

- **Project**: one directory tree with a `trials.yaml` file, the shared configuration and the experiments. See [[projects|Projects]].
- **Experiment**: a physical setup with its software, declared in one folder `experiments/<name>/`. See [[experiments|Experiments]].
- **Backend**: the part that knows the middleware: how the stack starts and how a trial is recorded. The built-in `process` backend needs no middleware, and `ros2` is a separate package. See [[backends|Backends]].
- **Stack**: the processes that run the system under test during a trial, started from the backend's launch command and stopped after the recording.

## What you declare

- **Factor, level, default**: a factor is a variable the experiment controls, its levels are the values it may take, and its default is the level of every trial whose campaign does not name it. See [[factors|Factors]].
- **Realization**: how the runner applies a level: on a computer, as a launch argument, as a record profile, as a value inside a command, as the gate's mode, or by an operator. See [[factors#How a level is realized|factors]].
- **Metadata contract**: the fields every trial must record, a shared core plus the experiment's own fields. See [[metadata-contract|The metadata contract]].
- **Coverage axes**: the fields of the contract with `factor: true`. The coverage index groups the trials by them. See [[metadata-contract|The metadata contract]].
- **Campaign**: which factors to sweep, which to fix, and how many repetitions each cell gets. See [[campaigns|Campaigns]].
- **Cell**: one combination of levels, also called a condition. It is a row of `trials plan`. See [[campaigns|Campaigns]].
- **Domain**: simulation or hardware, recorded for every trial from `simulation:` in `runner.yaml`. See [[coverage|Coverage and records]].

## A trial and its checks

- **Trial**: one run of one cell, made of a recording and a metadata record (`trial_metadata.json`). See [[runner|The runner]].
- **Attempt**: a started trial, whether the gate refused it or not. Every executed attempt adds one line to the ledger `runs/attempts.jsonl`. See [[runner|The runner]].
- **Gate (T-0 gate)**: machine checks and operator confirmations, run seconds before every trial. Every verdict goes into the gate log `runs/preflight_gate.jsonl`. See [[gate|The T-0 gate]].
- **Pre-launch check**: a gate entry marked to run before the stack starts, so that a NO-GO stops the trial early. See [[gate|The T-0 gate]].
- **Verify step**: a check of the recording after the trial. A failure aborts the trial, because an action can exit 0 although the trial did not happen. See [[runner|The runner]].
- **Operator steps, live steps, setup steps**: what a person must do before a trial, what a person must do while it runs, and the commands the runner runs before the stack starts. See [[runner|The runner]].
- **First and repeat trials**: within one run of `trials run`, the first trial and the trials after it, which may need different operator and setup steps. See [[runner|The runner]].
- **Robot reset**: a robot's steps before every trial, such as a power cycle. See [[runner|The runner]].
- **Step trace, failure report**: what each step of an attempt ran, how long it took and how it ended, and for an attempt that did not run, a short report in `runs/<trial>.failure.md`. See [[runner|The runner]].

## What counts

- **Class**: every recorded trial is usable (it counts), unverified (its metadata breaks the contract) or excluded (aborted, quarantined by a person, or without data). See [[coverage|Coverage and records]].
- **Coverage, coverage index**: the usable trials per cell. `trials plan` reports what is wanted, covered and still missing, and `trials index` groups every trial by the coverage axes. See [[coverage|Coverage and records]].
- **Silent error**: a counted trial whose realized condition differs from the declared one. It can happen only in a factor that no check covers. See [[factors|Factors]].
- **Decision**: what a person decides about a recorded trial afterwards, with a reason: it must not count, a recorded level was wrong, or a verify rule was wrong. Nobody edits the runner's metadata. See [[decisions|A person's decision about a trial]].

## Other recordings and results

- **Scratch recording**: a diagnostic trial through the campaign's stack and gate that never counts as coverage. See [[scratch-trials|Scratch trials]].
- **Reduced recording**: a copy of a recording without its heavy data, such as images and point clouds. The ROS 2 backend calls it the slim copy and writes it to `bags_slim/`. See [[ros2-backend|The ROS 2 backend]].
- **Exploration**: one condition's stack, started live so that you can debug and tune it. It is never a trial and never coverage. See [[explore|Explore a live stack]].
- **Named analysis**: a declared computation over a cohort of usable trials, which records the exact list of recordings it read. See [[analyses|Analyses]].
