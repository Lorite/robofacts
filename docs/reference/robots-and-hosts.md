# robots.yaml and hosts.yaml

`hosts.yaml` declares the machines that run trials, once for the whole project. `robots.yaml` declares how to stop each robot, its named actions and its reset steps. This page lists the keys of both files and of the `hosts:` block of `runner.yaml`.

## Where the files are

Both files live in `experiments/common/config/`. `trials init` writes `robots.yaml` with an empty list of robots. `hosts.yaml` is optional: a project without it writes the full settings of each host in `runner.yaml`.

## hosts.yaml

```yaml
version: 1
rig: robot
hosts:
  laptop: {local: true}
  robot:
    host: my-robot-host
    container: my_container
    container_hostname: robot-dev
    shell: zsh
    workspace: /workspaces/my_project
```

| Key | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `hosts` | mapping | no | empty | One entry per host level, the levels of the `execution_host` factor. |
| `rig` | string | no | none | The level whose ssh alias `$RIG_HOST` replaces for one session. It must be one of the levels. |
| `version` | integer | no | none | The templates write `version: 1`. The code does not read it. |

### The settings of a host

The same keys appear in `hosts.yaml` and under `hosts:` of [[runner-yaml|runner.yaml]].

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `local` | boolean | `false` | `true` runs the commands on the runner's own machine. |
| `host` | string | none | The ssh alias of the machine. A host that is not local needs it. |
| `container` | string | none | The container on that machine that runs the commands. The runner starts each remote command as `ssh <host> docker exec -i <container> <shell> -c ...`, so a trial on a host that is not local needs it. |
| `container_hostname` | string | none | The host name inside that container. A runner that runs inside this container runs the commands directly, without ssh. The stop tools use it to tell the robot's container from a local container of the same name. |
| `shell` | string | `zsh` | The shell inside the container. |
| `workspace` | path | the project root | The directory of the project's checkout on that machine. The commands run there. |
| `env_setup` | list of paths | empty | More files to source before each command. |
| `ros_setup`, `ws_setup` | path | the backend's | Two setup files to source before each command. The process backend sources none by default. |
| `bags_root` | string | the experiment's | Where this host writes the recordings, see [[storage-yaml|storage.yaml]]. |

### How runner.yaml uses the registry

An experiment's `runner.yaml` lists the levels that it may run on. A level with no value takes the settings of `hosts.yaml`. A level with keys takes them and replaces only the keys it names. A level that `hosts.yaml` does not know is used as written.

```yaml
hosts:
  laptop:
  robot:
    env_setup: [/workspaces/my_project/extra_setup.zsh]
```

### With the ROS 2 backend

Without `ros_setup` and `ws_setup`, the ROS 2 backend sources `/opt/ros/humble/setup.zsh` and `<workspace>/ros2_ws/install/setup.zsh` on a remote host. On a local host, the runner sources only the files that the host names.

## robots.yaml

```yaml
version: 1
robots:
  rover1:
    host_level: robot
    verified: false
    soft_stop: "touch /tmp/trial_stop_latch && my_robot_cli halt"
    estop:
      description: "Cut the motors"
      command: "touch /tmp/trial_stop_latch && my_robot_cli emergency_stop"
    actions:
      stand: "my_robot_cli stand"
    reset:
      - {name: power_cycle, command: "my_robot_cli power_cycle", when: always}
```

| Key of a robot | Type | Default | Meaning |
| --- | --- | --- | --- |
| `estop` | command | none | Stop the robot now. `trials tool estop.sh` runs it for every robot. |
| `soft_stop` | command | none | Halt or land gracefully. `trials tool estop.sh --soft` runs it. |
| `actions` | mapping | empty | Named commands that are not a stop, such as `stand`. `trials tool robot_action.sh <robot> <action>` runs one. |
| `reset` | list | empty | Steps that run before the stack of each trial of an experiment that uses the robot. See below. |
| `host_level` | string | none | Where the robot's commands run: a level of `hosts.yaml`. |
| `host`, `container`, `container_hostname`, `shell` | string | `local`, none, none, `zsh` | Where the commands run, for a project without `hosts.yaml`. |
| `verified` | boolean | `false` | Whether the stop commands were tested on the real robot. The stop tool prints a warning for an entry that is not verified. |

A command is a string, or a mapping with `command` and an optional `description`. A robot without the requested stop is skipped and reported.

!!! warning
    A software stop crosses several links, and every one can fail. The physical emergency stop stays the primary stop. Let every stop command write the stop latch file first, so the `stop_latch` gate check refuses the next trial. See [[stopping-robots]].

### Where a stop goes

The stop tools resolve the target of each robot in this order: the `--host-level` flag, then the execution host of a hardware trial that runs on this machine, then the robot's `host_level`, then its own `host` and `container`. A simulated trial never moves a stop.

### Reset steps

| Field | Default | Meaning |
| --- | --- | --- |
| `name` | `<robot>_reset_<index>` | The name in messages. |
| `command` | none, required | The command, with the same placeholders as a setup step. |
| `required` | `true` | A required reset that fails blocks the trial. |
| `when` | `always` | `first`, `repeat` or `always`, as for setup steps. |
| `only_with` | none | A feature name. The step runs only for an experiment that declares this feature. |

An experiment declares its robots in `runner.yaml`: `robots: [rover1]`, or `robots: [{name: rover1, with: [camera], reset: false}]`. `with` lists the features for `only_with`, and `reset: false` skips the robot's resets for that experiment. A campaign skips them with `robot_reset: false`.

## Environment variables

| Variable | Meaning |
| --- | --- |
| `RIG_HOST` | Replaces the ssh alias of the `rig` level of `hosts.yaml` for one session. For the stop tools, it also replaces the own `host` of a robot that is not local. |
| `TRIALS_ROBOTS_CONFIG` | Another `robots.yaml` for the stop tools. |
