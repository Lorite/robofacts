# RoboFACTS

**RoboFACTS** stands for Factors, A Contract, and checked Trials in Simulation and on hardware, for robot teams. This repository holds two packages. `robofacts` is the general one: Python only, no ROS. `robofacts_ros2` is its ROS 2 backend and depends on it. Until 2026-10-16 the package was called `declarative_robot_trials`, and until 2026-11-24 the two packages were one ROS 2 package: same command.

Declarative experiment design for robot trials. An experiment declares five files (the factors, the metadata contract, the T-0 gate, the runner and the analyses), a campaign declares what set of trials under what conditions, and one command runs the loop: a linter refuses a declaration that cannot be measured, a planner lists the cells still missing, a gate of machine checks and operator confirmations runs seconds before every trial, a runner brings the stack up, records the trial and verifies the recording, a coverage index counts only the trials whose metadata satisfies the contract, and named analyses run over chosen cohorts. The same declaration drives one robot or a team, in simulation or on hardware, on one machine or across several.

| Package | Holds | Needs |
|---|---|---|
| `robofacts/` | the declarations (factors, contract, campaigns), the planner, the linter, the coverage index, the trial loop of the runner, the gate protocol with 8 general checks, the web interface, the launcher and recorder interfaces, and a built-in backend without any middleware | Python 3.10 and PyYAML. Installs with `pip`. The web interface needs Flask: `pip install './robofacts[ui]'` |
| `robofacts_ros2/` | the ROS 2 backend: `ros2 launch`, `ros2 bag record`, the bag readers and the slim copy, 13 ROS 2 gate checks, the robot adapters, Explore, and the ROS 2 part of the web interface (Foxglove replay and layouts, the bag download, the clean slate of the rig hosts) | `robofacts` and ROS 2 Humble. An `ament_python` package, built with colcon |

The dependency goes one way. `robofacts/test/test_core_is_free_of_ros.py` fails the build when the general package imports a ROS module or names ROS in any file.

## Quickstart: the whole loop in one command

Without ROS, with Python 3.10 or newer:

```bash
pip install ./robofacts
./robofacts/quickstart.sh
```

`robofacts/quickstart.sh` runs, on the example project `robofacts/example_project`, the linter, the planner, a dry run, the four trials of a campaign that sweeps two factors (gate, trial, recording, verify step, metadata) and the coverage index, which then lists the four cells with one usable trial each. The example's "system under test" is a script that appends lines to a log file, the recorder stores what it writes during a trial, and nothing needs a robot or a middleware.

With ROS 2 and Docker, from this directory:

```bash
docker build -t trials-quickstart -f docker/Dockerfile .
docker run --rm trials-quickstart
```

The image is the official ROS 2 Humble image plus the packages of the example. `quickstart.sh` runs, on the example project, the linter, the planner, a dry run, the four trials of the example campaign (gate, trial, recording, verify steps, metadata) and the coverage index, which then lists the four new usable trials. The example's "robot" is `demo_nodes_cpp talker`, so it runs on any machine with ROS 2 Humble.

## Install

Into a colcon workspace:

```bash
cd ~/ros2_ws/src && git clone <this repository> robofacts
cd ~/ros2_ws && colcon build --packages-select robofacts robofacts_ros2 && source install/setup.bash
trials --help                          # or: ros2 run robofacts_ros2 trials --help
```

colcon builds `robofacts` as a plain Python package (it has no `package.xml`) and orders it before `robofacts_ros2`. After sourcing the workspace, `trials` is on the PATH, and `ros2 run robofacts_ros2 trials` starts the same command.

Without ROS, the general package alone, into any Python 3.10+ environment:

```bash
pip install ./robofacts                # from a clone
trials --help
```

With pip and ROS 2 (an environment that can see your ROS 2 installation): `pip install ./robofacts ./robofacts_ros2`.

A project names its backend in `trials.yaml` (`backend: ros2`). Everything works without ROS on the built-in `process` backend. The ROS 2 launcher, the ROS 2 checks and the bag tools need a sourced ROS 2 Humble (`ros2`, `rosbag2`).

## Your own project in five steps

```bash
trials init my_project --experiment my_experiment     # trials.yaml, the shared config, one experiment
cd my_project
trials lint                                            # every declaration against the rules
trials plan experiments/my_experiment/campaigns/baseline.yaml
trials run  experiments/my_experiment/campaigns/baseline.yaml --limit 1    # dry run: the exact commands
trials run  experiments/my_experiment/campaigns/baseline.yaml --execute    # the trials
```

`trials init` takes `--backend ros2` for a ROS 2 project (the default is the built-in `process` backend). `trials new <name>` writes a commented experiment that works as generated: on the ROS 2 backend it listens to `demo_nodes_cpp talker`, on the `process` backend a small script stands in for the system and writes a log file. Edit, in this order: `config/factors.yaml` (the axes and how the runner realizes each level), `config/trial_metadata_schema.yaml` (the fields every trial records, with the coverage axes), `config/preflight_checklist.json` (the gate), `config/runner.yaml` (the stack, one trial, the verify steps, the computers) and `campaigns/baseline.yaml` (the sweep, the fixed levels, the repetitions). The linter and the planner then check the declarations before any trial runs.

**When a similar experiment exists, start from it instead.** `trials new <name> --from <existing> [--campaign <c>[:<new_name>]]` copies that experiment's declarations (`config/`, the campaigns, `scripts/`, the runbook and the other text files) with its name replaced, and leaves out its bags, runs, results, notebooks, tests and large files. The README becomes a short stub that names the source. Change only what differs, then ask one question:

```bash
trials new my_variant --from my_experiment --campaign baseline:my_claim   # copy one campaign, renamed
trials validate experiments/my_variant      # check, plan, gate, lint, its own tests, a dry run per host: READY or NOT READY
```

`trials validate` takes an experiment or one campaign file, prints one line per stage with its time, shows the output of a failed stage, and exits 0 only when every stage passed. It runs in seconds because every stage is scoped to that experiment. The full test suite of a project is for CI.

## Where the experiments are

Every command resolves the experiments root in this order: the `--experiments-root` flag, the `TRIALS_EXPERIMENTS_ROOT` environment variable, a `trials.yaml` found by walking up from the current directory (`experiments_root:` and `project_root:` keys, relative to that file), or the nearest `experiments/` directory. The project root (the working directory of the runner's commands) is `--project-root`, `TRIALS_PROJECT_ROOT`, `trials.yaml`, or the parent of the experiments root. `trials roots` prints the resolution.

## Project settings in `trials.yaml`

Besides the two roots, `trials.yaml` may hold the names a project gives to what used to be constants in the code, so that the package itself knows no robot: `stop_latch` (the file every stop command writes first, which the gate's `stop_latch` check refuses while it is younger than 120 s, default `/tmp/trial_stop_latch`) and `record_profiles` (the names, such as the drone or the camera namespace, that fill the placeholders of the project's record profiles. The core has no defaults, and an enabled robot adapter brings its own, `cf1` for the Crazyflie and `spot` for Spot). `adapters`, `parallel_safe_checks` and `stop_patterns` are described under "Robot adapters" below. An experiment's `runner.yaml` may still say `cf_name:` for its own drone. The environment variables the package reads all start with `TRIALS_`: `TRIALS_EXPERIMENTS_ROOT`, `TRIALS_PROJECT_ROOT`, `TRIALS_STOP_LATCH`, `TRIALS_PREFLIGHT_CONCURRENCY`, `TRIALS_PREFLIGHT_TIMING_LOG`, `TRIALS_VOICE`, `TRIALS_VOICE_HOST`, `TRIALS_VOICE_PORT`, `TRIALS_ROBOTS_CONFIG`. Two older names are the exception: `RIG_HOST` (read by the host registry and by `estop.sh`) and `RIG_CONTAINER` (read by `estop.sh`). They stay because operators type them during a stop.

## The backend: how the stack starts and how a trial is recorded

The planner, the linter, the gate protocol, the trial loop and the coverage index do not depend on the middleware of the system under test. Two things do: starting the stack and recording the trial. A backend supplies both as two interfaces that return command lines, a launcher and a recorder (`robofacts/backend.py`), and the runner keeps all process handling. `trials.yaml` names the backend:

```yaml
backend: process    # built in: a shell command, and a recorder that stores what the system appends to its log files
backend: ros2       # the package robofacts_ros2: ros2 launch or ros2 run, ros2 bag record, a recording is a bag
```

A project that names none runs on `process`, and a name loads the package `robofacts_<name>`. `$TRIALS_BACKEND` overrides the file for one session. A `runner.yaml` with `launch.package` and `launch.file` is refused on the `process` backend with the line to add, so a ROS 2 project that forgot the key cannot run a trial without its stack. A backend also ships the gate checks of its middleware, their spoken sentences, its robot adapters, its lint rules, its experiment template and its extra commands (for ROS 2: `trials explore`, `trials slim`). It may also extend the web interface (`trials ui`, part of the general package): pages and routes of its own, and a `_viewers.html` template that replaces the general one, so the ROS 2 backend adds the Foxglove links, the Viewing page, the bag download and the Kill ROS button. On the `process` backend the interface shows the plan, the coverage grid, the trials, the gate and its confirmations, the runs and the journal, without any of these. On the `process` backend, `runner.yaml` declares `launch: {command: "..."}` (templated like the action, optional) and `record_sources:` (the files to follow), and a recording is a directory with `data/` and a `recording.json` written last. A command may name the trial's recording directory as `{recording}` or, as before, `{bag}`. The folders keep their names on every backend: `bags/`, `bags_issues/`, `bags_slim/`, `bags_scratch/`.

## Where a trial runs

The `execution_host` factor names a host level for each trial (for example `laptop` or `rig`). `<experiments root>/common/config/hosts.yaml` declares once how to reach each level: `local: true`, or an ssh `host` alias with its `container`, `container_hostname`, `shell` and setup files. An experiment's `runner.yaml` lists under `hosts:` the levels it may run on. A level with no value takes the registry's settings, and a level with keys replaces only those keys, for example `env_setup` for an overlay. A trial on a level the experiment does not declare is refused, where it used to run on the runner's own machine without a word. `RIG_HOST=<alias>` replaces the ssh alias of the registry's `rig:` level for one session, for example a VPN name when you work remotely. A project without `hosts.yaml` writes each level's full settings in `runner.yaml`, as before. Code: `robofacts/host_registry.py`.

The stop tools use the same registry. A robot in `common/config/robots.yaml` names a `host_level`, and `estop.sh` and `robot_action.sh` resolve it through `hosts.yaml` next to that file. On the machine where a hardware trial runs, the runner notes the trial's `execution_host`, its experiment's `robots:` and whether it is a simulation in its run marker, and the stop then goes to the trial's host for those robots (every robot when the experiment declares none). A simulation never moves a stop. `--host-level <level>` overrides both for one call, `estop.sh --list` prints each robot's host and the reason, and the runner prints the targets at the start of every hardware trial. A robot without `host_level` keeps its own `host`, `container` and `shell`. Inside a container, a stop runs directly only when that container is the target's own, recognized by the level's `container_hostname`; from any other container it goes over ssh. `estop.sh --list` prints the route from where it runs.

## Robot adapters

Neither package names a robot outside the adapters. What only makes sense for one robot family is a robot adapter of the ROS 2 package, under `robofacts_ros2/robofacts_ros2/adapters/`, and a project enables the ones it uses in `trials.yaml`:

```yaml
adapters: [crazyflie, spot, px4]
```

| Adapter | Gate checks | Default names | Other |
|---|---|---|---|
| `crazyflie` | `battery`, `crazyflie_radio_battery`, `drone_armable`, `ekf_convergence` | `cf_name: cf1` | verify steps `verify_flight_happened`, `verify_ext_pose_age`, the analysis windows of `rosout_trim_utils` |
| `spot` | `graph_nav_localized` | `spot_name: spot` | the lease table of an action that echoes `<robot>/status/leases` in the web interface |
| `px4` | `px4_preflight_pass` | | |

An adapter's checks are found after the project's own checks and before the backend's and the core ones, its names fill record-profile placeholders under the project's own, and its helpers are modules such as `robofacts_ros2.adapters.crazyflie.verify_flight_happened`. Nothing of a disabled adapter is in force. A project's own checks go in `experiments/common/preflight/checks/`, and the ones that are read-only probes may share a gate batch when `trials.yaml` lists them in `parallel_safe_checks:`.

The stop sweeps match the processes of the project's workspace. What only the project knows goes in `trials.yaml` under `stop_patterns:`: `recorder_scripts` (wrappers that exec `ros2 bag record`), `owned_containers` (composable containers its launches start, matched by node name so an orphan is found), and `kinds` (extra named patterns, regex templates with `{workspace}`).

A slim copy of a bag drops the image, depth and point-cloud topics (`trials slim`, and the runner after each trial). The core's rules cover the common names (`image_raw`, `compressed`, `compressedDepth`, `depth_registered`, `points`, ...) and keep every `camera_info`. A project adds its own in `trials.yaml`: `slim_topics: {heavy_patterns: [/my_lidar/points_raw], light_suffixes: [/debug/overlay/compressed]}`, where a heavy pattern is a substring of the topic name and a light suffix keeps a topic that a heavy pattern would drop.

No package names a robot or a lab outside the adapters. `robofacts_ros2/test/test_packages_name_no_robot_and_no_lab.py` fails the build when a file of either package names a robot family or a lab (a short allowlist covers facts about third-party tools), so robot code goes into an adapter and a lab's names into its project.

## Record profiles

A record profile is a named topic list that a trial records. The project declares its profiles in `experiments/common/config/record_profiles.yaml`, and the package ships none: a topic set describes one project's robots, drivers and nodes, so it belongs to the project. The file has reusable `blocks` (topic lists) and `profiles` (a list of blocks, optional extra `topics`, and an optional `size` note). A topic may hold `{name}` placeholders, filled from the `record_profiles:` names in `trials.yaml`. `trials init` writes a commented template.

```yaml
blocks:
  robot_state: ["/{cf_name}/pose", "/{cf_name}/battery", /tf, /tf_static, /rosout]
  camera_debug: ["{camera}/color/camera_info"]
profiles:
  flight: {blocks: [robot_state], size: "~5 MB/min"}
  flight_with_camera: {blocks: [robot_state, camera_debug], topics: [/joy]}
```

A trial picks its profile with a factor whose `maps_to` has `kind: record_profile`. Without such a factor the runner uses `default_record_profile` from `trials.yaml`, and without that it refuses the trial rather than record a guess. An experiment whose topics are its own lists them in `runner.yaml` as `record_topics`, which wins over any profile. The lint checks that every profile is a level of `record_profile` in the metadata contract (L9) and records `/rosout` (L11). `trials profiles --list` prints the profiles in force and the files they came from, and `trials profiles <name> --topics-only` prints one profile's topics.

## The commands

| command | does |
|---|---|
| `trials plan <campaign>` | what the campaign still needs, against the recorded usable trials |
| `trials run <campaign>` | a dry run of the next trials, `--execute` runs them, `--scratch` one diagnostic run |
| `trials explore <campaign>` | one condition's stack, live in Foxglove, with the parameters `runner.yaml` allows writable. No gate and never coverage. At stop it writes `parameter_changes.yaml`, and it opens the `explore.viewers` windows (a simulator GUI). Its run page in `trials ui` opens Foxglove by itself, runs the `explore.actions` as buttons, shows the parameter changes live with a set back button, records scratch recordings to `bags_scratch/` (with an Analyze button), writes notes to `notes.md`, has a jog pad (`explore.jog`), says where a changed value is declared (`proposed_changes.md`, nothing applied) and restarts with another cell. A dry run without `--execute` |
| `trials index` | the coverage index, `--condition-space` one condition across experiments |
| `trials validate <exp \| campaign>` | is it ready for a first trial: check, plan, gate, the lint and the tests of that experiment, a dry run per host. One verdict |
| `trials lint` | every declaration against the rules (pytest), `--experiment <exp>` for one experiment |
| `trials gate --checklist <json>` | a T-0 gate by hand, `--validate` and `--list` for the checklist |
| `trials analyze <exp> <name>` | a named analysis over a cohort of usable bags |
| `trials records <exp>` | every trial with its campaign, gate verdict, slim bag, run log, analyses and video; `--campaign`, `--status`, `--where`, `--json`, `--posters` to make the stills of the UI's card view ahead of time, and `--stills` to make the stills and the camera video of the camera an experiment declares in `config/trial_media.yaml` |
| `trials exclude <exp> <trial> --reason … --category …` | a person's decision that a trial must not count: moves the bag and its slim copy to `bags_issues*` and writes `exclusion.json` beside each (date, by, category, reason, evidence). Categories: `infrastructure`, `operator`, `estimator`, `other`. Refuses a trial the runner may still be writing. On a trial already in `bags_issues/` without a reason, it adds the reason in place |
| `trials restore <exp> <trial>` | the reverse: moves an excluded trial back and keeps its `exclusion.json` as `exclusion_restored_<time>.json` |
| `trials amend <exp> <trial> <factor>=<level> --reason …` | a person's correction of a recorded level (an operator level is not checked at T-0, so the recorded value follows the plan): an entry in `corrections.json` beside the bag and its slim copy, with the recorded value, the corrected one, who, when and why. The level must be one the experiment declares. `trial_metadata.json` is never edited |
| `trials reverify <exp> <trial> --reason … [--dry-run]` | runs the trial's CURRENT verify steps again after a rule was fixed, every step, on the full bag or else its slim copy, and writes `reverification.json` beside the bag and its slim copy (steps, return codes, output tails, code version, who, when, why). The index lifts the abort only when it came from a verify step alone and every step passes now. A failing re-verify never excludes a trial. Run it on the host that recorded the trial, inside its dev container |
| `trials check <campaign>` | the expanded plan of one campaign, `--factors <exp>` the factor space |
| `trials metadata` | audit the recorded metadata against the contract |
| `trials ui` | the web interface (`EXPERIMENTS_UI_PASSWORD` required, `EXPERIMENTS_UI_USER` optional: when set, the user name must match too). Every trials list is a table or, with the switch above it, cards with a still from each trial's video that play it in place (ffmpeg makes the stills on first view, cached in `runs/posters/`). A card's picture can show several things, one tag each above it: the trial video, the declared camera's stills (hover to scrub, the play button plays the camera video, labeled with the camera's name) and up to four result plots |
| `trials ui --view-only` | the same interface as an archive viewer: the routes that run a trial, stop a robot, kill ROS, or exclude or restore a trial are not registered (they answer 404) and the pages show no control for them. `--cache-dir` keeps the poster stills out of a read-only experiments tree. A `.archive_status.json` at the top of the experiments root, when present, fills the banner with the time of the last merge and of each host's last sync. Also `TRIALS_UI_VIEW_ONLY=1`. A password is still required |
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
| step trace, failure report | what each step of an attempt ran, how long, how it ended and a failing step's output tail; for an attempt that did not run, its first failing step, the evidence and empty findings | `trace` in the ledger line; `runs/<trial>.failure.md` |
| gate | machine checks and operator confirmations, run seconds before every trial (the T-0 gate) | `config/preflight_checklist.json`, `scripts/preflight.sh`, `trials gate`; every verdict in `runs/preflight_gate.jsonl`, the gate log |
| verify steps | checks on the recording after the trial; a failure aborts it. Every experiment declares at least one in `runner.yaml`, or a written reason why it has none (lint L6), because an action can exit 0 without the trial having happened | `verify_steps:` in `runner.yaml` or in a campaign; `no_verify_steps: <reason>` in `runner.yaml` |
| robot reset, pre-launch check, spoken reason | a robot's steps before every trial (a power cycle), checks before the stack starts (a battery read over the radio), and the NO-GO said aloud (2026-11-04) | `reset:` per robot in `common/config/robots.yaml` with `robots:` in `runner.yaml` (`robot_reset: false` in a campaign opts out); `"stage": "pre_launch"` on a checklist entry; `"speak"` on an entry or `gate_speech.py`'s built-in sentence |
| first and repeat trials, setup steps | within ONE runner invocation, the first trial and the trials after it, which may need different operator steps and a reset between them (2026-11-04) | `when: first \| repeat \| always` on `operator_steps` and `setup_steps`; `setup_steps:` in `runner.yaml` and in a campaign (the campaign's run after the runner's, before the launch); `step_set` in the ledger |
| class | usable (counts), unverified (breaks the contract) or excluded (aborted, quarantined, no data) | `Trial.status` in `trial_index`; `trials records` adds the reason of an excluded trial and the outcome of an attempt that left no trial (blocked, failed, interrupted) |
| decision (exclusion, correction) | what a person decides about a recorded trial after the fact, with a reason: it must not count (quarantined), or a recorded level was wrong | `exclusion.json` and `corrections.json` beside the bag, from `trials exclude`, `trials restore` and `trials amend`, and `reverification.json` from `trials reverify` (a verify rule was wrong); never an edit of `trial_metadata.json` |
| coverage, coverage index | the usable trials per cell | `trials index`; `trials plan` prints wanted, covered and still missing per campaign |
| silent error | a counted trial whose realized condition differs from the declared one; possible only in a factor no check covers | what the linter's rule L13 and the factor `check:` exist to prevent |
| domain | simulation or hardware, recorded per trial | `domain` in `trial_metadata.json`, from `simulation:` in `runner.yaml` |
| reduced bag | the recording without images, a few MB, copied back from the rig | the slim copy, `bags_slim/<trial>_slim/` |
| scratch recording | a diagnostic trial through the campaign's stack and gate, never coverage | `trials run … --scratch`, `bags_scratch/` |
| exploration | one condition's stack brought up live to debug and tune it, with a Foxglove bridge that may change parameters, never a trial and never coverage | `trials explore`, the 🔭 Explore buttons, `runs/explore/<stamp>_explore/parameter_changes.yaml` and `notes.md`, `explore:` in `runner.yaml` (`param_whitelist`, `viewers`, `actions`), recordings in `bags_scratch/<stamp>_explore_<label>` |
| named analysis | a declared computation over a cohort of usable trials, with the exact bag list | `config/analyses.yaml`, `trials analyze`, `analysis_metadata.json` |

## A person's decision about a trial

The runner writes `trial_metadata.json` once, and nobody edits it afterwards. When a person later knows better, the decision is a file beside it, with a reason, and every reader applies it:

- **It must not count.** `trials exclude <exp> <trial> --reason "…" --category infrastructure` moves the bag and its slim copy into `bags_issues*` and writes `exclusion.json` beside each. `trials records`, the index and the UI show the category and the reason (`excluded (quarantined)`, then `why: infrastructure: …`). `trials restore` moves it back and keeps the old file as history.
- **A recorded level is wrong.** `trials amend <exp> <trial> route=building_loop --reason "…"` writes `corrections.json` beside the bag and its slim copy. `trial_index.load_trials` reads the corrected value on top of the recorded one (`Trial.metadata` is corrected, `Trial.recorded` is the file as written), so the plan counts the trial in its true cell. `trials plan` and `trials index` name the corrected trials, `trials records` prints a `corrected:` line, and the UI shows a corrected badge and a table of recorded and corrected values.

Who decided comes from `--by`, else `$TRIALS_BY`, else the login name. A slim copy made later (`trials slim`) copies both files, so the copy counts exactly as its bag does.

## A project's own gate checks

A check is a bash script that prints one `PASS:`, `FAIL:` or `SKIP:` line and exits 0, 1 or 3 (see `robofacts/robofacts/preflight/README.md`). The general package ships 8 checks that need no middleware (disk, memory, GPU memory, a file, a host, a network interface, the stop latch, the recording directory). The ROS 2 package ships 13 more (topic rates and values, nodes, parameters, services, DDS buffers, the clean slate, motion capture, the simulator's world, see `robofacts_ros2/robofacts_ros2/checks/README.md`), and each robot adapter its own. A project adds its own under `<experiments root>/common/preflight/checks/<name>.sh`; a project check with the same name as a packaged one wins. The helper the packaged checks source is passed as `$PF_LIB`.

## Development

```bash
cd robofacts && python3 -m pytest -q test                      # the general package alone: needs no ROS
python3 -m pytest -q robofacts_ros2/test robofacts/test        # both packages, with both on the Python path
./quickstart.sh                                                # with ROS 2 Humble sourced and both packages installed
```

`robofacts/test` runs with the general package alone. Its tests that exercise the core together with the ROS 2 backend are marked `ros2` and are skipped where `robofacts_ros2` is not installed. `robofacts/test` runs against `robofacts/example_project/`, and `robofacts_ros2/test` against `robofacts_ros2/example_project/`. `.github/workflows/ci.yaml` has three jobs: `core-no-ros` installs only `robofacts` on a plain Python image (`python:3.10-slim`, where the tests that need git are skipped), runs its tests, runs the example with `robofacts/quickstart.sh` from plan to coverage index, and does the same with a generated project. `colcon-humble` builds both packages on `ros:humble`, runs both test suites and the example campaign. `docker-image` builds the quickstart image.

## License

Apache-2.0, see `LICENSE`.
