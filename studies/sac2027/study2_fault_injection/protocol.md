<!-- Verbatim sections of experiments/px4_sitl_fault_injection/README.md in the laboratory repository at commit 5c5c220c72 (2026-11-25): the hypotheses, the pilot, the frozen predictions and the deviation log. Other sections (apparatus, results, workflow) are left out. See ../README.md. -->

## Research question

When a known failure is present at the start of a trial, does the T-0 gate keep that trial out of the coverage count, and which later layer (the no-metadata rule, the action's exit code, a verify step) stops the faulty trials when the gate does not decide?

The design is Section 3 of the vault note `ai_chats/notes/Design - evaluation experiments for the SAC 2027 experiments-framework paper (2026-10-01).md`, approved by Alejandro on 2026-10-01. This README is the runbook. The hypotheses, outcome rules and predictions below are pre-registered: they are frozen in one commit before the main run, and any later change is a logged deviation.

## Hypotheses (pre-registered)

- **H1.1, checked faults are blocked.** For the four faults the frozen gate has a check for (`leftover_stack`, `gt_bridge_down`, `dds_no_discovery`, `bag_dir_unwritable`), every attempt with `gate=on` ends blocked at T-0 with no bag. Falsified by any such attempt that produces a bag directory.
- **H1.2, without the gate, faulty trials enter coverage.** With `gate=off`, at least one attempt of a checked fault ends usable in the index while the oracle marks it invalid, and the share of silently wrong attempts among the checked faults is higher than with `gate=on` (one-sided Fisher exact test, alpha 0.05). Falsified if `gate=off` produces no silently wrong attempt.
- **H1.3, the later layers catch part of the rest.** With `gate=off`, each later layer stops at least one fault class. Predicted: `bag_dir_unwritable` by the no-metadata rule, `dds_no_discovery` by the no-metadata rule or the exit code, `vehicle_not_ready` by the exit code, `gt_bridge_down` by the campaign's verify step.
- **H1.4, a gate checks only what it declares.** The two faults without a matching check (`stale_world`, `vehicle_not_ready`) pass the gate in both arms, and `stale_world` ends silently wrong in both arms.
- **H1.5, the gate's cost.** Reported descriptively: the false NO-GO rate on `fault=none` (exact 95 % interval), the gate's duration per trial, and the wall time per attempt by outcome.
- **H0.** The distribution of outcomes for faulty attempts does not depend on `gate`.

## Pilot (2026-10-01, before the freeze)

One attempt per fault with `gate=off` (shadow) as normal trials in a throwaway experiments root (`/tmp/e1_pilot_root`, never this experiment's coverage), through `run_e1.sh`, so the verify steps ran too. Evidence, without bags: `results/20261001T221500_pilot/` (oracle tables, the pilot's ledger and gate log, its campaign file).

| fault | shadow gate verdict (failed item) | outcome (oracle) | what the pilot showed |
|---|---|---|---|
| `none` | GO | O5, V1 to V5 true | gate 12.5 s, attempt 77 s, hover 27 s |
| `leftover_stack` | **GO** | **O6**, V2 false (`x500_0` seen only in `windy`) | the trial's own agent fails to bind UDP 8888 (`bind error ... errno: 98`) and exits, its PX4 exits with `PX4 server already running for instance 0`, so `ros_clean_slate` counts one of each and passes. `offboard_node` flies the leftover vehicle in the other world. Two bridges on one Gazebo topic record ground truth at 100 Hz |
| `stale_world` | GO | O6, V2 false | as predicted: PX4 attaches to the running `windy` world |
| `gt_bridge_down` | NO-GO (`topic_rate`: ground truth not advertised) | O4, V4 false (0 Hz) | as predicted |
| `dds_no_discovery` (shared config) | GO | not realized | the shared `.devcontainer/cyclonedds.xml` discovers the agent while `tailscale0` is up (it lists the laptop's own Tailscale address as a peer). Replaced by `config/cyclonedds_no_agent.xml`, kept in the record as X3 |
| `dds_no_discovery` (no-agent config) | NO-GO (`topic_rate`: `/fmu/out/vehicle_local_position_v1 is not advertised at all`) | O3 (mission timeout after 90 s) | the documented symptom, with the ground-truth check passing |
| `vehicle_not_ready` | GO | O3 (timeout in `REQUEST_ARM`, `pre_flight_checks_pass=False`) | as predicted, the preflight checks fail within seconds of the parameter change |
| `bag_dir_unwritable` | NO-GO (`bag_dir_writable`) | O2 (no `metadata.yaml`) | as predicted |

Both realization details the design marked "to measure" hold: the runner's children do not re-source `/etc/zsh/zshenv`, so the DDS symlink switches the configuration per attempt, and `px4-param --instance 0 set NAV_DLL_ACT 2` makes the preflight checks fail. The witness samples at 1 Hz through one long-lived `gz.transport13` node (5 ms per sample, while the `gz` CLI costs 2 s per call) and copied every attempt's ulog. Measured attempt wall times: 72 to 93 s for a flown or verify-aborted hover, 135 to 156 s for an action timeout, so the 80 attempts take about 2 h.

## Pre-registered predictions (frozen, `config/predictions.yaml`)

| `fault` | `gate=on` | `gate=off` | changed by the pilot |
|---|---|---|---|
| `none` | O5 | O5 | |
| `leftover_stack` | **O6** | O6 | yes, from O1: the frozen gate names this fault, but its check does not fire, so **H1.1 is predicted falsified for `leftover_stack`** and H1.2 rests on no fault (see below) |
| `stale_world` | O6 | O6 | |
| `gt_bridge_down` | O1 | O4 | |
| `dds_no_discovery` | O1 | O3 | `gate=off` narrowed from "O2 or O3" |
| `vehicle_not_ready` | O3 | O3 | |
| `bag_dir_unwritable` | O1 | O2 | |

Consequences, stated before the main run: H1.1 holds for three of the four faults the frozen gate names (`gt_bridge_down`, `dds_no_discovery`, `bag_dir_unwritable`) and is predicted falsified for `leftover_stack`. H1.2 is predicted **not supported**: among the four named faults, the only silently wrong one (`leftover_stack`) is silently wrong in both arms, so the share does not differ between arms. In this setup the later layers stop the three faults whose check fires even without the gate, and the gate's measured value for them is time saved (an O1 attempt ends before the recorder starts). H1.3 is predicted to hold for all three layers, H1.4 to hold, and phase B (`gz_world_matches`, `px4_preflight_pass`) is predicted to block `leftover_stack`, `stale_world` and `vehicle_not_ready`. The analysis (`scripts/e1_table.py`) computes every test exactly as the design specified, on the design's four named faults, and scores each cell against the table above.

## Deviations from the design note (logged before the freeze)

- `gt_bridge_down` is killed when the aux launch starts, about 30 s after the launch (after `settle_s`), not 15 s after it: aux launches start after the settle, and the kill still precedes the gate.
- `dds_no_discovery` uses a dedicated config instead of the shared `.devcontainer/cyclonedds.xml`. In the pilot (2026-10-01 22:07) the shared config DID discover the agent, because it lists the laptop's own Tailscale address as a unicast peer and the FastDDS agent listens on the standard discovery ports of every interface: the fault was not realized while `tailscale0` was up. `config/cyclonedds_no_agent.xml` reproduces the documented symptom (`/fmu/out/*` not advertised while the ground-truth bridge flows) independently of the Tailscale state.
- Phase B adds `leftover_stack` to the design's two faults, because the pilot showed the frozen gate lets it through too. The design's purpose for phase B (a lesson turned into a rule) is unchanged.
- The pilot ran as normal trials in a throwaway experiments root instead of `--scratch`, because `--scratch` skips the verify steps (outcome O4) and records outside `bags/` (so `bag_dir_unwritable` would not be realized).
- Per-cell repetitions are a campaign key (`repetitions_where`) rather than two campaigns, so the 80 attempts share one randomized block design.
- The side check is a third `gate` level, `disabled`, rather than a flag.
- Exclusions are explicit outcome codes (X1 unclean start, X2 fault not realized, X3 other), reported and never dropped.
- **After the freeze (one change, presentation only):** `e1_table.write_figure` separates and hatches the two bars of each fault, because two bars of one color read as one wide bar. No count, test or table changed.
- The `mcap` and `pyulog` libraries are installed in the container's user site (`pip install --user mcap zstandard pyulog`, 2026-10-01). They are not in the image yet.
