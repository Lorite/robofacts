# Changelog

## 0.1.0 (unreleased)

The framework of the robotics lab's experiments, extracted into an installable ROS 2 package (`ament_python`, also `pip install`).

- One command, `trials`, with the planner, the runner, the coverage index, the linter, the T-0 gate, the named analyses, the web interface and the shell tools as subcommands. `ros2 run declarative_robot_trials trials <subcommand>` works the same.
- The experiments root and the project root are configurable: a flag, an environment variable (`TRIALS_EXPERIMENTS_ROOT`, `TRIALS_PROJECT_ROOT`), a `trials.yaml` found by walking up from the current directory, or the nearest `experiments/` directory. No hardcoded path remains.
- `trials init <dir>` creates a project with the shared configuration, and `trials new <name>` an experiment from a commented template that passes the linter, the planner and a dry run as generated.
- Spoken operator cues: a campaign's `voice_cues` and the runner's `voice_announce` reach a laptop voice daemon over UDP (`operator_voice.py`), printed only when `TRIALS_VOICE=off`.
- The lab-specific names left the code: the stop latch path and the record profiles' robot, camera and node names are project settings in `trials.yaml`, with neutral defaults in the package, and every environment variable the package reads starts with `TRIALS_`.
- A project may ship its own gate checks in `experiments/common/preflight/checks/`; they take precedence over the packaged ones.
- `trials tool --help` lists the packaged tools and an unknown name fails with that list, `trials lint` passes pytest flags through (`trials lint -k rule_L5 -x`), and `trials ui --help` works without Flask, which only the interface itself needs (found while rewriting the lab docs to the command, 2026-10-14).
- An example project with the no-robot `demo_talker_listener` experiment, a quickstart script and a Docker image for the quickstart and CI.
