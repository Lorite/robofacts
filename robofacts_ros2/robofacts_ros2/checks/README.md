# Modular pre-flight check framework 

One script per check, one composition per experiment, and a checklist file that machine checks and human checklist both read from — so they cannot drift apart.

## Why

The 2026-07-22 "net incident" flew with **three independent safety layers down at once** (dead extpos transport, absent localization watchdog, an e-stop terminal with no DDS discovery). Each had been verified *at some point* in the session; none was verified *seconds* before arming. The 2026-07-23 runaway added a second lesson: the watchdog *existed* and was *blind*, because it watched a rigid body that appeared in ~1 % of `/poses`.

The rules that fell out of those two days are baked into every check here:

- **A check that passed minutes ago is worthless.** Every check samples fresh data at run time. T-0 means seconds before arming.
- **"Node is up" is never evidence.** Measure the data actually flowing. Flight v2 recorded 0 camera frames from an "up" camera node.
- **Unreadable is a FAIL, never a skip.** A battery you cannot read is a battery you cannot trust.
- **Presence is not effectiveness.** Prefer a health beacon or a data-continuity measurement over `ros2 node list`.
- **The gate must fail atomically.** All checks pass or the flight is a no-go; a gate that cannot run has not passed (exit 2, never 0).

## Layout

```
experiments/common/preflight/
├── lib/common.sh        shared helpers: arg parsing, PASS/FAIL/SKIP, rate measurement
├── checks/*.sh          one single-purpose check each, parameterised, strict exit codes
├── run_preflight.py     composition driver: reads a checklist, runs its checks, gates
└── test_preflight_framework.py
```

Exit codes: `0` PASS · `1` FAIL · `2` usage/schema error (also a no-go) · `3` SKIP (not applicable).

## Running it

Each experiment gets a thin wrapper. For an experiment:

```bash
./scripts/kill_ros.sh                                   # clean slate BEFORE launching
bash experiments/my_experiment/scripts/preflight_t0_gate.sh
```

```bash
bash .../preflight_t0_gate.sh --list
```

shows the plan without running anything (and needs no ROS), which is the cheap way to review a gate from the laptop.

Any check can also be run alone:

```bash
bash experiments/common/preflight/checks/mocap_stream.sh topic=/poses min_hz=85
```

## Adding a check to an experiment

Checklist entries are either a plain string (manual, operator confirms) or an object that names the script verifying it:

```json
{
  "text": "[HARD] MOCAP server running and streaming at full rate.",
  "check": "mocap_stream",
  "args": {"topic": "/poses", "min_hz": "85"}
}
```

`args` values may use `${var}` placeholders, filled by the wrapper's `--var robot=…`, so one checklist serves any robot name. Since 2026-10-01 the runner's gate also passes every scalar factor value of the trial as `--var <factor>=<level>` (explicit gate variables win), so a checklist can say `${world}` or `${n_vehicles}`. Declare a default for it in `variables` so `--validate` resolves. `run_preflight.py --validate` — run for **every** experiment by `test_preflight_framework.py` — fails if a `check` name does not resolve or a placeholder is never supplied, so a typo breaks the test suite instead of a flight.

## Available checks

The core checks live in `checks/`. A check that only makes sense for one robot family ships with that robot's adapter (`robofacts/adapters/<name>/checks/`) and exists only in a project that enables the adapter in `trials.yaml` (`adapters: [crazyflie]`). The lookup order is the project's own `common/preflight/checks/`, then the enabled adapters, then the core, and a checklist that names a check of a disabled adapter fails validation with the adapter to enable. The table marks the adapter checks.

| Check | Verifies | Notes |
|---|---|---|
| `topic_rate` | a topic's data is actually flowing at ≥ `min_hz` (and, with `max_hz`, below it) | generic; `required=false` → SKIP when absent. `max_hz` (2026-10-14) lets a rate identify a configuration: a camera's JPEG build streams 8.0 to 8.2 fps on the same topic as the raw build at 10.2 to 11 fps, so the `stream_format` factor checks raw with `min_hz=9` and jpeg with `min_hz=7 max_hz=9` `appear_s` (2026-11-04, default 0) waits that many seconds for the topic to be advertised before measuring, for a source that creates its publisher only when ready (a visual-inertial estimator's odometry topic after its initializer). |
| `crazyflie_radio_battery` (crazyflie adapter) | the drone's battery, read over the radio with cflib (`pm.vbat`, the median of 5 samples) | a PRE-LAUNCH check (`"stage": "pre_launch"`, 2026-11-04): it opens its own link, so it runs before the driver server holds the radio. A low battery stops the trial in seconds instead of after the stack and the T-0 gate (32 s in one trial) |
| `topic_value` | a sensor stream is alive, not only flowing: the median of a field (`values[2]` of a `LogDataGeneric`, or a dotted path such as `pose.position.z`) over `window_s` is at least `min` and/or at most `max`, and with `vary=true` the field is not constant over the window | generic (2026-11-04); `required=false` → SKIP when absent. Born for a drone's optical-flow sensor, which came up stuck after a boot (squal 0, shutter frozen at 900) while `flow_log` streamed at 50 Hz and `topic_rate` passed |
| `mocap_stream` | MOCAP streaming at full rate | named wrapper over `topic_rate` |
| `camera_frames` | camera frames flowing, not just a node up | optional by default |
| `rigidbody_continuity` | the rigid body is in ≥ `min_pct` of mocap frames | catches name mismatch **and** tracking dead zones |
| `ekf_convergence` (crazyflie adapter) | onboard EKF agrees with MOCAP now | needs a live drone sample; cannot be front-loaded |
| `estop_reachable` | e-stop service visible **from this shell** | run it in the actual kill terminal |
| `battery` (crazyflie adapter) | voltage ≥ threshold | unreadable = FAIL |
| `ros_clean_slate` | no leftovers from the previous run | `max_each` = instances allowed; the current rig is 1 |
| `disk_space` | enough free space for the bag | flight bags run 6–10 GB |
| `bag_dir_writable` | the recorder can write the bag directory (a write probe, not `-w`) | since 2026-10-13 a directory that does not exist yet passes when its nearest existing parent is writable, because the recorder creates it and the gate runs first (the adoption test's false NO-GO). An existing directory that is not writable still fails (a uid mismatch on a shared computer) |
| `node_present` | a node is in `ros2 node list` | deliberately weak, prefer a health/data check. Substring by default. `exact=true` requires the full node name (e.g. `/detector_node`): use it whenever the target has similarly-named neighbours, after the substring `detector` was satisfied by a pose bridge node while the detector had failed to load (2026-08-12) |
| `file_exists` | a required artifact is present (and big enough) | TensorRT `.plan` engines, meshes, calibration maps |
| `host_reachable` | another machine answers, link not degraded | a passing ping does **not** certify DDS |
| `address_on_interface` | an IP is carried by the interface we *think* it is | the rig reaches mocap over either radio and **both carry the same address**, so a fallback is invisible: the internal Realtek chip stalls its downlink ~59 ms every 8.000 s and that lands in the bags as 50–160 mm mocap teleports). Uses `ip`, falls back to python3, and **fails** if it can enumerate neither |
| `gpu_memory` | enough free VRAM for the GPU nodes | a leftover node holding VRAM is the usual cause |
| `dds_socket_buffer` | the kernel can give CycloneDDS the receive buffer its **active** config demands | `net.core.rmem_max` is not persistent across reboots. When the rig rebooted on 2026-08-19 it reverted to the 212992 default, Cyclone's `min="16MB"` became unsatisfiable, and **every node died at `rmw_create_node`** — the gate then reported seven absent-node/dead-topic failures and none of them was the cause. Reads `$CYCLONEDDS_URI` so it speaks for the DDS the stack will really use; compares the **doubled** `SO_RCVBUF` the kernel actually grants (`2 × min(requested, rmem_max)`), because comparing the raw ceiling would NO-GO a working rig. Also FAILs the ignored-since-renamed `<MinimumSocketReceiveBufferSize>` spelling |
| `mem_available` | enough free memory on the recording host, now (`MemAvailable`) | 2026-10-01: the laptop at 29 of 30 GiB used and a load of 21 corrupted a camera latency run, while every stream still "flowed". Inside a container it reads the host's memory, which is the number that matters |
| `rosout_report` | a node's own `/rosout` report contains `present` and not `absent` | for states only the node can tell: a camera node's 5 s report says `JPEG ... B mean` when the camera runs its JPEG build, which still streams decoded frames at 8 fps. **No report at all is a FAIL**, since silence would also read as "no JPEG" |
| `gz_world_matches` | exactly one Gazebo world advertises a clock on the gz transport, and it is `world=${world}` | 2026-10-01: a simulator start script (`px4-rc.gzsim` for PX4) attaches to an already running world, so a leftover Gazebo server makes the trial fly in the wrong world while the metadata records the requested one, and `ros_clean_slate` cannot see it (one server is allowed). `${world}` comes from the trial's factor values (the gate passes every factor as `--var` since 2026-10-01) |
| `graph_nav_localized` (spot adapter) | Spot is localized to its GraphNav map, read from the map-alignment node's `~/status` | `not_localized` or a frozen alignment is a FAIL |
| `px4_preflight_pass` (px4 adapter) | PX4 reports `pre_flight_checks_pass=true` on `/fmu/out/vehicle_status_v1` | 2026-10-01: with `NAV_DLL_ACT 2` and no ground station PX4 refuses to arm while every stream still flows, and `offboard_node` then waits until its mission timeout after the recorder has started |
| `ros_param_equals` | a live node's parameter equals `expected` (`ros2 param get`), numbers numerically (4 = 4.0), text case insensitive | 2026-10-14: for a level that is a node parameter no launch argument sets, such as a depth camera's `depth_module.visual_preset` the operator changes at the rig. Usually declared by a factor with a per-level `expected` (`check:` in factors.yaml). An empty `expected` FAILS (not declared), an absent node or an unset parameter FAILS. It reads what the node reports, which is the requested value, not a measurement of the device |
| `stop_latch` | no operator stop latch (`$TRIALS_STOP_LATCH`, else trials.yaml `stop_latch`, else `/tmp/trial_stop_latch`) younger than 120 s | 2026-10-12, after a lab day (2026-10-07) where the gate passed 62 s after a latch. The 120 s is not an argument: it is `STOP_LATCH_MAX_AGE_S` in `experiments/common/scripts/stop_latch_clear.py`, the rule the setup step and the flight actions use too. The detail ends with `age_s=<s>` (or `age_s=none`), which the gate keeps as `preflight.stop_latch` and in the gate log |


**Stages and spoken reasons (2026-11-04).** A machine entry with `"stage": "pre_launch"` runs in the runner's pre-launch stage, before the stack starts (after `runner.yaml`'s setup steps, before the robot resets and the campaign's setup steps). Every other entry runs in the T-0 gate once the stack is up. `trials gate` runs all of them. An entry may carry `"speak"`: the sentence the voice says when it fails (`{detail}` is filled in). Without it, `gate_speech.py` builds one from the FAIL line for the common checks, and the runner speaks at most three failed checks after "The gate says no go".

## Concurrent machine checks (2026-10-13)

By default the gate runs the independent machine checks of one checklist together, in a thread pool of at most `--jobs` (8). Consecutive entries that may run in parallel form one batch. An entry that may not runs alone, after every earlier entry and before every later one, so `"parallel": false` on an entry is also how a checklist says "start this only after the entries above it" (for example a `/fmu` flight-controller check). The report lines, their order (checklist order), the exit code and therefore the gate log and the trial metadata are the same in both modes, and the operator confirmations come after every machine check.

Which checks may run in parallel is decided per script from its code, in `run_preflight.py`: `PARALLEL_SAFE_CHECKS` (read-only probes) and `SEQUENTIAL_CHECKS` (with the reason). A new check runs alone until someone reads its code and classifies it, and `test_preflight_concurrency.py` fails until they do. The three that run alone: `ros_clean_slate` counts its sibling checks' processes (a `file_exists` path that contains a process pattern would match), `rigidbody_continuity` entries share one capture file per topic, and `mem_available` measures the memory the other checks use. Declaring `"parallel": true` on one of them is a `--validate` error.

The mode: `--concurrency sequential|concurrent`, else `$TRIALS_PREFLIGHT_CONCURRENCY`, else the checklist's top-level `"concurrency"`, else `concurrent`. A frozen checklist can say `"concurrency": "sequential"`, so a re-run reproduces the original gate. `$TRIALS_PREFLIGHT_TIMING_LOG=<path>` appends one JSON line per run with each check's start and end, without changing the report.

## Variables

A checklist declares its own `${...}` defaults, so the wrapper does not repeat them and the test suite can validate every placeholder without a hardcoded list:

```json
"variables": { "robot": "robot1", "robot_host": "192.0.2.80" },
```

`--var name=value` overrides at run time. A placeholder that is used but not declared — or declared but no longer used — fails the test suite.

## Blocking a trial on a fresh PASS

`experiments/common/scripts/preflight_gate.py` makes the gate *binding* for automated sweeps:

```python
from preflight_gate import PreflightGate
gate = PreflightGate(checklist=EXPERIMENT_ROOT / "config/preflight_checklist.json")
ctx.preflight_gate = gate          # experiment_runner calls it before every trial
```

- **`max_age_s` defaults to `0`** — the gate re-runs before *every* trial. A check that passed minutes ago is worthless; raising this trades that guarantee for sweep speed and is recorded per-trial as `reused_age_s`.
- **`override=True` is the WARNING checkbox.** The trial runs after a loud banner, and the override, its reason, and the exact failing checks are written into that trial's `trial_metadata.json`. That is the difference between an escape hatch and commenting the gate out: an overridden trial stays visible and can be excluded from analysis deliberately.
- A gate that cannot run (missing runner, timeout) blocks too — it has not passed.

## Status

Each experiment has a thin `scripts/preflight.sh` wrapper.

**Not yet verified against hardware.** Every check is exercised offline, but no check has been run against a live rig, nor against a deliberately-broken layer. Thresholds are conservative on purpose: they catch a dead or stalled stream, not a slightly slow one.

**The gate is on the live recording path.** Every remaining run notebook has a gate cell immediately before the cell that starts recording. New experiments are no longer scaffolded as notebooks : `trial_runner.py` runs the gate before it launches or records anything, so a trial that cannot pass pre-flight produces no bag at all. It raises `PreflightBlocked` on failure, which stops the notebook there, and `test_run_notebooks_gated.py` asserts the ordering so a cell reshuffle cannot silently move the gate below the recorder. `experiment_runner.py` is gated the same way for automated sweeps.

The gate cell exposes four knobs: `PREFLIGHT_ENABLED` (False for pure-simulation runs), `PREFLIGHT_OVERRIDE` + `PREFLIGHT_OVERRIDE_REASON` (the WARNING checkbox), and `PREFLIGHT_MAX_AGE_S`. The result lands in `TRIAL_METADATA['preflight']`, which every notebook copies into the bag, so the evidence travels with the recording without touching the metadata-writing code.
