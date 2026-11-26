# Walkthrough: the talker demo

This page follows one campaign of the ROS 2 example from the first question to a filled coverage grid. No robot is needed: the ROS 2 demo talker plays the robot.

## Before you start

You need ROS 2 Humble and both packages (see [[install|Install]]). Source your workspace and go to the example project in your clone:

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
cd ~/ros2_ws/src/robofacts/robofacts_ros2/example_project
```

The project has one [[experiments|experiment]], `demo_talker_listener`, and one [[campaigns|campaign]], `rates`. The campaign sweeps one [[factors|factor]], `chatter_rate`, over the levels `slow` and `fast`, with two repetitions each. The level changes how long the action listens (5 s for `slow`, 8 s for `fast`), so the two levels produce different bags.

## Step 1: what does the campaign want?

```bash
trials plan experiments/demo_talker_listener/campaigns/rates.yaml
```

```text
Campaign: rates  (demo_talker_listener)
  Sweep the stand-in factor so the coverage grid visibly fills in as trials run. Nothing flies; this is the framework's worked example and smoke test.

  wanted 4 · covered 0 · STILL MISSING 4

  (* marks a non-default value)

  STILL MISSING (2 cell(s)):
    x2  chatter_rate=fast* execution_host=laptop record_profile=demo   [none yet, want 2]
    x2  chatter_rate=slow execution_host=laptop record_profile=demo   [none yet, want 2]
```

Each line is a cell, one combination of levels. `execution_host` and `record_profile` are factors the campaign does not name, so every trial takes their defaults. The star marks a level that differs from the default.

## Step 2: a dry run

```bash
trials run experiments/demo_talker_listener/campaigns/rates.yaml --limit 1
```

Without `--execute`, the runner prints the plan and the exact commands of the next trial, and runs nothing (the paths are shortened here):

```text
=== DRY RUN — nothing will be launched or recorded ===

[dry-run] chatter_rate=fast* execution_host=laptop  record_profile=demo
    nothing executed
    $ ros2 run demo_nodes_cpp talker
    $ ros2 bag record -s mcap -o <experiment>/bags/<trial> /chatter /rosout
    $ python3 experiments/demo_talker_listener/scripts/listen_for.py --rate fast
```

The three commands are the stack, the recorder and the action. The trial's name (shown here as `<trial>`) holds a timestamp, the non-default levels and the repetition, for example `20261126T114122_chatter_rate-fast_rep01`.

## Step 3: run the trials

```bash
trials run experiments/demo_talker_listener/campaigns/rates.yaml --execute --no-pause
```

Each of the four trials goes through the same sequence (see [[runner|The runner]]):

1. The runner starts `talker` in the background and waits `settle_s` (2 s).
2. The [[gate|T-0 gate]] runs `config/preflight_checklist.json`: the kernel's receive buffer for DDS, exactly one running talker, free disk space, and a writable bag directory. A NO-GO stops the trial before anything is recorded.
3. The recorder starts: `ros2 bag record` writes `/chatter` and `/rosout` into a new bag under `bags/`.
4. The action listens to `/chatter` for 5 or 8 s.
5. The runner stops the recorder before the stack, so that rosbag2 finalizes the bag. Then it stops the talker.
6. The verify step `verify_chatter.py` reads the bag's `metadata.yaml` and fails the trial when it holds fewer than 3 messages.
7. The runner writes `trial_metadata.json` with every factor level under its metadata key, and the backend makes a reduced copy of the bag in `bags_slim/`.

`--no-pause` lets the four trials run one after the other. Without it, each trial waits for Enter after its gate, because the experiment declares `simulation: true`.

!!! note "Why a verify step"
    `listen_for.py` exits 0 even when the talker never started. Only the recording shows that the trial happened, so the runner asks the bag, not the exit code. A trial whose verify step fails is aborted and does not count.

## Step 4: the same question again

```bash
trials plan experiments/demo_talker_listener/campaigns/rates.yaml
trials index --experiment demo_talker_listener --summary
```

The grid is now full: the plan reports `wanted 4 · covered 4 · STILL MISSING 0`, with both cells under `COVERED`. Run the campaign again, and the runner prints `Nothing to run — the campaign is already covered.` To record more trials, raise `repetitions` in the campaign. To see every trial with its gate verdict and its files, run `trials records demo_talker_listener` (see [[coverage|Coverage and records]]).

## The same in the web interface

The web interface needs Flask (`pip install './robofacts[ui]'`) and a password:

```bash
EXPERIMENTS_UI_PASSWORD=demo trials ui
```

Open `http://localhost:8080`, choose the experiment and then the campaign. The campaign page shows the coverage grid. **Dry run: show the plan** prints the commands without running anything. To run trials, tick **arm** and press **EXECUTE**. The run page then shows the trials as they happen. See [[web-ui|The web interface]].

## What to try next

- Break the verify step: change `--min-messages 3` to a number the bag cannot reach, run one trial, and read why it does not count in `trials records`.
- Raise `repetitions` in `campaigns/rates.yaml` from 2 to 3, and watch the plan ask for one more trial per cell.
- Read [[first-project|Your first project]] to start a project of your own.
