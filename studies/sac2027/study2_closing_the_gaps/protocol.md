<!-- Verbatim sections of experiments/ros2_crazyflie_crazyswarm2_sim_team_fault_injection/README.md in the laboratory repository at commit 5c5c220c72 (2026-11-25): the hypotheses, the pilot, the frozen predictions and the deviation log. Other sections (apparatus, results, workflow) are left out. See ../README.md. -->

## Pre-registration

Frozen in one commit before the first attempt: `campaigns/lab_day_gaps.yaml` (3 levels, 5 repetitions, randomized complete blocks with seed 20261012), `config/predictions.yaml` (outcome per level and the predicates), `config/preflight_checklist.json` and `scripts/gaps_tally.py` (the outcome classes). Predictions: `latch_fresh` blocked at the gate (O1) in 5 of 5 attempts, with `stop_latch` the only failed check, a latch age under 120 s and no bag. `lead_grounded` aborted by the action's exit code 7 (O3) in 5 of 5, with every drone on the ground at the end of the bag, `cfb2` never airborne, and the lead's `flight_happened` verify step failed. `none` counted as coverage and valid (O5) in 5 of 5. Before the main run, up to three scratch pilot attempts (one per level, recorded to `bags_scratch/`, never coverage, never in the tally) check the plumbing. A fix after the pilot is a logged deviation.

## Pilot and deviations

Pilot, 2026-10-13 07:26 to 07:32 UTC, five scratch attempts in `bags_scratch/` (never coverage, never in the tally), after the pre-registration commit `33de19f9b5`:

- `latch_fresh`: blocked at the gate, `stop_latch` the only failed check, latch age 1.8 s. As designed.
- `lead_grounded`: the lead stayed at z 0.00 m, the action aborted after 6.0 s with exit 7, `cfb2` was never commanded. As designed.
- `none`: **falsely aborted** with exit 7, the lead at z 0.07 m after 6.0 s. The bag shows why: the crazyswarm2 sim server steps 0.5 ms of simulated time per Python timer callback as fast as it can, which is about 0.2 times real time on the laptop (the `/tf` stamps advance 0.1 s per 0.5 s of wall time, at a load average of 1.9, so not load). A 3 s takeoff took about 15 s of wall time, and the check measured its timeout in wall time.

**Deviation D1 (code, before any main attempt, predictions unchanged).** `confirm_airborne` now measures the timeout on the pose stream's clock, the newest header stamp of `/poses` on hardware (mocap time, about wall time) or of the drones' `/tf` in the sim (simulated time), with a 60 s wall-clock backstop for a clock that stops. Re-pilot: `none` passed both takeoff checks (the lead at 0.40 m after 2.4 s of simulated time, `cfb2` after 2.4 s) and the action completed, both drones climbed 433 mm on `/tf` in the verify step, and `lead_grounded` still aborted at 6.0 s on the pose clock.

**Known limitation, not changed (scope).** The action's other phases (stagger, hover, square legs, the wait after landing) still run on wall time, so in the slow simulator they are about five times shorter in simulated time: in the re-pilot both drones peaked at 0.43 m, not the commanded 0.5 m, before the land command. This does not affect the two checks under test. The earlier sim runs of the team action (2026-10-02, 2026-10-07) ran under the same compression.

