# Campaigns

A campaign says which trials an experiment needs: which factors to sweep, which to fix, and how many repetitions each combination gets. The planner compares a campaign with the recorded trials, and the runner runs only what is still missing.

## A campaign file

Campaigns live in `experiments/<name>/campaigns/<campaign>.yaml`:

```yaml
name: grid
experiment: demo_log_recorder
describe: >-
  Sweep two factors, so the coverage index shows a grid of four cells that fills in as the
  trials run.
repetitions: 1
sweep:
  rate: [slow, fast]
  duration: [short, long]
# fixed:
#   execution_host: laptop
```

- `experiment` is required: a campaign belongs to one [[experiments|experiment]].
- `sweep` lists the [[factors|Factors]] that vary and the levels each one takes.
- `fixed` pins factors at one level.
- Every factor the campaign does not name takes its default.
- `repetitions` is the number of trials each cell wants (default 1).
- `describe` says what the trials will show and what is safety-critical about them. The operator reads it before the first trial.

Without `name`, the campaign takes the name of its file.

## Cells, wanted, covered, missing

The planner expands the sweep into its cross product. Each combination of levels is a **cell**. Each cell wants `repetitions` trials. The campaign above has 2 × 2 cells and one repetition, so it wants four trials.

The planner then counts the usable recorded trials of each cell (**covered**) and reports what is **still missing**. A trial belongs to a cell through its recorded levels, not through the campaign that ran it: every factor with a metadata key takes part in the match, and a factor the campaign does not name must be at its default. So two campaigns that ask for the same cell share its trials. See [[coverage|Coverage and records]].

```bash
trials plan experiments/demo_log_recorder/campaigns/grid.yaml
trials check experiments/demo_log_recorder/campaigns/grid.yaml    # does it load, and what does it expand to
```

## What a campaign may not do

Every command that loads a campaign refuses it, with the reason, when:

- it names a factor the experiment does not declare, or a level the factor does not declare,
- it asks for a level that the factor declares `blocked:`,
- a sweep lists the same level twice (use `repetitions` instead),
- a factor is both swept and fixed,
- `repetitions` is below 1,
- it sweeps a factor that is not recorded as a coverage axis (see [[metadata-contract#The invariant|the invariant]]).

## Repetitions per cell and randomized order

A cell may need more trials than the others, for example a control condition:

```yaml
repetitions: 5
repetitions_where:
  - {where: {condition: control}, repetitions: 10}   # the first matching entry sets the count
order: {shuffle: true, seed: 12345, blocks: true}
```

`order` changes when a trial runs, never what is wanted or covered. With `blocks: true`, the runner makes randomized complete blocks, each holding every cell, and `shuffle` draws the order inside each block from `seed`. A shuffle needs an integer seed, so the order can be reproduced. `trials plan <campaign> --schedule` prints the execution order, and every trial records its place in it.

A trial's `repetition` counts the trials of its cell across runs of `trials run`. It continues after the cell's recorded trials, so a cell run on two days records 1, 2 and 3, not 1, 1 and 2.

## Conditions the campaign owns

A campaign is "which set of trials, under which conditions". So it may also declare the conditions of its trials:

| Key | What it adds |
| --- | --- |
| `operator_steps` | what a person must do before each trial. The runner shows them and waits for a confirmation |
| `operator_live_steps` | what a person must do while the trial runs |
| `setup_steps` | commands that run after those of `runner.yaml` and before the stack starts |
| `verify_steps` | checks of the recording, added to those of `runner.yaml` |
| `preflight_checklist` | the campaign's own T-0 gate, instead of `config/preflight_checklist.json` |
| `robot_reset` | `false` skips the robots' reset steps for this campaign |
| `voice_cues`, `voice_announce` | spoken cues during the trial (see [[spoken-cues|Spoken operator cues]]) |

The [[runner|The runner]] explains when each of these runs. [[campaign-yaml|Campaign files]] lists every key.

Give a campaign its own gate when its preconditions really differ. Write that checklist as a positive list of what must hold for these trials, not as the experiment's list with entries removed. A gate that you expect to fail teaches the operator to ignore a NO-GO.

Put the verify step that always applies in `runner.yaml`, and the one specific to this campaign in the campaign: the runner adds the campaign's steps to its own and never replaces them.

## Override from the command line

`trials plan`, `trials check` and `trials run` take `--set name=level` to pin a factor and `--set name=a,b` to sweep it, without a change to the file. A pinned level must be one the factor declares.

```bash
trials run experiments/demo_log_recorder/campaigns/grid.yaml --set rate=fast --limit 1
```

## When a campaign is complete

When nothing is missing, `trials run` prints `Nothing to run — the campaign is already covered.` To record more trials, raise `repetitions`, or write a new campaign. A [[scratch-trials|scratch trial]] answers a one-off question without a change to the campaign. [[new-campaign|Write a campaign]] is the full procedure for a new campaign.
