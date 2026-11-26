# Storage

Recordings can be large, for example when a trial records camera images. This page shows where the runner writes a recording, how to record straight to an external disk, what a slim copy is, and the safe order to move data off a machine.

## Where a recording goes

By default the runner writes each recording into the experiment's own folders, inside the project. The [[backends|backend]] names the folders:

| Role | Process backend | ROS 2 backend |
| --- | --- | --- |
| counted trials | `recordings/` | `bags/` |
| excluded trials | `recordings_issues/` | `bags_issues/` |
| slim copies | `recordings_slim/`, `recordings_issues_slim/` | `bags_slim/`, `bags_issues_slim/` |
| scratch trials | `recordings_scratch/`, `recordings_scratch_slim/` | `bags_scratch/`, `bags_scratch_slim/` |

The [[coverage|coverage index]] reads only the counted, excluded and slim folders, never the scratch ones.

## Declare external storage

`<experiments root>/common/config/storage.yaml` declares, per host level, where an external disk is mounted:

```yaml
version: 1
external_subdir: trial_bags          # the folder under a mount point
hosts:
  laptop:
    external_dest: null              # null: `external` refuses on this host, the safe default
  robot:
    external_dest: /media/me/MY-DISK/trial_bags
```

The host levels are the levels of the `execution_host` factor. The storage tools match a machine's host name onto its level through that factor's `aliases`, so no second list of machine names exists. `trials storage` prints what this machine declares, and `trials storage --host-level <level>` prints it for another level. See [[storage-yaml]].

Declare a path only when the disk is really mounted there. An unmounted mount point is an ordinary empty folder on the internal disk, so a wrong path fills the disk that it should relieve.

## Record straight to external storage

Choose the destination per experiment with `bags_root: external` in `runner.yaml` (at the top level, or per host), or for one run on the command line:

```bash
trials run <campaign> --bags-root external         # storage.yaml's external_dest for the recording host
trials run <campaign> --bags-root /mnt/data         # an explicit root
trials run <campaign> --bags-root repo              # the experiment's own folder (the default)
```

`--recordings-root` is a second name of the same option. Under an external root, the runner keeps the project's layout, `<root>/<experiment>/<folder>/`, and it leaves a symbolic link in the experiment's folder. Coverage, slim copies and analyses then read the recording through the link while its disk is mounted.

Two rules protect the data:

- When `external` is asked and no destination is declared for the host, the runner stops with a message that names `storage.yaml` and the option. It never writes to the internal disk without a word.
- When a recording links to a disk that is not mounted, `trials plan` and `trials run` stop with an error. A plan without those recordings would show covered cells as missing, and a run would record them again. Mount the disk, then plan again.

## Slim copies

A slim copy is a small copy of a recording without its heavy data. It carries a copy of `trial_metadata.json`, and the index counts the trial once, from the full recording or from its slim copy. A slim copy is what keeps a trial in the coverage grid after its full recording has moved off the machine.

The [[ros2-backend|ROS 2 backend]] makes a slim copy after each trial. It drops the image, depth and point-cloud topics and keeps every `camera_info`. `trials slim --all` makes the missing ones, `trials slim --list` shows the plan, and `trials slim --bag <dir>` makes one. A project adds its own heavy topics in `trials.yaml`:

```yaml
slim_topics:
  heavy_patterns: [/my_lidar/points_raw]     # a substring of the topic name: dropped
  light_suffixes: [/debug/overlay/compressed] # kept, although a heavy pattern matches
```

The [[process-backend|process backend]] makes no slim copy: its recording is the full record of the trial.

## Move data off a machine

The order of the steps is the safety property:

1. **Slim first.** Make the slim copies of every trial, and bring them to the machine where you analyze. A trial whose full recording moves away still counts through its slim copy.
2. **Off-site before delete.** Back up the full recordings to a second place, with a tool that only adds files, and check the backup.
3. **Copy, verify, then delete.** Move the heavy recordings with a tool that verifies every file before it deletes the source.

Two more rules:

- Never move a recording that is still being written. The recorder of each backend can tell whether a recording is finalized: a bag has its `metadata.yaml`, and a recording of the process backend has its `recording.json`.
- While a run executes, the file `<experiments root>/.robofacts_trial_running.json` exists. A copy job should wait while it exists, so that it does not compete with a trial for the network, the disk or the CPU.

A machine that holds recordings is also where its coverage is counted (see [[other-machines]]). [[gotchas]] lists the storage traps of containers.
