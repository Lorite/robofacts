# RoboFACTS

RoboFACTS is a framework for experiments with robots, in simulation and on hardware. You declare an experiment once (its factors, the metadata every trial must record, the checks before each trial, how to run and verify one trial), and the framework plans the trials, runs them, checks each recording, and counts only the trials that satisfy the declaration. The same declaration drives one robot or a team, on one computer or several.

The name stands for Factors, A Contract, and checked Trials in Simulation and on hardware. The framework is described in a SAC 2027 paper (see [Cite](#cite)).

**Documentation: <https://lorite.github.io/robofacts/>**. Start with [Install](https://lorite.github.io/robofacts/getting-started/install/), the [Quickstart](https://lorite.github.io/robofacts/getting-started/quickstart/) and [Your first project](https://lorite.github.io/robofacts/getting-started/first-project/). Before a trial with a real robot, read [Run on hardware](https://lorite.github.io/robofacts/guides/hardware-runs/).

## Two packages

| Package | Holds | Needs |
|---|---|---|
| `robofacts/` | the declarations (factors, contract, campaigns), the planner, the linter, the coverage index, the runner, the gate with 8 general checks, the web interface, and a built-in backend without any middleware (`process`) | Python 3.10, PyYAML and pytest (the linter runs its rules with it). The web interface also needs Flask |
| `robofacts_ros2/` | the ROS 2 backend: `ros2 launch`, `ros2 bag record`, the bag readers, 13 ROS 2 gate checks, the robot adapters, and the ROS 2 part of the web interface | `robofacts` and ROS 2 Humble |

The dependency goes one way. A test fails the build when `robofacts` imports a ROS module or names ROS in any file.

## Install

The general package, from [PyPI](https://pypi.org/project/robofacts/), into any Python 3.10 or newer environment:

```bash
pip install robofacts                 # add [ui] for the web interface: pip install 'robofacts[ui]'
trials --help
```

Both packages, with colcon, into a ROS 2 Humble workspace:

```bash
cd ~/ros2_ws/src && git clone https://github.com/Lorite/robofacts.git
cd ~/ros2_ws && colcon build --packages-select robofacts robofacts_ros2 && source install/setup.bash
trials --help                         # or: ros2 run robofacts_ros2 trials --help
```

The [install page](https://lorite.github.io/robofacts/getting-started/install/) has the other ways (a clone, the extras, rosdep).

## Quickstart

```bash
./robofacts/quickstart.sh                                          # without ROS, after pip install ./robofacts
docker build -t trials-quickstart -f docker/Dockerfile . && docker run --rm trials-quickstart   # with ROS 2, in Docker
```

Each runs the whole loop on an example project: the linter, the planner, a dry run, four trials (gate, trial, recording, verify step, metadata) and the coverage index, which then lists four usable trials.

## Three examples

| Example | Backend | What it does |
|---|---|---|
| `robofacts/example_project` | `process`, no ROS | a small script stands in for the system and writes a log file, the recorder stores it, a verify step reads the recording |
| `robofacts_ros2/example_project` | `ros2` | the ROS 2 demo talker is the "robot", the trial records its topic into a bag |
| `examples/cflib_hover` | `process`, no ROS | one Crazyflie hovers on its Flow deck, commanded with cflib, with a gate over the radio and a verify step on the recorded height |

More on each: [The examples](https://lorite.github.io/robofacts/getting-started/examples/).

## Cite

If you use RoboFACTS, please cite the paper:

> Alejandro Lorite-Mora. RoboFACTS: A Declarative Experiment Design Framework for Multi-Robot Trials in Simulation and on Hardware. In Proceedings of the 42nd ACM/SIGAPP Symposium on Applied Computing (SAC '27), Gwangju, South Korea, April 5 to 9, 2027. DOI to come.

`CITATION.cff` holds the same reference. The pre-registered predictions of the paper's studies are in `studies/sac2027/`.

## Development and license

How to run the tests, what CI checks and how to build the docs: [Development](https://lorite.github.io/robofacts/project/development/). The docs live in `docs/` (MkDocs, pages linked with wikilinks). Apache-2.0, see `LICENSE`.
