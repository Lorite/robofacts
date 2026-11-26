# Robot adapters

A robot adapter holds what only makes sense for one robot family: its gate checks, the default names of its topics, its spoken sentences and its helper modules. This page lists the adapters that ship with the [[ros2-backend|ROS 2 backend]], shows how a project enables them, and explains what to do for a robot without an adapter.

## Why adapters exist

Neither package names a robot outside its adapters. A test of the repository fails the build when a file of `robofacts` or `robofacts_ros2` names a robot family or a lab outside the adapters, with a short allowlist for facts about third-party tools. So robot code goes into an adapter, and the names of a lab go into its own project. The general package stays usable for any robot.

## Enable an adapter

A project enables the adapters it uses in `trials.yaml`:

```yaml
adapters: [crazyflie, spot, px4]
```

Nothing of a disabled adapter is in force: its checks are unknown, and its names and sentences do not apply. When a checklist names a check of a disabled adapter, the error names the adapter to enable. The [[process-backend|process backend]] ships no adapters, so an adapter named in `trials.yaml` of such a project is refused.

## The shipped adapters

| Adapter | Robot | Gate checks | Default names | Other |
| --- | --- | --- | --- | --- |
| `crazyflie` | Crazyflie through crazyswarm2 | `battery`, `crazyflie_radio_battery`, `drone_armable`, `ekf_convergence` | `cf_name: cf1` | the verify steps `verify_flight_happened` and `verify_ext_pose_age`, and the analysis windows of `rosout_trim_utils` |
| `spot` | Spot through spot_ros2 | `graph_nav_localized` | `spot_name: spot` | the lease table in the web interface, for an action that echoes `<robot>/status/leases` |
| `px4` | PX4 through the uXRCE-DDS bridge | `px4_preflight_pass` | | |

What the checks verify:

- `battery`: the battery voltage on the drone's status topic.
- `crazyflie_radio_battery`: the voltage over the radio, with cflib, before the stack starts. It must run alone, because the radio allows one user at a time, so it belongs to the `pre_launch` stage of a checklist.
- `drone_armable`: the supervisor state of the drone allows it to arm.
- `ekf_convergence`: the onboard estimate agrees with motion capture.
- `graph_nav_localized`: Spot is localized on its GraphNav map.
- `px4_preflight_pass`: PX4 reports that it would arm now.

[[gate-checks]] lists the arguments of every check.

## How an adapter takes part

- **Gate checks.** The gate looks for a check in the project's own checks first, then in the enabled adapters, then in the backend, then in the general package. See [[own-gate-checks]].
- **Names.** An adapter's default names fill the placeholders of the [[record-profiles|record profiles]], under the names that the project sets in `trials.yaml` under `record_profiles:`. A project whose drone has another name sets `cf_name` there, and a project that keeps the default relies on `cf1`.
- **Concurrency.** An adapter says which of its checks are read-only probes that may share a gate batch, and which must run alone, with the reason.
- **Sentences.** An adapter gives the spoken sentence of a failed check, for example the measured battery voltage and the advice to swap the battery. See [[spoken-cues]].
- **Helpers.** A verify step or an analysis imports a helper as a module, for example `robofacts_ros2.adapters.crazyflie.verify_flight_happened`.

## What an adapter folder holds

An adapter is a folder `<backend package>/adapters/<name>/`:

| File | Holds |
| --- | --- |
| `checks/<check>.sh` | the gate checks, with the protocol of every check |
| `record_profiles.yaml` | record-profile blocks that the project's profiles may use (optional) |
| `__init__.py` | `NAMES` (the default names), `PARALLEL_SAFE_CHECKS`, `SEQUENTIAL_CHECKS` and `GATE_SENTENCES` |
| other modules | helpers such as verify steps |

## A robot without an adapter

A project does not need an adapter for its own robot. It can do the same work with its own files:

- gate checks in `<experiments root>/common/preflight/checks/` (see [[own-gate-checks]]), with `parallel_safe_checks:` in `trials.yaml` for the read-only ones
- the robot's names under `record_profiles:` in `trials.yaml`
- the stop commands and the robot actions in `robots.yaml` (see [[stopping-robots]])
- verify steps as scripts of the experiment

An adapter is worth adding to the backend when several projects use the same robot family. Write it as a folder of that shape, and test it as any change of the package (see [[development]]).
