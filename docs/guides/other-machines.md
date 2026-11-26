# Run on another machine

A trial can run on another machine than the one where you type `trials run`, for example on the computer of a robot. This page shows how to declare the machines once, how an experiment chooses where its trials run, and what happens on the remote machine.

## Host levels

A machine is a level of the `execution_host` [[factors|factor]], for example `laptop` or `robot`. Each trial runs on the machine that its level names. A campaign sweeps or fixes the level like any other factor, and `--set execution_host=<level>` chooses it for one call:

```bash
trials run experiments/<exp>/campaigns/<c>.yaml --set execution_host=robot --limit 1
```

## Declare each machine once

`<experiments root>/common/config/hosts.yaml` says once how to reach each level:

```yaml
version: 1
rig: robot                      # the level that RIG_HOST retargets
hosts:
  laptop: {local: true}
  robot:
    host: my-robot-host         # an ssh alias
    container: my_container     # omit to run directly on the host
    container_hostname: robot-dev
    shell: zsh
```

A level is either `local: true` or an ssh `host`, with an optional `container`, the `shell` inside it, and setup files to source. `container_hostname` is the host name inside that container: when the runner finds itself running there, it runs the commands directly instead of over ssh. [[robots-and-hosts]] lists every key.

## What an experiment declares

An experiment's `runner.yaml` lists under `hosts:` the levels it may run on, because not every experiment can run everywhere:

```yaml
hosts:
  laptop:
  robot:
    env_setup: [/home/me/overlay_ws/install/setup.zsh]
```

A level with no value takes the settings of `hosts.yaml`. A level with keys takes the registry's settings with only those keys replaced, so you write only what differs, for example the setup file of an overlay workspace. The runner refuses a trial on a level that the experiment does not declare. A project without `hosts.yaml` writes each level's full settings in `runner.yaml`.

## How a remote trial runs

For a remote level, the runner wraps each command of the trial: ssh to the host, then into the container, then the declared shell. Your machine only coordinates. The stack, the recorder, the action and the verify steps all run on the remote host. The remote host needs the package and the project, at the path that the level's `workspace` names (by default the same project path as on your machine).

**The gate runs on the execution host.** The runner sends the checklist with the command and runs the [[gate|T-0 gate]] inside the host's container. The checks therefore measure that host's processes, disk, network and stop latch, not your machine's. The verdict records where it ran (`ran_on` in the trial metadata and in the gate log). A failed ssh connection is a gate ERROR, never a pass.

**The recording stays where it was recorded.** A remote trial writes its recording and its `trial_metadata.json` on the remote host. A trial that recorded nothing fails, and the runner writes no metadata for it, so an empty recording can never fill a cell.

**Coverage counts what this machine holds.** The [[coverage|coverage index]] reads the recordings on the machine where it runs. The [[ros2-backend|ROS 2 backend]] makes a slim copy of each recording on the remote host and brings it back, and the slim copy carries the trial's metadata, so the trial also counts on your machine. The [[process-backend|process backend]] brings nothing back: count coverage on the host that holds the recordings, or copy them first.

`trials validate` makes one dry run per execution host that a campaign uses, so it checks the local and the remote command paths.

## The stop follows the trial

`estop.sh` and `robot_action.sh` use the same registry. During a hardware trial on a remote level, a stop typed on the runner's machine goes to that level for the robots that the experiment declares. A simulation never moves a stop. See [[stopping-robots]].

## Retarget a host for one session

A robot computer can have more than one ssh alias, for example one on the robot's own network and one through a VPN. `RIG_HOST=<alias>` replaces the ssh alias of the level that `rig:` names, for one session. `trials run`, `estop.sh` and `robot_action.sh` all read it:

```bash
RIG_HOST=my-robot-host-vpn trials run experiments/<exp>/campaigns/<c>.yaml --limit 1
```

## Keep a long run alive

A remote trial controls a robot through a network connection. Start the runner on a machine where a dropped connection cannot end it: open a terminal multiplexer such as tmux on the remote host, and start `trials run` inside it. A network drop then only detaches your view, and the trial continues. Attach again to see it.

The runner also needs a terminal for the pose pause of a hardware trial (see [[hardware-runs]]). A session in tmux is a terminal, and a command that you send in the background is not.
