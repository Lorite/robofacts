# Stop robots

This page shows how to stop the robots of a project from the command line and from the web interface, where each stop goes, and how the stop latch keeps a stopped robot from starting the next trial.

!!! warning "Not the primary stop"
    A software stop travels from your machine over ssh and a container to the robot's own link, and every step can fail. The physical emergency stop of each robot stays the primary stop. Keep it within reach during every hardware trial.

## Stop every robot

```bash
trials tool estop.sh              # emergency stop of every configured robot, in parallel
trials tool estop.sh --soft       # halt or land gracefully instead
trials tool estop.sh --robot <name>
trials tool estop.sh --list       # what is configured, where each stop goes and why, does nothing
trials tool estop.sh --dry-run    # prints the exact commands, runs nothing
```

The default is the emergency stop, because one short command must stop everything in the moment you have. Inspection is what needs a flag, not the stop. `trials tool` lists the tools of the project, and `estop.sh --help` prints every option.

## One file knows how to stop each robot

The stop commands live in `<experiments root>/common/config/robots.yaml`. The command line, the web interface and every other surface read the same file, so they cannot disagree. Adding a robot is an edit of this file, not of code.

```yaml
version: 1
robots:
  rover1:
    host_level: robot        # where its driver runs, resolved through hosts.yaml
    soft_stop: "touch /tmp/trial_stop_latch && my_robot_cli halt"
    estop: "touch /tmp/trial_stop_latch && my_robot_cli emergency_stop"
    actions:
      sit: {command: "my_robot_cli sit", description: "sit down"}
```

A stop entry is a command string, or a mapping with `command` and `description`. A robot without `host_level` names its own `host`, `container` and `shell`. `estop.sh` warns about an entry that is not marked `verified: true`, which means that nobody has tested it against the real robot. [[robots-and-hosts]] describes every key.

## Where a stop goes

For each robot, `estop.sh` decides the host in this order:

1. `--host-level <level>`, for this one call.
2. The hardware trial that runs on this machine now. Its stack, and so the robot's driver, runs on the trial's execution host, so the stop goes there for the robots that the experiment declares (every robot when it declares none). A simulation never moves a stop.
3. The robot's `host_level`, resolved through `hosts.yaml` next to `robots.yaml`.
4. The robot's own `host`, `container` and `shell`.

`estop.sh --list` prints the host of each robot and the reason. The runner prints the same targets at the start of every hardware trial. Inside a container, a stop runs directly only when that container is the target's own, recognized by the level's `container_hostname`. From any other container, it goes over ssh.

Two environment variables retarget a stop for one session, without an edit of a file: `RIG_HOST` replaces the ssh alias of the registry's `rig` level, and `RIG_CONTAINER` replaces the container of every robot that names one. See [[other-machines]].

## The stop latch

Make every stop command write the stop latch first, as in the example above. The latch is a file, `/tmp/trial_stop_latch` by default. A project sets another path as `stop_latch:` in `trials.yaml`, and `$TRIALS_STOP_LATCH` overrides both.

The latch protects the next trial when a later step of the stop fails. The gate's `stop_latch` check fails while the latch is younger than 120 seconds, so add it to the checklist of every hardware experiment. Its message names the latch and says what to do: make the robot safe, then delete the latch file. `python3 -m robofacts.stop_latch_clear` applies the same rule as a setup step, which refuses before anything launches.

## Robot actions

Some commands are not stops but are still part of operating a robot: stand, sit, claim control, read a status. `robot_action.sh` runs exactly the actions that `robots.yaml` declares under `actions:`, and nothing else:

```bash
trials tool robot_action.sh --list               # every declared action
trials tool robot_action.sh rover1 sit
trials tool robot_action.sh --dry-run rover1 sit # prints the wrapped command
```

It resolves the host as `estop.sh` does, and it accepts `--host-level`.

## In the web interface

Every page of the [[web-ui|web interface]] shows a stop bar with "E-STOP ALL" and "Soft stop all". Both call `estop.sh` with the project's `robots.yaml`. The start page lists the robots that the bar reaches and their declared actions.

A stop from the web interface also ends the running campaign. The runner stops the action, closes the recording and then stops the stack, and no further trial starts. When the stop arrives during the settle time, the gate or the pose pause, the trial ends before its recorder or its action starts. A view-only interface registers no stop route.

## With the ROS 2 backend

The [[ros2-backend|ROS 2 backend]] adds `kill_ros.sh`, the clean slate between trials, and a "Kill ROS" button in the web interface. This is not a robot stop. It removes the commands and the position feed of a moving robot, which leaves the robot to its own failsafe, so the web interface refuses it while a trial runs, unless you force it. Use the stop bar for a robot, and the clean slate between trials.
