# Factors

A factor is a variable that an experiment controls. This page explains factors, levels and defaults, how the runner applies a level, and how a level is recorded and checked.

## Factor, level, default

A factor has a list of levels, the values it may take, and a default level. A [[campaigns|campaign]] sweeps some factors over several levels and fixes others at one level. Every factor that the campaign does not name takes its default. Here is a factor of the log recorder example:

```yaml
factors:
  rate:
    describe: >-
      How fast the system writes to its log. The stack is started with this level, so it is
      a property of the system under test.
    levels: [slow, fast]
    default: slow
    metadata_key: rate
    maps_to: {kind: internal}
```

The core factors of `experiments/common/config/factors.yaml` apply to every experiment: `execution_host` (the computer that runs the trial) and `record_profile` (what the trial records). An experiment declares its own factors in `config/factors.yaml`. It may also redeclare a core factor by name, for example to narrow its levels or to change its default.

The planner marks a level that differs from the default with a star (`rate=fast*`), and the name of a recording carries its non-default levels.

## The metadata key

Every factor names a `metadata_key`: the field of `trial_metadata.json` that records its level. The runner writes each trial's levels under these keys, and the planner and the coverage index read them back to find the cell of each trial.

A factor that a campaign sweeps must record to a key that the [[metadata-contract|metadata contract]] declares with `factor: true`. Otherwise `trials plan`, `trials check` and `trials run` refuse the campaign, and the linter fails (rule L10). Without the key, the planner could not tell the levels apart: it would merge them into one cell, and every trial would run at one level while the campaign claimed a sweep.

## How a level is realized

`maps_to.kind` says how the runner applies a level:

| Kind | The level |
| --- | --- |
| `internal` | fills a `{factor}` placeholder in the commands of `runner.yaml`. This is the default kind |
| `launch_arg` | is passed to the stack's launcher as one or more launch arguments (`name` or `names`). `level_values` may translate the level into the value the launcher needs |
| `host` | selects the computer that runs the trial, from the hosts that `runner.yaml` declares |
| `record_profile` | selects a named set of data to record (see [[record-profiles|Record profiles]]) |
| `operator` | is set by a person, and a gate check confirms it (see below) |
| `gate` | selects the mode of the T-0 gate for the trial: binding, shadow or disabled. It is allowed only in simulation (see [[gate|The T-0 gate]]) |

On the `process` backend, a launch command uses a launch argument as `{arg_<name>}`. On the ROS 2 backend, a launch argument becomes `name:=value` of `ros2 launch`.

`names` passes one level to several launch arguments, so that two arguments that must agree cannot drift apart. A `level_values` entry that is a mapping gives each launch argument its own value.

## A factor that a person realizes

Some conditions no runner can set, such as the lighting of the room or the surface of the floor. Such a factor is `maps_to: {kind: operator}`, and lint rule L13 requires one of two declarations:

- a `check:`, a gate check with arguments per level. When a campaign names the factor, the runner adds this check to the trial's T-0 gate, with the trial's level. A declared level that the measurement does not show blocks the trial before anything is recorded, and the measured value goes into the trial's metadata.
- `unchecked: <reason>`, a written reason why nothing can measure the condition.

```yaml
lighting:
  levels: [lights_on, lights_dimmed]
  default: lights_on
  metadata_key: lighting
  maps_to: {kind: operator}
  check:
    script: room_brightness        # a gate check, for example one of the project's own
    window_s: 6                    # an argument for every level
    per_level:                     # every level, with null bounds until calibrated
      lights_on: {min: 95, max: 160}
      lights_dimmed: {min: 20, max: 70}

surface:
  levels: [tiles, carpet]
  default: tiles
  metadata_key: surface
  maps_to: {kind: operator}
  unchecked: "No sensor of this setup can tell the two floors apart."
```

`per_level` must name every level. A bound that is `null` fails the check, so a band nobody has measured yet blocks the trial instead of passing it. A factor check that does not pass always blocks: the gate's override never passes it.

!!! note "Silent errors"
    A counted trial whose realized condition differs from the declared one is a silent error. It can happen only in a factor that no check covers. Rule L13 makes every such factor visible in its declaration.

The values above are only an example. The check scripts and their arguments belong to your project (see [[own-gate-checks|Write your own gate checks]]).

## More fields of a factor

- `describe`: what the factor means and why it matters.
- `aliases`: older spellings of a level, so that trials recorded with them still match their cell.
- `blocked`: levels that no campaign may ask for yet, each with the reason. Every command that loads a campaign refuses such a level, but trials recorded at it still match.
- `domain`: levels that decide whether a trial is a simulation or a hardware trial, when `runner.yaml`'s `simulation:` alone cannot say.

[[factors-yaml|factors.yaml]] lists every field and its rules.

## Override a factor from the command line

`trials plan`, `trials check` and `trials run` take `--set`. One level pins the factor, and a comma-separated list sweeps it:

```bash
trials plan experiments/my_experiment/campaigns/baseline.yaml --set rate=fast
trials plan experiments/my_experiment/campaigns/baseline.yaml --set rate=slow,fast
```

The level must be one the factor declares. `trials check --factors <experiment>` prints the whole factor space of an experiment.
