# RoboFACTS

RoboFACTS is a framework for experiments with robots, in simulation and on hardware. You declare an experiment once: its factors, the metadata every trial must record, the checks before each trial, and how to run and verify one trial. The framework then plans the trials, runs them, checks each recording, and counts only the trials that satisfy the declaration. The same declaration drives one robot or a team, on one computer or several.

The name stands for Factors, A Contract, and checked Trials in Simulation and on hardware. A paper at SAC 2027 describes the framework (see [[cite|Cite RoboFACTS]]).

## Two packages

| Package | Holds | Needs |
| --- | --- | --- |
| `robofacts` | the declarations (factors, contract, campaigns), the planner, the linter, the coverage index, the runner, the T-0 gate with its general checks, the web interface, and the built-in `process` backend that needs no middleware | Python 3.10 or newer, PyYAML and pytest (the linter runs its rules with pytest). The web interface also needs Flask |
| `robofacts_ros2` | the ROS 2 backend: `ros2 launch`, `ros2 bag record`, the bag readers, the ROS 2 gate checks, the robot adapters, and the ROS 2 part of the web interface | `robofacts` and ROS 2 Humble |

The dependency goes one way. A test fails the build when `robofacts` imports a ROS module or names ROS in any of its files. `robofacts` is on PyPI, so `pip install robofacts` is enough for the `process` backend.

## Where to start

- New here: [[install]], then the [[quickstart]], then [[first-project|your first project]].
- See it work: [[examples|the examples]], and the [[talker-walkthrough|walkthrough of the talker demo]].
- Learn the ideas: [[words|the words]] first, then [[experiments]], [[campaigns]], [[gate|the T-0 gate]] and [[runner|the runner]].
- Before a trial with a real robot: [[hardware-runs|run on hardware]].
- Look something up: [[commands]], [[lint-rules|the lint rules]], [[gate-checks|the gate checks]], and one page per declaration file, starting with [[trials-yaml|trials.yaml]].
- Connect another middleware: [[backends]] and [[backend-contract|what a backend provides]].

## The source

The code, the issue tracker and the [[changelog]] are on [GitHub](https://github.com/Lorite/robofacts). RoboFACTS is licensed under Apache-2.0.
