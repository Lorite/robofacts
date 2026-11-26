# Create an experiment

This page shows the fast way to create a new [[experiments|experiment]]. Most new experiments are a variant of one that exists, so you copy the nearest one, change only what differs, and let one command tell you when the copy is ready.

## The three steps

```bash
trials new <name> --from <nearest_experiment> --campaign <closest_campaign>:<new_campaign>
# edit what differs: the factors and the contract, runner.yaml, the checklist, the campaign, the design
trials validate experiments/<name>
```

The first command writes a renamed copy of the declarations. The second step is your own work. The last command answers "is it ready" with READY or NOT READY. Repeat the last two steps until it says READY.

!!! tip "Read only what you change"
    You do not need to read every reference page first. Read the page of the file you change, for example [[factors-yaml]] when you add a factor, and let `trials validate` find the rest.

## Copy the nearest experiment

`trials new <name> --from <experiment>` copies the declarations of an existing experiment under a new name. The name uses lowercase letters, digits and underscores, and starts with a letter.

What the copy holds:

- `config/`, the campaigns, and the scripts and other text files that a declaration names, with the source's name replaced. The command lists every file where it replaced the name.
- A short `README.md` that says which experiment the copy comes from. The design is the one part that a copy cannot carry over, so you write it again.

What the copy leaves out:

- Recorded data, run logs, results, notebooks, caches and large files.
- The tests of the source experiment (`test_*.py`). They test the source's scripts, so write the new experiment's own tests.
- A file at the top level or under `scripts/` that no declaration names. The command lists these under "Left out". They are usually the source's own notes and study scripts. Copy one by hand when the new experiment needs it.

A file under `config/` that no declaration names is copied, and the command lists it for review, because a launch file of another package may load it. Delete it when the new experiment does not use it.

`--campaign NAME` copies only that campaign, and `--campaign NAME:NEW` also renames it. The option is repeatable. Without it, the command copies every campaign with its name unchanged.

When no similar experiment exists, `trials new <name>` (without `--from`) starts from the commented template of the project's [[backends|backend]]. The [[first-project]] page walks through that template.

## Change only what differs

After the copy, the command prints the files to change, in this order:

1. `config/factors.yaml` and `config/trial_metadata_schema.yaml`: the [[factors]] and the [[metadata-contract|metadata contract]]. See [[factors-yaml]] and [[metadata-schema]].
2. `config/runner.yaml`: the launch, the action, the verify steps and the hosts. See [[runner-yaml]].
3. `config/preflight_checklist.json`: the [[gate|T-0 gate]]. See [[checklist-json]].
4. The campaign: the claim, the sweep and the operator steps. See [[new-campaign]].
5. `README.md` (the design), a `RUNBOOK.md` when the source had one, and the trial scripts.

Change one file, then run `trials validate` again. A small step makes the failing stage easy to read.

## Validate until READY

`trials validate` takes an experiment (its name or its directory) or one campaign file. It runs these stages, each one scoped to that experiment:

| Stage | What it checks |
| --- | --- |
| `check` | every campaign loads and expands (`trials check`) |
| `plan` | the planner accepts each campaign against the recorded trials (`trials plan`) |
| `gate` | every T-0 checklist the experiment uses is coherent (`trials gate --validate`) |
| `lint` | the declaration rules for this experiment only (`trials lint --experiment`) |
| `tests` | the experiment's own `test_*.py` files, when it has any |
| `dry_run` | `trials run --limit 1` once per execution host that the campaign uses |

Each stage prints ok, FAIL or skip with its time, and a failed stage prints the output of the command behind it. Nothing is launched, recorded or written, because every stage is the dry form of its command. The exit code is 0 for READY, 1 for NOT READY and 2 for a usage error.

```bash
trials validate experiments/my_variant                      # the whole experiment
trials validate experiments/my_variant/campaigns/sweep.yaml # one campaign
trials validate experiments/my_variant -v                   # print the output of every stage
trials validate experiments/my_variant --skip tests         # leave out a stage (repeatable)
```

The stages take seconds. Do not run the full test suite of the project by hand to be sure: that is the job of CI.

## After READY

READY means that the declarations are coherent. It does not mean that the system works. The next step is a first real trial:

```bash
trials run experiments/my_variant/campaigns/sweep.yaml --limit 1             # dry run: the exact commands
trials run experiments/my_variant/campaigns/sweep.yaml --limit 1 --execute   # one trial
```

Read the run log of that trial before you run the whole campaign. When the experiment moves a real robot, read [[hardware-runs]] first. The [[daily-loop]] page shows the commands of a normal working day.
