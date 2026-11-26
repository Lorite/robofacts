# factors.yaml

`factors.yaml` declares the [[factors]] that a [[campaigns|campaign]] may vary: their levels, their defaults, the metadata field that records them, and how the runner applies a level. This page lists every key of the file.

## Where the files are

A project has two kinds of factor files:

- `experiments/common/config/factors.yaml` holds the core factors of the project. It must exist.
- `experiments/<name>/config/factors.yaml` holds the factors of one experiment. It is optional.

The loader merges the experiment's file on top of the core file. A factor of the experiment with the same name as a core factor replaces it, for example to narrow its levels or to change its default.

## A minimal example

```yaml
version: 1
factors:
  rate:
    describe: How fast the system writes to its log.
    levels: [slow, fast]
    default: slow
    metadata_key: rate
    maps_to: {kind: internal}
```

## Keys of the file

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `factors` | mapping | yes | none | One entry per factor, by name. |
| `version` | integer | no | none | The templates write `version: 1`. The loader does not read it. |

## Keys of a factor

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `levels` | list | yes | none | The legal values. A campaign may only ask for these. The list must not be empty. |
| `default` | a level | yes | none | The level of a trial whose campaign does not name the factor. It must be one of `levels`. |
| `metadata_key` | string | for a swept factor | none | The field of `trial_metadata.json` that records the level. A campaign that sweeps the factor is refused unless the [[metadata-schema|metadata contract]] declares this field with `factor: true`. |
| `maps_to` | mapping | no | `{kind: internal}` | How the runner applies a level. See the next section. |
| `describe` | string | no | empty | What the factor means and why it matters. |
| `aliases` | mapping | no | empty | A level to a list of other spellings of it in recorded metadata, so that older trials still match the level. |
| `blocked` | mapping | no | empty | A level to the reason why it cannot run now. A campaign that asks for a blocked level is refused. The reason must not be empty, and the default level cannot be blocked. |
| `check` | mapping | no | none | A gate check that confirms the level of each trial. See the section below. |
| `unchecked` | string | no | empty | Why a factor of kind `operator` has no check. A factor may not declare both `check` and `unchecked`. |
| `domain` | mapping | no | empty | A level to `simulation` or `hardware`, for a level that decides the domain of the trial. The other levels follow `simulation` in `runner.yaml`. |

## The kinds of `maps_to`

| `kind` | How the runner applies a level | Other keys of `maps_to` |
| --- | --- | --- |
| `internal` | as the value of the `{name}` placeholder in the commands of [[runner-yaml|runner.yaml]] | none |
| `launch_arg` | as a launch argument: the placeholder `{arg_<argument>}` in the commands, and with the ROS 2 backend an argument of `ros2 launch` | `name` or `names` (one of them is required), `level_values` |
| `host` | as the machine that runs the trial: the level must be listed under `hosts:` in `runner.yaml` | none |
| `record_profile` | as the [[record-profiles|record profile]] of the trial | none |
| `gate` | as the mode of the [[gate|T-0 gate]]: `binding`, `shadow` or `disabled` | `modes` |
| `operator` | not at all: the operator sets up the condition by hand | the factor needs `check` or `unchecked` |

`names` is a list of launch arguments that all receive the level. `level_values` maps a level to the value that the launcher needs. It may also map a level to a mapping of argument names to values, so one level sets several arguments.

A `gate` factor's levels must be strings, so quote `"on"` and `"off"` (YAML reads them as booleans otherwise). Without `modes`, `on` means `binding`, `off` means `shadow` and `disabled` means `disabled`. [[lint-rules|Lint rule]] `L12` allows a `gate` factor only in an experiment with `simulation: true`.

### With the ROS 2 backend

The ROS 2 launcher appends each launch argument as `name:=value` to `ros2 launch`. It leaves out an argument whose value is empty, so the launch file's own default applies. With `launch_mode: run`, the stack gets no launch arguments.

## The `check` block

A factor check adds one entry to the gate of every trial whose campaign names the factor. The entry carries the arguments of the trial's level.

```yaml
lighting:
  levels: [lights_on, lights_dimmed]
  default: lights_on
  metadata_key: lighting
  maps_to: {kind: operator}
  check:
    script: image_brightness
    topic: /camera/image_raw/compressed
    window_s: 6
    per_level:
      lights_on: {min: 95, max: 160}
      lights_dimmed: {min: 20, max: 70}
```

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `script` | string | yes | none | The [[gate-checks|gate check]] to run, with or without `.sh`. |
| `args` | mapping | no | empty | Arguments for every level. Any other key of the block that is not in this table is an argument for every level too. |
| `per_level` | mapping | no | empty | A level to its own arguments. When present, it must name every level. A `null` argument reaches the check as an empty value, so a band that nobody has measured fails the gate. |
| `hard` | boolean | no | `true` | Whether the gate entry starts with `[HARD]`. `--gate-override` never passes a failed factor check, whatever this value. |
| `text` | string | no | a generated sentence | The text of the gate entry. |
| `parallel` | boolean | no | the check's own class | Whether the entry may share a batch with other checks. |
| `realized` | mapping | no | none | A measurement of the realized value, written into the trial's metadata: `key` and `command` (both required), and `json` (default `true`). |
| `calibrate` | string | no | empty | The command that a campaign with `factor_checks: calibration` runs instead of the check. Its last output line is JSON. |

`{factor}` placeholders in the arguments are filled from the trial's levels. `${variable}` placeholders are left to the checklist's own variables.
