# Explore a live stack

Before a campaign runs, you often need to debug and tune its stack: watch it, change a gain, see what happens. `trials explore` brings up exactly the stack that a trial of one condition starts, with a Foxglove bridge that may change parameters. An exploration is never a trial and never counts as coverage. Explore is a feature of the [[ros2-backend|ROS 2 backend]].

## Start an exploration

```bash
# a dry run: the commands, nothing launches
trials explore experiments/<exp>/campaigns/<c>.yaml
# one condition live, until Ctrl-C
trials explore experiments/<exp>/campaigns/<c>.yaml --set gain=high --label gains --execute
```

| Option | Effect |
| --- | --- |
| `--set name=value` | pins a factor. Otherwise the first level of each swept factor is used |
| `--label WORD` | a word for the output folder, `<stamp>_explore_<label>` |
| `--minutes N` | stops by itself after N minutes, instead of at Ctrl-C |
| `--execute` | launches. Without it, the command only prints the commands |

In the [[web-ui|web interface]], an Explore button sits on each experiment row, on each campaign row, and in a campaign's run form, where it uses the cell that you picked in the grid.

## What it starts, and what it leaves out

An exploration starts the setup steps, the launch with the condition's arguments and the auxiliary launches, as a trial does. It adds a `foxglove_bridge` on `127.0.0.1`, port 8765 by default. This bridge may change the parameters that the whitelist allows, but it cannot publish to topics or call services. It also starts the viewers that `runner.yaml` declares.

An exploration has no T-0 gate, no trial action, no `trial_metadata.json` in the counted folder and no line in the attempt ledger. A stack whose parameters you changed is never handed to a trial, because it is no longer the stack that the campaign declares.

## Where it is allowed

- **Simulation** (`simulation: true` in `runner.yaml`): always, and every parameter is writable by default.
- **Real robots**: only when `runner.yaml` declares an `explore:` block. Its `param_whitelist` is empty by default, which gives a live view that changes nothing. Each entry is a regular expression over `<node>.<parameter>` that names one parameter you allow. A pattern that matches every parameter is refused, by lint rule L16 and again at run time. The web interface asks for an arm checkbox, because Explore starts the robots' drivers without a gate.

```yaml
explore:
  param_whitelist: ['^/my_robot/controller\.kp$', '^/detector\.window_px$']
  port: 8765
```

## Actions, jog and viewers

The `explore:` block of `runner.yaml` may also declare buttons for the run page. The runner refuses an unknown key when it loads the file.

```yaml
explore:
  actions:                      # buttons, one action at a time, never run by a trial
    - name: take_off
      command: python3 experiments/my_exp/scripts/my_action.py --mode take_off
  jog:                          # a jog pad that publishes geometry_msgs/Twist
    topic: /cmd_vel
    max_speed_mps: 0.3          # required: each linear axis, clipped by the runner
    step_s: 1.0                 # one press, at most 5 s
    max_yaw_rate_rps: 0.0       # 0 means no yaw buttons
    note: Take off first.       # shown above the pad
  viewers:                      # windows for a person, started after the bridge
    - name: reconfigure
      command: ros2 run rqt_reconfigure rqt_reconfigure
```

Make every action end in a safe state and stop safely on SIGTERM, because the red stop button sends SIGTERM. On real robots, the actions and the jog pad need the "arm actions" checkbox. Each jog press publishes for `step_s`, then a zero twist. The jog limits have no default, because how fast a button may move a robot is a reviewed decision, like a safety limit. A trial never runs an action or a viewer.

## The run page

- Foxglove opens by itself when the bridge is ready. A checkbox turns this off, and a button stays.
- A table shows each parameter that changed (node, parameter, from, now, how many times). "Set back" returns one to its starting value.
- "Record" records the running stack with the trial's own record command into the scratch folder, `bags_scratch/<stamp>_explore[_<label>]`. Its metadata says `scratch: true` and `explore: true`, and holds every parameter value at the start of the recording. At stop it leaves a slim copy for replay.
- The notes box and "Mark this moment" write timestamped lines to `notes.md`.
- "Keep it?" names where a changed value is declared (a launch argument, a factor level, a parameter file) and appends the proposed edit to `proposed_changes.md`. Nothing is applied.
- "Analyze" runs a per-recording analysis of `analyses.yaml` on a recording, also after the stop. A recording on a remote host is refused, because analyses run where the interface runs.
- "Another condition" stops the exploration and starts the picked cell.
- "Stop exploring" lands a running action (SIGTERM, up to 90 s, then SIGKILL), finalizes a recording, and then takes the stack down.

## What it leaves

`experiments/<exp>/runs/explore/<stamp>_explore[_<label>]/` holds `run.log`, the raw `parameter_events.jsonl`, `notes.md`, `proposed_changes.md`, and `parameter_changes.yaml`. The last one lists each parameter that changed (from, to, how many times), the ones set back, the notes and the recordings. A `from: null` means that the node could not be read, and the value is never guessed. To keep a value, change a launch default or a factor level in the declarations, and review the change like any other.

## Three things to know

- A change takes effect only in a node that reads the parameter while it runs. A node that reads the value once, at startup, accepts the change and keeps the old value. Test such a value by restarting the exploration with `--set` on the factor that sets it.
- The bridge shows every node on your `ROS_DOMAIN_ID`, so a stack of another session on the same domain appears too. See [[daily-loop]].
- The setup steps run as for a trial, so a clean-slate step stops the other ROS processes of your domain.
