# Quickstart

The quickstart runs the whole loop of RoboFACTS on an example project with one command. Use it to see what the framework does before you write declarations of your own.

## Without ROS

You need a clone and the general package (see [[install|Install]]):

```bash
git clone https://github.com/Lorite/robofacts.git && cd robofacts
pip install ./robofacts
./robofacts/quickstart.sh
```

The script works on `robofacts/example_project`. Its experiment, `demo_log_recorder`, runs on the built-in `process` [[backends|backend]]: a small script stands in for the system under test and writes a log file. Nothing moves, and no middleware is needed.

## With ROS 2, in Docker

```bash
docker build -t trials-quickstart -f docker/Dockerfile .
docker run --rm trials-quickstart
```

The image starts from the official ROS 2 Humble image, installs both packages and runs `./quickstart.sh` on `robofacts_ros2/example_project`. There, the experiment `demo_talker_listener` records the ROS 2 demo talker. [[talker-walkthrough|Walkthrough: the talker demo]] follows the same loop step by step.

## What the script does

Both scripts run the same steps, in this order:

| Step | Command | What it shows |
| --- | --- | --- |
| 0 | `trials roots` | where the experiments root and the project root resolve |
| 1 | `trials lint` | every declaration checked against the lint rules |
| 2 | `trials plan <campaign>` | the trials the campaign still needs |
| 3 | `trials run <campaign> --limit 1` | a dry run: the exact commands, with nothing executed |
| 4 | `trials run <campaign> --execute --no-pause` | the trials: gate, stack, recording, action, verify step, metadata |
| 5 | `trials plan <campaign>`, then `trials index --experiment <exp> --summary` | the same question again, and the coverage index |

`--no-pause` lets the trials run one after the other. Without it, every trial waits after its gate so that you can pose the setup: for Enter in a simulation, and for the word `fly` on hardware (see [[runner|The runner]]).

## The output, before and after

The [[campaigns|campaign]] `grid` sweeps two [[factors|factors]], `rate` and `duration`, with one repetition each. So it wants four trials, one per cell. Before the run, the planner reports all four as missing:

```text
Campaign: grid  (demo_log_recorder)
  wanted 4 · covered 0 · STILL MISSING 4

  (* marks a non-default value)

  STILL MISSING (4 cell(s)):
    x1  duration=long* execution_host=laptop rate=fast* record_profile=custom   [none yet, want 1]
    x1  duration=long* execution_host=laptop rate=slow record_profile=custom   [none yet, want 1]
    x1  duration=short execution_host=laptop rate=fast* record_profile=custom   [none yet, want 1]
    x1  duration=short execution_host=laptop rate=slow record_profile=custom   [none yet, want 1]
```

The dry run prints the commands of the next trial and touches nothing: the stack (`system.py`), the recorder and the action (`run_trial.py`). After step 4, the same plan says `wanted 4 · covered 4 · STILL MISSING 0`, and the coverage index lists four usable trials. A trial counts only when its recording holds data, its verify step passed and its metadata satisfies the [[metadata-contract|metadata contract]] (see [[coverage|Coverage and records]]).

## What to read next

- [[examples|The examples]]: the three example projects and what each one shows.
- [[first-project|Your first project]]: the same loop on a project of your own.
- [[words|The words]]: the terms these pages use.
