# The runner

The runner turns a campaign's plan into trials. `trials run` asks the planner which trials are still missing, runs each one in a fixed order, and writes the metadata that lets the coverage index count it.

## Dry run first

```bash
trials run experiments/my_experiment/campaigns/baseline.yaml --limit 1             # dry run
trials run experiments/my_experiment/campaigns/baseline.yaml --limit 1 --execute   # one real trial
```

Without `--execute`, the runner prints the plan and the exact commands of the next trials, with their operator steps, live steps and setup steps, and runs nothing. `--limit N` runs at most N trials. The runner renders every command of the planned trials before the first trial starts, so a typo in a placeholder stops the run before anything moves.

## The order of one trial

The runner owns the order, and `runner.yaml` only declares the content of the steps:

1. **Operator steps**: the runner shows the campaign's `operator_steps` and waits until you confirm them. Then it prints the `operator_live_steps` and does not wait for them.
2. **Setup steps** of `runner.yaml`: foreground commands. A required step that fails aborts the trial.
3. **Pre-launch checks**: the gate entries marked `"stage": "pre_launch"`.
4. **Robot resets** of the robots that `runner.yaml` declares (for example a power cycle), then the campaign's own **setup steps**.
5. **The stack** starts in the background, and the runner waits `settle_s`.
6. **The [[gate|T-0 gate]]**. A NO-GO blocks the trial: nothing is recorded and the action does not run.
7. **The pose pause** (see below).
8. **The recorder** starts, then **the action**: the one foreground command that is the trial.
9. **Teardown**: the runner stops the recorder first, so that the recording is finalized, and then the stack.
10. **The recording**: without recorded data, the runner writes no metadata, so the trial can never count.
11. **Verify steps** check the recording.
12. **Metadata**: `trial_metadata.json`, with every level under its metadata key, the gate result, the code version and the domain (simulation or hardware). Then the backend may make a reduced copy.

## The pose pause

After the gate, every executed trial waits so that you can pose the setup (stand the robot up, place an object) before the recorder starts:

- in a simulation trial, the runner waits for **Enter**,
- in a hardware trial, it waits for the word **`fly`** and Enter. The word `no` aborts the trial, and after three other answers it aborts too.

The trial's domain decides which of the two: `simulation` in `runner.yaml`, unless a factor level names a domain (see [[factors]]). `--no-pause` skips the pause. The web interface answers the pause in the browser and needs no terminal.

!!! warning "Hardware needs a terminal or --no-pause"
    `trials run` refuses a hardware run whose standard input is not a terminal, unless you pass `--no-pause`, because nobody could type `fly`. With `--no-pause` and no terminal, the runner prints the operator steps and says that nobody waited for them. A hardware trial also needs a [[gate|T-0 gate]]. See [[hardware-runs|Run on hardware]].

## Operator steps and live steps

A campaign declares two lists of things a person must do, because they are read at different moments:

- `operator_steps` come **before** the trial. The runner prints them and waits for Enter before anything launches. A step's text may name a factor (`{lighting}`), so you read the trial's own level.
- `operator_live_steps` come **while** the trial runs, during the action. The runner does not wait for them. `trials run` prints them when each trial starts, after the operator steps and before the setup steps, also without a terminal. Their texts name the trial's own levels, as in the operator steps, and the run log holds them. The web interface shows them on the run page, and a step that declares `run:` gets a button that runs its command on the trial's computer. `trials run` prints that command for you to run by hand.

An operator step and a setup step may say `when: first`, `repeat` or `always` (the default). Within one run of `trials run`, the first trial gets the `first` and `always` steps, and every later trial the `repeat` and `always` steps. A trial after a blocked one gets the first-trial steps again.

## Verify steps

An action can exit 0 although the trial did not happen: a robot that never moved, a stack that never started. So verify steps check the **recording**, after the recorder has stopped. `{recording}` (or `{bag}`) in a verify command is the trial's recording directory, and `{factor}` placeholders take the trial's levels. A non-zero exit aborts the trial, and an aborted trial never counts.

```yaml
verify_steps:
  - name: log_was_recorded
    command: "python3 experiments/my_experiment/scripts/verify_log.py {recording} --rate {rate} --min-lines 3"
```

The verify steps of `runner.yaml` apply to every trial, and the runner adds a campaign's `verify_steps` to them. Every experiment declares at least one, or a written reason why it has none (`no_verify_steps: <reason>`), and lint rule L6 enforces this. When a verify rule proves wrong, `trials reverify` runs the current steps again on a recorded trial (see [[decisions|A person's decision about a trial]]).

## Blocked and aborted trials

- A **blocked** attempt never started its action: a NO-GO, unconfirmed operator steps, or no `fly` at the pose pause. It leaves no recording. The run stops after a blocked trial, because a setup that fails the gate usually fails it again, unless you pass `--keep-going`.
- An **aborted** trial ran but did not happen as planned: the action failed, a stop ended it, or a verify step failed. The runner records it with `aborted: true` and its reason, and it never counts.

Every executed attempt adds one line to the ledger `runs/attempts.jsonl`, with its outcome, its levels, its gate verdict and a step trace (what each step ran, how long it took, how it ended). An attempt that did not run also gets a short failure report, `runs/<trial>.failure.md`. `trials records` lists the attempts that left no trial (see [[coverage|Coverage and records]]).

## Stops

Ctrl-C goes to the action first, so a flight can land. The runner waits up to 90 s for the action to end, and only then stops the recorder and the stack. A second Ctrl-C stops everything at once. Only one executed run per project runs at a time. See [[stopping-robots|Stop robots]].

## Scratch trials

`trials run <campaign> --scratch` runs one diagnostic trial through the campaign's stack, gate and operator steps. It records into the scratch folder, skips the verify steps, and never counts as coverage. See [[scratch-trials|Scratch trials]].

## Other options

`--set name=a[,b]` pins or sweeps a factor for one run, and `--bags-root` chooses where the heavy recordings go (see [[storage|Storage]]). [[commands|Commands]] lists every option, and [[runner-yaml|runner.yaml]] every key of `runner.yaml`.
