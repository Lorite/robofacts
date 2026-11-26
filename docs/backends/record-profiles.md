# Record profiles

A record profile is a named list of what a trial records: topics on the [[ros2-backend|ROS 2 backend]], or files on the [[process-backend|process backend]]. This page shows where a project declares its profiles, how a trial picks one, and how to check what a profile records.

## The project owns its profiles

The project declares its profiles in `<experiments root>/common/config/record_profiles.yaml`. The package ships none, because a set of topics describes one project's robots, drivers and nodes. `trials init` writes a commented template.

The file has reusable `blocks` (lists of topics) and `profiles`. A profile lists blocks, may add `topics` of its own, and may carry a `size` note that the listing prints:

```yaml
blocks:
  robot_state: ["/{robot}/pose", "/{robot}/battery", /tf, /tf_static, /rosout]
  camera_debug:
    topics: ["{camera}/color/camera_info"]
profiles:
  drive: {blocks: [robot_state], size: "~5 MB/min"}
  drive_with_camera: {blocks: [robot_state, camera_debug], topics: [/joy]}
```

A block is a list, or a mapping with `topics`. A profile's list keeps the order of its blocks and drops repeats. A profile that names an unknown block is refused.

On the process backend, the same file lists files instead of topics, for example `"{log_dir}/state.log"`.

## Placeholders

A topic may hold `{name}` placeholders. The names that fill them come from two places:

- the default names of each enabled [[robot-adapters|robot adapter]]
- the project's own names in `trials.yaml`, which win over the adapters' names:

```yaml
record_profiles:
  robot: rover1
  camera: /camera
```

The general package has no default names, because it knows no robot.

## Where the profiles come from

The profiles in force merge these files, in this order. A later file replaces a block or a profile of the same name:

1. the package's own file, when there is one (generic blocks only)
2. the `record_profiles.yaml` of each enabled robot adapter, when it has one
3. the project's `common/config/record_profiles.yaml`

So a project can use an adapter's blocks in its own profiles, and it can replace any of them.

## How a trial picks a profile

In this order:

1. A list in the experiment's `runner.yaml`, `record_topics` (or `record_sources`), wins over any profile. Use it when an experiment's topics are its own.
2. A factor whose `maps_to` has `kind: record_profile` picks the profile per trial. Its levels are profile names.
3. `default_record_profile` in `trials.yaml` names the profile of a trial without such a factor.

Without any of these, the runner refuses the trial rather than record a guess, because a guessed list could leave a recording that looks finished and holds nothing.

A recorder skips a source that does not exist, so a profile may list optional topics.

## Rules the lint checks

- L9: every profile, and every level of a `record_profile` factor, must be a level of `record_profile` in the [[metadata-contract|metadata contract]]. Otherwise every trial recorded on it fails the contract.
- L11 (ROS 2 backend): every profile, and every `record_topics` list, must record `/rosout`.

See [[lint-rules]].

## Check what a profile records

```bash
trials profiles --list                     # the profiles in force, their size notes and their files
trials profiles drive --topics-only        # one profile's topics, one per line
```

On the ROS 2 backend, `trials profiles <name>` prints the whole `ros2 bag record` command of a profile, `--output` sets its bag directory, and `--run` runs it instead of printing it.

## What a slim copy keeps

On the ROS 2 backend, the slim copy of a bag drops the image, depth and point-cloud topics. The package's rules cover the common names (`image_raw`, `compressed`, `compressedDepth`, `depth_registered`, `points` and others) and keep every `camera_info`. A project adds its own rules in `trials.yaml`:

```yaml
slim_topics:
  heavy_patterns: [/my_lidar/points_raw]       # a substring of the topic name: dropped
  light_suffixes: [/debug/overlay/compressed]  # kept, although a heavy pattern matches
```

See [[storage]] for the slim copies.
