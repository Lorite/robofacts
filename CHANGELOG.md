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
- The code stamp of a trial also describes the build: for each colcon workspace (`colcon_workspaces` in `trials.yaml`, else `.` and `ros2_ws`), the HEAD of every source repository under `src/` and the newest `install/` time, and `possibly_unbuilt` names the repositories committed after that build. A slow build query no longer costs the commit. The stop and action scripts bound the command inside the container too, so a hung stop cannot keep running there after the local client gives up.
- The page of an action that echoes a Spot `LeaseArray` states who holds the body lease in one line, with a table of every resource (the tablet held it unseen through four trials on 2026-08-10).
- A verify step for pose loss read from the drone itself, `verify_ext_pose_age` (2026-10-14): the on-board `locSrv.ageMs` of every `/<drone>/ext_pose_age` topic over the IS_FLYING window, with a declared threshold and run length, per-drone requirements (always, or only when airborne, waived before a declared date or on the simulation backend) and a one-line JSON summary for a metadata probe. The server's `Motion capture rate off` warnings, which the 2026-10-07 bags proved a bookkeeping artifact, play no part.
- Gate checks: `topic_rate.sh` takes an optional `max_hz` (a rate that must stay below a ceiling, for a level defined by a slower stream), and the new `ros_param_equals.sh node= param= expected=` compares a live node parameter with the trial's level, numerically for numbers.
- An example project with the no-robot `demo_talker_listener` experiment, a quickstart script and a Docker image for the quickstart and CI.
