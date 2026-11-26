# Write a campaign

A [[campaigns|campaign]] says which set of trials to run, and under which conditions. This page shows how to write one by answering five questions, and how to let the validators refuse what is wrong before anyone runs a trial.

## 1. Start from the nearest campaign

Copy the campaign that is closest to what you need, and adapt it. The existing campaigns and the rules of the package are the living schema, so a copy carries fewer mistakes than a file written from nothing. `trials new <name> --from <experiment> --campaign <old>:<new>` copies and renames one campaign together with its experiment (see [[new-experiment]]).

The smallest complete campaign is the one in the template of `trials new`:

```yaml
name: baseline
experiment: my_experiment
describe: >-
  Sweep the rate factor with two repetitions per level. Write here what the resulting
  trials will evidence and what is safety-critical about collecting them: the operator
  reads this before the first trial.
repetitions: 2
sweep:
  rate: [slow, fast]
```

[[campaign-yaml]] lists every key.

## 2. One campaign, one claim

Decide what the trials of this campaign will show, and write it in `describe`. Add what is safety-critical about collecting them. The operator reads this text before the first trial, often next to the robot, so write it for the operator.

When you need a second claim, write a second campaign. Two small campaigns are easier to plan, run and review than one campaign that mixes two questions.

## 3. Sweep, fixed and repetitions

- `sweep` names the [[factors]] to vary and their levels. The campaign runs every combination, one cell per combination.
- `fixed` pins other factors to one level. A factor that the campaign does not name takes its default.
- `repetitions` is the number of trials per cell.

Every swept factor must declare a `metadata_key` that the [[metadata-contract|metadata contract]] marks as `factor: true`. Otherwise every command that plans or runs the campaign refuses it, because the coverage of that factor could never be measured.

Pin the factors that matter for the claim, even when the value equals the default. The metadata then records what actually held during the trial.

On the command line, `--set name=a` pins a factor and `--set name=a,b` sweeps it, for one call only.

## 4. The conditions belong to the campaign

A campaign also says what must hold around each trial:

- `preflight_checklist` names a checklist for this campaign only. Declare one only when the preconditions really differ from the experiment's `config/preflight_checklist.json`. See [[gate]].
- `operator_steps` are the steps the operator does before the trial. The runner shows them and waits until the operator confirms them, before anything launches.
- `operator_live_steps` are the steps the operator does while the trial runs. Keep the two lists apart: a live step in the first list gets done too early, and a preparation step in the second list gets missed.
- `when: first`, `when: repeat` or `when: always` (the default) on an operator step or a setup step chooses the trials it applies to, within one runner invocation. Give the first trial the full procedure, give later trials short `when: repeat` steps, and put a reset between trials in the campaign's `setup_steps` with `when: repeat`.
- `setup_steps` of the campaign run after the ones of `runner.yaml`, before the launch.
- `verify_steps` answer the question "what would prove that the trial did what the campaign claims". The steps of `runner.yaml` cover the general part (did the trial happen at all), and the campaign's steps cover what the campaign asked a person to do. The two lists merge, so never repeat a step of `runner.yaml`.

Optional keys add [[spoken-cues]] (`voice_cues`, `voice_announce`), opt out of the robot resets (`robot_reset: false`) or set the order of the trials (`order`).

## 5. Validate before anyone runs it

One command runs every check and prints one verdict:

```bash
trials validate experiments/<exp>/campaigns/<c>.yaml
```

The same checks, one at a time:

```bash
trials check experiments/<exp>/campaigns/<c>.yaml            # the expanded plan
trials plan  experiments/<exp>/campaigns/<c>.yaml            # wanted, covered, still missing
trials lint --experiment <exp>                               # the declaration rules
trials run   experiments/<exp>/campaigns/<c>.yaml --limit 1  # a dry run, creates nothing
```

Propose `--execute` only after the verdict is READY. For a campaign on a real robot, read [[hardware-runs]] before the first trial.

## When the validators miss something

The [[lint-rules|lint rules]] are the enforcement layer. When writing a campaign teaches you something that a machine can check, add a rule to the lint with its test, rather than a paragraph to a guide. A rule refuses the next mistake of every author, and a paragraph only helps the people who read it. [[development]] shows how to run the tests of the package.
