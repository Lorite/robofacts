# RoboFACTS

**RoboFACTS** stands for Factors, A Contract, and checked Trials in Simulation and on hardware, for robot teams. The ROS 2 package and the Python module are `robofacts`. Until 2026-10-16 the package was called `declarative_robot_trials`: same code, same command.

Declarative experiment design for ROS 2 trials. An experiment declares five files (the factors, the metadata contract, the T-0 gate, the runner and the analyses), a campaign declares what set of trials under what conditions, and one command runs the loop: a linter refuses a declaration that cannot be measured, a planner lists the cells still missing, a gate of machine checks and operator confirmations runs seconds before every trial, a runner brings the stack up, records the bag and verifies it, a coverage index counts only the trials whose metadata satisfies the contract, and named analyses run over chosen cohorts. The same declaration drives one robot or a team, in simulation or on hardware, on one machine or across several.

The package is a plain `ament_python` ROS 2 package (Humble) that also installs with `pip`.

## Quickstart: the whole loop in one command

With Docker, from this directory:

```bash
docker build -t trials-quickstart -f docker/Dockerfile .
docker run --rm trials-quickstart
```

The image is the official ROS 2 Humble image plus the packages of the example. `quickstart.sh` runs, on the example project, the linter, the planner, a dry run, the four trials of the example campaign (gate, trial, recording, verify steps, metadata) and the coverage index, which then lists the four new usable trials. The example's "robot" is `demo_nodes_cpp talker`, so it runs on any machine with ROS 2 Humble.

## Install

Into a colcon workspace:

```bash
cd ~/ros2_ws/src && git clone <this repository> robofacts
cd ~/ros2_ws && rosdep install --from-paths src --ignore-src -y
colcon build --packages-select robofacts && source install/setup.bash
ros2 run robofacts trials --help
```

Or with pip, into any Python 3.10+ environment that can see your ROS 2 installation:

```bash
pip install git+<this repository>      # or: pip install . from a clone
trials --help
```

A colcon install puts the command in `lib/robofacts/`, so it is `ros2 run robofacts trials …` there (or an alias: `alias trials='ros2 run robofacts trials'`). A pip install puts `trials` on the PATH. The runner, the gate's topic checks and the bag tools need a sourced ROS 2 Humble (`ros2`, `rosbag2`). The linter, the planner, the coverage index and the scaffolding work without ROS.

## Your own project in five steps

```bash
trials init my_project --experiment my_experiment     # trials.yaml, the shared config, one experiment
cd my_project
trials lint                                            # every declaration against the rules
trials plan experiments/my_experiment/campaigns/baseline.yaml
trials run  experiments/my_experiment/campaigns/baseline.yaml --limit 1    # dry run: the exact commands
trials run  experiments/my_experiment/campaigns/baseline.yaml --execute    # the trials
```

`trials new <name>` writes a commented experiment that works as generated (it listens to `demo_nodes_cpp talker`). Edit, in this order: `config/factors.yaml` (the axes and how the runner realizes each level), `config/trial_metadata_schema.yaml` (the fields every trial records, with the coverage axes), `config/preflight_checklist.json` (the gate), `config/runner.yaml` (the stack, one trial, the verify steps, the computers) and `campaigns/baseline.yaml` (the sweep, the fixed levels, the repetitions). The linter and the planner then check the declarations before any trial runs.

**When a similar experiment exists, start from it instead.** `trials new <name> --from <existing> [--campaign <c>[:<new_name>]]` copies that experiment's declarations (`config/`, the campaigns, `scripts/`, the runbook and the other text files) with its name replaced, and leaves out its bags, runs, results, notebooks, tests and large files. The README becomes a short stub that names the source. Change only what differs, then ask one question:

```bash
trials new my_variant --from my_experiment --campaign baseline:my_claim   # copy one campaign, renamed
trials validate experiments/my_variant      # check, plan, gate, lint, its own tests, a dry run per host: READY or NOT READY
```

`trials validate` takes an experiment or one campaign file, prints one line per stage with its time, shows the output of a failed stage, and exits 0 only when every stage passed. It runs in seconds because every stage is scoped to that experiment. The full test suite of a project is for CI.

## Where the experiments are

Every command resolves the experiments root in this order: the `--experiments-root` flag, the `TRIALS_EXPERIMENTS_ROOT` environment variable, a `trials.yaml` found by walking up from the current directory (`experiments_root:` and `project_root:` keys, relative to that file), or the nearest `experiments/` directory. The project root (the working directory of the runner's commands) is `--project-root`, `TRIALS_PROJECT_ROOT`, `trials.yaml`, or the parent of the experiments root. `trials roots` prints the resolution.

## Project settings in `trials.yaml`

Besides the two roots, `trials.yaml` may hold the names a project gives to what used to be constants in the code, so that the package itself knows no robot: `stop_latch` (the file every stop command writes first, which the gate's `stop_latch` check refuses while it is younger than 120 s, default `/tmp/trial_stop_latch`) and `record_profiles` (the drone, quadruped, camera and node names the named record profiles build their topic lists from, defaults `cf1`, `spot`, `/camera`, `/estimator`, `/estimator_bridge`, `/localization_watchdog`). An experiment's `runner.yaml` may still say `cf_name:` for its own drone. The environment variables the package reads all start with `TRIALS_`: `TRIALS_EXPERIMENTS_ROOT`, `TRIALS_PROJECT_ROOT`, `TRIALS_STOP_LATCH`, `TRIALS_PREFLIGHT_CONCURRENCY`, `TRIALS_PREFLIGHT_TIMING_LOG`, `TRIALS_VOICE`, `TRIALS_VOICE_HOST`, `TRIALS_VOICE_PORT`, `TRIALS_ROBOTS_CONFIG`.

## The commands

| command | does |
|---|---|
| `trials plan <campaign>` | what the campaign still needs, against the recorded usable trials |
| `trials run <campaign>` | a dry run of the next trials, `--execute` runs them, `--scratch` one diagnostic run |
| `trials explore <campaign>` | one condition's stack, live in Foxglove, with the parameters `runner.yaml` allows writable. No gate, no recording, no action, so never coverage. At stop it writes `parameter_changes.yaml`. A dry run without `--execute` |
| `trials index` | the coverage index, `--condition-space` one condition across experiments |
| `trials validate <exp \| campaign>` | is it ready for a first trial: check, plan, gate, the lint and the tests of that experiment, a dry run per host. One verdict |
| `trials lint` | every declaration against the rules (pytest), `--experiment <exp>` for one experiment |
| `trials gate --checklist <json>` | a T-0 gate by hand, `--validate` and `--list` for the checklist |
| `trials analyze <exp> <name>` | a named analysis over a cohort of usable bags |
| `trials records <exp>` | every trial with its campaign, gate verdict, slim bag, run log, analyses and video; `--campaign`, `--status`, `--where`, `--json` |
| `trials check <campaign>` | the expanded plan of one campaign, `--factors <exp>` the factor space |
| `trials metadata` | audit the recorded metadata against the contract |
| `trials ui` | the web interface (`EXPERIMENTS_UI_PASSWORD` required) |
| `trials new`, `trials init` | the scaffolding, `trials new <name> --from <exp>` copies a similar experiment |
| `trials tool <name>` | the shell tools: `kill_ros.sh` (clean slate), `estop.sh`, `robot_action.sh` |

## The words

The package uses the terms of the design of experiments, as its paper does. Where a file or a key carries an older name, the table says so; the names of files and keys stay, because projects depend on them.

| Term | Meaning | In the package |
|------|---------|----------------|
| experiment | a physical setup with its software | a folder `experiments/<name>/` |
| factor, level, default | a variable the experiment controls, its values, the value a campaign does not set | `config/factors.yaml` |
| realization | how a level is applied: the computer, a launch argument, a recording profile, an internal value or an operator step | `maps_to: {kind: host \| launch_arg \| record_profile \| internal \| operator}` |
| metadata contract | the fields every trial must record | `common/config/trial_metadata_core.yaml` plus `config/trial_metadata_schema.yaml` |
| coverage axes | the contract fields the coverage index groups by | fields with `factor: true` |
| cell | one combination of levels (a condition) | a row of `trials plan` and of the campaign page |
| campaign | which factors to sweep, which to fix, how many repetitions | `campaigns/<name>.yaml` |
| trial | one run of one cell: a recording (a ROS 2 bag) and a metadata record | `bags/<trial>/` with `trial_metadata.json` |
| attempt | a started trial, whether the gate refused it or not | a line of `runs/attempts.jsonl`, the ledger |
| gate | machine checks and operator confirmations, run seconds before every trial (the T-0 gate) | `config/preflight_checklist.json`, `scripts/preflight.sh`, `trials gate`; every verdict in `runs/preflight_gate.jsonl`, the gate log |
| verify steps | checks on the recording after the trial; a failure aborts it | `verify_steps:` in `runner.yaml` or in a campaign |
| class | usable (counts), unverified (breaks the contract) or excluded (aborted, quarantined, no data) | `Trial.status` in `trial_index`; `trials records` adds the reason of an excluded trial and the outcome of an attempt that left no trial (blocked, failed, interrupted) |
| coverage, coverage index | the usable trials per cell | `trials index`; `trials plan` prints wanted, covered and still missing per campaign |
| silent error | a counted trial whose realized condition differs from the declared one; possible only in a factor no check covers | what the linter's rule L13 and the factor `check:` exist to prevent |
| domain | simulation or hardware, recorded per trial | `domain` in `trial_metadata.json`, from `simulation:` in `runner.yaml` |
| reduced bag | the recording without images, a few MB, copied back from the rig | the slim copy, `bags_slim/<trial>_slim/` |
| scratch recording | a diagnostic trial through the campaign's stack and gate, never coverage | `trials run … --scratch`, `bags_scratch/` |
| exploration | one condition's stack brought up live to debug and tune it, with a Foxglove bridge that may change parameters, never a trial and never coverage | `trials explore`, the 🔭 Explore buttons, `runs/explore/<stamp>_explore/parameter_changes.yaml`, `explore:` in `runner.yaml` |
| named analysis | a declared computation over a cohort of usable trials, with the exact bag list | `config/analyses.yaml`, `trials analyze`, `analysis_metadata.json` |

## A project's own gate checks

A check is a bash script that prints one `PASS:`, `FAIL:` or `SKIP:` line and exits 0, 1 or 3 (see `robofacts/preflight/README.md`). The package ships the general ones (topic rates, node presence, disk, memory, DDS buffers, the stop latch, the bag directory, hosts, GPU memory, and several robot-specific ones). A project adds its own under `<experiments root>/common/preflight/checks/<name>.sh`; a project check with the same name as a packaged one wins. The helper the packaged checks source is passed as `$PF_LIB`.

## Development

```bash
python3 -m pytest -q test           # from this directory; ROS-dependent tests skip without ROS
./quickstart.sh                     # with ROS 2 Humble sourced and the package installed
```

The tests run against `example_project/`. `.github/workflows/ci.yaml` builds the package with colcon on `ros:humble`, runs the tests and the example campaign, installs with pip without ROS, generates a project with `trials init` and checks it, and builds the Docker image.

## License

Apache-2.0, see `LICENSE`.
