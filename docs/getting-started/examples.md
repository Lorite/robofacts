# The examples

The repository holds three example projects. Each one is a complete project with its own `trials.yaml`, and each shows a different way to connect a system to RoboFACTS.

| Example | Backend | What it does | Tested |
| --- | --- | --- | --- |
| `robofacts/example_project` | `process`, no ROS | a script stands in for the system and writes a log file | runs in CI |
| `robofacts_ros2/example_project` | `ros2` | the ROS 2 demo talker is the "robot", and each trial records its topic | runs in CI and in the Docker image |
| `examples/cflib_hover` | `process`, no ROS | one Crazyflie hovers on its Flow deck, commanded with cflib | its tests run in CI without a drone |

## The log recorder, without any middleware

`robofacts/example_project` has one experiment, `demo_log_recorder`, on the built-in `process` [[backends|backend]]. Its stack is a small script, `system.py`, that appends a line to a log file many times per second. The recorder stores what the script writes during a trial, the action `run_trial.py` lets the system run for a while, and a verify step reads the recording. Nothing needs a robot or any middleware.

Its campaign `grid` sweeps two [[factors|Factors]], `rate` (a property of the system) and `duration` (a property of the procedure), so the coverage index shows a grid of four cells.

```bash
cd robofacts/example_project
trials lint
trials plan experiments/demo_log_recorder/campaigns/grid.yaml
trials run  experiments/demo_log_recorder/campaigns/grid.yaml --limit 1             # dry run
trials run  experiments/demo_log_recorder/campaigns/grid.yaml --execute --no-pause  # the four trials
trials index --experiment demo_log_recorder --summary
```

`robofacts/quickstart.sh` runs these steps in order (see [[quickstart|Quickstart]]). The tests of the general package also run against this project.

## The talker demo, on the ROS 2 backend

`robofacts_ros2/example_project` has one experiment, `demo_talker_listener`, on the `ros2` backend. Its stack is `ros2 run demo_nodes_cpp talker`. Each trial records `/chatter` and `/rosout` into a bag, the action listens to `/chatter` for a few seconds, and a verify step counts the messages in the bag. Its campaign `rates` sweeps one factor, `chatter_rate`, with two repetitions, so it wants four trials. It needs ROS 2 Humble, but no robot.

```bash
cd robofacts_ros2/example_project
trials lint
trials plan experiments/demo_talker_listener/campaigns/rates.yaml
trials run  experiments/demo_talker_listener/campaigns/rates.yaml --limit 1             # dry run
trials run  experiments/demo_talker_listener/campaigns/rates.yaml --execute --no-pause  # the four trials
trials index --experiment demo_talker_listener --summary
```

The top-level `quickstart.sh` and the Docker image run these steps. [[talker-walkthrough|Walkthrough: the talker demo]] explains each one.

## The Crazyflie hover, without middleware

`examples/cflib_hover` flies a real robot on the `process` backend. One Crazyflie hovers for 10 s on its Flow deck, commanded with [cflib](https://github.com/bitcraze/crazyflie-lib-python). The experiment has no stack: the action `cf_hover.py` holds the radio link for the whole flight and writes a flight log, and the recorder stores that log.

- **The gate** checks the stop latch, the battery and the Flow deck over the radio, and the recording directory, with two operator confirmations. The battery and Flow deck checks are the project's own, in `experiments/common/preflight/checks/` (see [[own-gate-checks|Write your own gate checks]]).
- **The reset** before every trial is a power cycle of the drone over the radio, declared in `experiments/common/config/robots.yaml`.
- **The action** refuses to arm on a fresh stop latch, a missing Flow deck, a low battery, an estimator that does not settle, or a ground position away from the origin. It lands at the end of the hover, on a signal, on the stop latch, at the geofence and on any error.
- **The verify step** reads the recorded height: the drone was airborne for at least 3 s, the recording ends on the ground, and the flight log holds no emergency stop.

Without a drone, the tests replace the radio with a fake drone:

```bash
pip install ./robofacts pytest            # from the repository root
cd examples/cflib_hover
trials lint
trials run experiments/cflib_hover/campaigns/baseline.yaml --limit 1     # dry run
python -m pytest -q test
```

!!! warning "A real drone takes off"
    With a drone, `pip install cflib`, plug in a Crazyradio, set the drone's address in `config/drone.yaml` (or `export CF_URI=...`), and follow the operator procedure in `experiments/cflib_hover/README.md`. Keep the landing command typed in a second terminal (`trials tool estop.sh --soft`), and stay within reach of the drone. The example has flown one 10 s hover on hardware (gate GO, verify PASS). Its soft stop and its emergency stop are not yet tested on a drone. See [[stopping-robots|Stop robots]].

The experiment is `simulation: false`, so each trial waits after its gate until you type `fly` (see [[runner|The runner]]).

## Which example to copy

- A system that is a set of processes and log files: start from the log recorder, or from `trials init` (see [[first-project|Your first project]]).
- A ROS 2 system: start from the talker demo, or from `trials init --backend ros2`.
- A robot with its own Python library and no middleware: read the Crazyflie hover. It shows a gate over the robot's own link, a reset, and a verify step on the robot's log.
