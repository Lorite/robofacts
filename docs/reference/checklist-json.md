# preflight_checklist.json

`preflight_checklist.json` is the [[gate|T-0 gate]] of an experiment: what must hold seconds before each trial. This page lists the keys of the file and of its entries.

## Where the file is

Each experiment has one at `experiments/<name>/config/preflight_checklist.json`. A [[campaign-yaml|campaign]] may name another file of the experiment with `preflight_checklist:`. Without a checklist, the runner refuses a hardware trial, and a simulated trial runs with an UNGATED warning.

## A minimal example

```json
{
  "variables": {},
  "checklist": [
    {
      "text": "Enough disk for the recording.",
      "check": "disk_space",
      "args": {"path": ".", "min_gb": "1"}
    },
    {
      "text": "[HARD] The recording directory is writable on the machine that records.",
      "check": "recording_dir_writable",
      "args": {"path": "experiments/my_experiment/recordings", "label": "recordings"}
    },
    "Operator: the system and the recording are the ones this trial declares."
  ]
}
```

## Keys of the file

The file is a JSON object, or only the list of entries.

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `checklist` | list | yes | none | The entries, in order. An empty list is refused: a gate that checks nothing has not passed. |
| `variables` | object | no | empty | Default values of the `${name}` placeholders in the arguments. |
| `concurrency` | string | no | `concurrent` | `concurrent` or `sequential`. See [[gate-checks#Concurrency classes|concurrency classes]]. `--concurrency` and `$TRIALS_PREFLIGHT_CONCURRENCY` win over it. |

The gate ignores any other key, so the templates keep their comments under `_comment`.

## Keys of an entry

An entry is a plain string or an object. A string is a manual entry: the operator confirms it. An object with `check` is a machine entry: a script verifies it.

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `text` | string | yes | none | What must hold, as the gate report and the operator read it. |
| `check` | string | no | none | The [[gate-checks|gate check]] to run, by its script name without `.sh`. Without it, the entry is manual. |
| `args` | object | no | empty | The arguments of the check, passed as `key=value`. Every value becomes a string. |
| `hard` | boolean | no | `true` when `text` starts with `[HARD]` | A hard entry cannot be overridden: `--gate-override` never passes a failed hard entry. |
| `parallel` | boolean | no | the check's own class | `true` lets the entry share a batch with other checks, `false` makes it run alone. A check that must run alone cannot be declared `true`. |
| `stage` | string | no | the T-0 stage | `pre_launch` runs a machine entry before the stack starts, so a fault stops the trial in seconds. |
| `speak` | string | no | a sentence from the check's result | The sentence that the voice says when the entry fails. `{detail}` is replaced with the check's own detail. |

## Variables

`${name}` in an argument is a variable. The runner passes every factor value of the trial as a variable, so a checklist can say `${world}` for a factor `world`. A variable given with `trials gate --var name=value` also wins over the default in `variables`. `trials gate --validate` reports a placeholder that no variable fills.

## How the gate reads the result

Each check prints one line and exits with a code: `0` PASS, `1` FAIL, `2` a usage error, `3` SKIP. A FAIL or a usage error of any machine entry is a NO-GO. The manual entries come after every machine check. To run a checklist by hand, see the `trials gate` section of [[commands]].

```bash
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json --list
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json --validate
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json
```

!!! note
    A [[factors-yaml|factor check]] adds its own entry to the gate of each trial whose campaign names the factor. You do not write that entry into this file.
