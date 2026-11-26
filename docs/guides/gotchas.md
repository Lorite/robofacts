# Gotchas

This page collects lessons that cost time in real projects. Each one is a behavior of the package, or of the tools around it, that surprises a new user.

## Running trials

- **A dry run creates nothing.** Without `--execute`, no recording directory and no metadata exist afterward. If you see a recording, it ran.
- **An action can exit 0 without the trial having happened.** A script that starts and ends normally proves nothing about the robot or the recording. Every experiment declares at least one verify step in `runner.yaml` that reads the recording, or a written reason why it has none (lint rule L6).
- **A trial that recorded nothing fails.** When the recorder leaves no data, the runner writes no `trial_metadata.json`, so an empty recording can never fill a cell of the coverage grid.
- **The action has no terminal.** A local action leads its own process group and has no terminal, so it must not prompt. Use `sudo -n`, and ssh with `BatchMode`.
- **An action must wait for what it starts.** When the action exits, a process that it left running in its group is only named in a warning, and the trial counts. An action that starts the robot's motion in the background must wait for it before it exits.
- **One run per project.** A second executed run on the same experiments root is refused while the first one runs. After a runner was killed hard, the next run refuses to start while the old run's processes still run, and it prints the commands that stop them.
- **Two sessions on one middleware partition stop each other.** Give each session or agent its own partition before its first run. See [[daily-loop]].

## Recordings and coverage

- **Never edit `trial_metadata.json`, and never move a recording by hand.** A hand edit destroys the recorded value, and a hand move leaves no reason for anyone to read. Use `trials exclude`, `trials amend` and the other [[decisions|decision commands]].
- **A slim copy is not a second trial.** A slim copy carries a copy of the metadata, and the index counts the trial once, from the full recording or from its slim copy.
- **Coverage counts what this machine holds.** A remote trial keeps its recording on the host that recorded it. Without a copy here, count coverage there. See [[other-machines]].
- **An unmounted disk stops the plan.** When recordings link to an external disk that is not mounted, `trials plan` and `trials run` refuse, rather than show covered cells as missing. See [[storage]].
- **A scratch recording never counts.** Its metadata says `scratch: true`, so it stays excluded even when someone moves it into the counted folder.

## Containers and hosts

- **The shell decides the environment.** A command on another host runs as `<shell> -c "<command>"` in the shell that the host level declares, after the setup files that the level names. A non-interactive shell reads few startup files, and which ones depends on the shell, so environment that only an interactive shell sets is missing. Put every setting that a trial needs into a file that the level names in `env_setup`. A command without its middleware settings can reach nothing while it appears to succeed.
- **The container is not the host.** For a trial on another machine, the container that runs the runner needs the ssh keys and the ssh configuration. Without them, remote hosts neither resolve nor authenticate.
- **A wrong user id makes the workspace read-only.** When the user id inside a container differs from the owner of the mounted workspace, the container cannot write to it. The recorder then prints one line and leaves an empty directory. The `bag_dir_writable` check (also `recording_dir_writable`) catches this at the gate. Put it into every checklist.
- **An external disk is not inside the container.** A disk mounted on the host is invisible inside a container unless the container mounts it too. Recording straight to it from a container needs that mount.
- **Inside a container, the host name is the container's.** The storage tools match a host name onto a level through the `execution_host` factor's aliases, so they may not recognize a container. Pass `--host-level`, or run the storage tools on the host.
- **A git worktree made on the host has no code version in a container.** Its `.git` file names an absolute host path that the container does not have. The runner prints a WARNING, and the metadata records `commit: null` with the reason in `code_version.warning`. Make the worktree's `.git` file relative, for example `gitdir: ../../.git/worktrees/<name>`, or run trials from the main checkout.

## Creating experiments

- **`trials new --from` copies only what the declarations name.** A top-level file or a script that no declaration names stays with the source, and the command lists it under "Left out". See [[new-experiment]].
- **Do not run the full test suite to check one experiment.** `trials validate` runs only the stages of that experiment and takes seconds. CI runs everything.

## With the ROS 2 backend

- **An empty launch argument is dropped.** `ros2 launch` refuses a bare `name:=`, so a factor level whose value is the empty string passes no argument, and the launch file's default applies.
- **A parameter change works only in a node that reads it live.** A node that reads a parameter once, at startup, accepts a change from [[explore|Explore]] and keeps the old value.
- **A stale ROS 2 daemon reports an empty graph.** `ros2 node list` can then show nothing on a healthy stack. The backend restarts the daemon before the machine checks of every gate, and `kill_ros.sh` restarts it after its clean slate.
