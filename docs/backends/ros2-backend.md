# The ROS 2 backend

The package `robofacts_ros2` is the backend for ROS 2 stacks. `ros2 launch` or `ros2 run` starts the stack, `ros2 bag record` records the trial, and a recording is a rosbag2 directory, called a bag. This page shows how an experiment declares its stack, what the backend adds to the general package, and how to make a simulator run without a window.

## Select it

The backend needs ROS 2 Humble and the package `robofacts_ros2` next to `robofacts` (see [[install]]). A project selects it in `trials.yaml`:

```yaml
backend: ros2
```

Every `trials` command must then import `robofacts_ros2`, so run it from a shell where the ROS 2 workspace is sourced.

## Declare the stack

```yaml
launch:
  package: my_package
  file: my_stack.launch.py
launch_mode: launch               # or run, for a single node
record_topics: ["/chatter", "/rosout"]
```

- With `launch_mode: launch` (the default), the stack is `ros2 launch <package> <file> name:=value ...`. The arguments come from the [[factors]] whose `maps_to.kind` is `launch_arg`, sorted by name.
- An empty value is dropped instead of passed as `name:=`, which `ros2 launch` would refuse. The launch file's default then applies.
- With `launch_mode: run`, the stack is `ros2 run <package> <file>`, and `file` names the executable. This form takes no launch arguments. A stack that needs parameters uses a launch file.

The topics come from `record_topics`, or from a [[record-profiles|record profile]]. Lint rule L11 requires every profile and every `record_topics` list to record `/rosout`, and lint rule L2 requires every `launch_arg` factor to name an argument that the launch file declares.

## A recording

The recorder runs `ros2 bag record -s mcap -o <bag> <topics>`. `record_storage:` in `runner.yaml` chooses another storage plugin. A bag is complete when its `metadata.yaml` exists, which rosbag2 writes last, and it holds at least one `.mcap` or `.db3` file. Without that, the trial fails and the runner writes no metadata.

After each trial, on the host that holds the bag, the backend:

1. writes `rosout.txt` beside the bag, from the bag's `/rosout`
2. makes a slim copy without the image, depth and point-cloud topics, and brings it to the runner's machine (see [[storage]])
3. makes the stills and the camera video of the camera that `config/trial_media.yaml` declares, when an experiment declares one

None of these steps can fail a trial that has happened.

## What it adds

- **Gate checks.** 13 checks of ROS 2: `camera_frames`, `dds_socket_buffer`, `estop_reachable`, `gz_world_matches`, `mocap_stream`, `node_present`, `rigidbody_continuity`, `rigidbody_near`, `ros_clean_slate`, `ros_param_equals`, `rosout_report`, `topic_rate` and `topic_value`. See [[gate-checks]].
- **A fresh daemon before every gate.** A stale `ros2` daemon can report an empty graph on a healthy stack, so the backend restarts the daemon of your `ROS_DOMAIN_ID` before the machine checks. A daemon that hangs is killed. `trials gate --no-daemon-bounce` skips the restart.
- **Robot adapters.** See [[robot-adapters]].
- **Commands.** `trials explore` (see [[explore]]), `trials slim` (the slim copies), and a `trials profiles` that can also print or run the `ros2 bag record` command of a profile.
- **A tool.** `kill_ros.sh`, the clean slate of your `ROS_DOMAIN_ID` (see [[daily-loop]]).
- **The web interface.** Replay in Foxglove, the layouts, the Viewing page, the bag download, the Kill ROS button and the Explore pages (see [[web-ui]]).
- **Defaults for a remote host.** A host that names no setup files sources `/opt/ros/humble/setup.zsh` and `<workspace>/ros2_ws/install/setup.zsh`. On another machine, `trials` starts as `ros2 run robofacts_ros2 trials`.
- **The folders** `bags/`, `bags_issues/`, `bags_slim/`, `bags_issues_slim/`, `bags_scratch/` and `bags_scratch_slim/`.

## The stop sweeps

The backend matches the ROS 2 processes of the project's workspace by kind: `all`, `recorder` (only `ros2 bag record`) and `launch` (only `ros2 launch` and `ros2 run`). It never matches the `ros2` daemon. What only the project knows goes into `trials.yaml`:

```yaml
stop_patterns:
  recorder_scripts: [record_my_bag]     # wrappers that exec ros2 bag record
  owned_containers: [my_container]      # composable containers that the project's launches start
  kinds: {my_kind: "ros2 launch.*my_subsystem"}
```

## A launch file without a headless argument

A trial in simulation should run without a window. When you own the launch file, add a `headless` launch argument and map it from a factor, or give it a default that suits trials.

When the launch file is not yours, look for an argument that the launch file pastes into the simulator's command line, such as the world file. Append the simulator's own flag to its value in `factors.yaml`:

```yaml
world:
  levels: [ground_plane]
  default: ground_plane
  metadata_key: world
  maps_to:
    kind: launch_arg
    names: [world_file]
    level_values:
      ground_plane: {world_file: "/path/to/worlds/ground_plane.sdf -s"}   # -s: the simulator's server only
```

The runner passes a value with a space as one argument, `world_file:=/path/to/worlds/ground_plane.sdf -s`, both locally and on a remote host. The method works only when the launch file joins the argument into a command string. Check it once in the trial's run log, `runs/<trial>.log`: the simulator's command line must show the flag. When the launch file passes the value as its own argument, write a small wrapper launch file in your package instead.
