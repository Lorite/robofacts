# The daily loop

This page lists the commands of a normal working day: see what exists, see what a campaign still needs, run it, and analyze the result. It also shows how two sessions or agents share one machine without stopping each other's trials.

## The loop

```bash
# 1. what exists already?
trials index --summary
trials index --experiment <exp> --factors a,b

# 2. what does this campaign still need?
trials plan experiments/<exp>/campaigns/<c>.yaml

# 3. run it: a dry run first, then for real
trials run experiments/<exp>/campaigns/<c>.yaml --limit 1
trials run experiments/<exp>/campaigns/<c>.yaml --limit 1 --execute

# 4. analyze a cohort
trials analyze <exp> <analysis> --where <factor>=<level> --execute
```

Override any factor for one call, in the style of Hydra: `--set trajectory=hover` pins a factor, and `--set trajectory=hover,circle` sweeps it.

## 1. What exists

`trials index` is the [[coverage|coverage index]] over every recorded trial. `--summary` lists only what varies per experiment, `--experiment` restricts it to one experiment, and `--factors` names the axes to group by.

Coverage counts are three-way. A trial is usable (it counts), unverified (its metadata cannot prove its cell) or excluded (aborted, quarantined, no data, or a scratch recording). Only usable trials count as coverage, so an aborted trial never fills a cell. `--include-unverified` on `trials plan`, `trials run` and `trials analyze` widens the count, and the output says so.

`trials records <exp>` lists every trial of one experiment, newest first, with its campaign, class, gate verdict and run log.

## 2. What is still missing

`trials plan` prints, for each cell of the campaign, the trials wanted, the usable trials it already has, and the trials still missing. `--schedule` also prints the order in which the runner will run them. The plan is always computed against the recordings on disk, so it is correct after any interruption.

## 3. Run

Without `--execute`, `trials run` is a dry run. It prints the exact commands of the next trials and creates nothing: no recording directory and no metadata. If you see a recording, it ran.

With `--execute`, the [[runner]] runs each missing trial: the operator steps, the stack, the [[gate|T-0 gate]], the recorder, the action, the verify steps, and then the metadata. `--limit N` runs at most N trials. After a blocked trial (a NO-GO at the gate), the runner stops the campaign, because a stack that fails the gate usually fails it again. `--keep-going` continues instead. The last line counts the trials that ran OK, the blocked ones and the failed ones.

At a terminal, an executed run can wait for you twice per trial. It waits before anything launches, until you confirm the campaign's operator steps. It waits again after the gate passes, until you confirm that the robot is posed (`--no-pause` skips this second wait). [[hardware-runs]] gives the rules for a trial with a real robot.

!!! note "One run per project"
    Only one executed run per project can run at a time. While a run holds the project, a second `trials run --execute` on the same experiments root is refused with a message that names the first run.

## 4. Analyze

`trials analyze <exp> --list` lists the declared [[analyses]]. Without `--execute`, `trials analyze <exp> <name>` prints the cohort (the exact list of recordings) and the commands. `--where factor=level` restricts the cohort and is repeatable. `--execute` runs the analysis.

## When a person must correct a trial

Never edit `trial_metadata.json`, and never move a recording by hand. A command records each decision with its reason in a file beside the recording: `trials exclude`, `trials restore`, `trials amend` and `trials reverify`. See [[decisions]].

## Sharing one machine with another session or agent

Two sessions on one machine can disturb each other in two ways. The clean-slate step of one session stops the processes of the other, and the discovery of the middleware mixes the nodes of both stacks. Give each session its own partition of the middleware, before its first run.

The runner already stops only its own process group after a local trial, so it never stops another session's processes. Isolation does not cover what exists only once: a robot, a radio, a camera, a fixed network port, and the CPU that two simulators share.

### With the ROS 2 backend

Set your own domain, and your own Gazebo partition when you use Gazebo, in the shell that runs `trials`:

```bash
export ROS_DOMAIN_ID=57          # a domain no other session uses: your nodes and topics see only each other
export GZ_PARTITION=$USER-$$     # Gazebo transport: your simulator and its bridge see only each other
trials run experiments/<exp>/campaigns/<c>.yaml --limit 1 --execute
```

The stack, the recorder, the gate and the verify steps of a local trial inherit the runner's environment, so they all use your domain.

The clean-slate tool `kill_ros.sh` (`trials tool kill_ros.sh`) stops only the ROS 2 processes of your `ROS_DOMAIN_ID`. It reads the domain from each process's environment (unset means 0), so it still finds an orphan of your last run. It lists the processes of other domains and leaves them alone. `--all` stops every domain: use it only when you are alone on the machine. `--dry-run` lists what it would stop.

Two sessions on the same domain still stop each other's trials. When `kill_ros.sh` lists a process of another domain that you started yourself, for example a driver that still holds its device, stop it by its process id.
