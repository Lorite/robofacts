# cflib_hover

One Crazyflie hovers on its Flow deck for 10 s, commanded with [cflib](https://github.com/bitcraze/crazyflie-lib-python), without any middleware. The experiment runs on the `process` backend of robofacts: there is no stack, the action holds the radio link for the whole flight, and the recorder stores the flight log the action writes.

| file | says |
|---|---|
| `config/drone.yaml` | the drone's address, the refusal thresholds, the takeoff stages, the geofence and the landing, one file for every script |
| `config/runner.yaml` | the action (`scripts/cf_hover.py`), the flight log as the record source, the verify step, the drone's reset |
| `config/preflight_checklist.json` | the T-0 gate: the stop latch, the battery and the Flow deck over the radio, two operator confirmations |
| `config/factors.yaml`, `config/trial_metadata_schema.yaml` | one factor, the hover time |
| `campaigns/baseline.yaml` | one 10 s hover |
| `../common/config/robots.yaml` | the stop commands (they write the stop latch, the action lands or cuts the motors) and the reset (a power cycle over the radio) |
| `../common/preflight/checks/` | the two gate checks of this project, thin wrappers of `scripts/cf_link.py` |

The scripts:

- `scripts/cf_hover.py`, the action. It refuses before arming on a fresh stop latch, a missing Flow deck, a low battery, an estimator that does not settle, or a ground position away from the origin. It takes off in two stages (flow off on the floor, 0.20 m in 1.0 s, flow back on at 0.10 m, then to 0.50 m), holds, and lands on the end of the hover, on Ctrl+C or SIGTERM, on the stop latch, when the estimate leaves the geofence, and on any error. A latch whose last line starts with `estop` cuts the motors.
- `scripts/cf_link.py`, read-only questions for the gate (battery, Flow deck) and the reset. It never arms.
- `scripts/verify_hover.py`, the verify step: the recorded height (the range sensor) was above 0.15 m for at least 3 s, and the recording ends on the ground.
- `scripts/cflib_drone.py`, everything that touches the radio, behind a small interface that the tests replace with a fake drone.

Without a drone (no radio is contacted, see `../../test/test_cflib_hover.py`):

```bash
pip install ./robofacts pytest            # from the repository root
cd examples/cflib_hover
trials lint
trials run experiments/cflib_hover/campaigns/baseline.yaml --limit 1     # dry run
python -m pytest -q test
```

With a drone: `pip install cflib`, plug in a Crazyradio, set `uri` in `config/drone.yaml` (or `export CF_URI=...`), and follow the operator procedure below.

## Operator procedure

1. The drone stands on its mark on a flat, textured floor, nobody within 1.5 m. No other program holds the Crazyradio.
2. Terminal 1, in `examples/cflib_hover`: `trials gate --checklist experiments/cflib_hover/config/preflight_checklist.json`. Expect four PASS lines (stop latch, battery, Flow deck, recording directory) and the two operator confirmations. Nothing arms.
3. Terminal 2, in `examples/cflib_hover`: type `trials tool estop.sh --soft` without Enter, so a landing is one key away. `trials tool estop.sh --list` shows what each stop does.
4. Terminal 1: `trials run experiments/cflib_hover/campaigns/baseline.yaml --limit 1 --execute`. The runner power-cycles the drone, runs the gate, starts the recorder and asks you to type `fly`. The drone takes off to 0.5 m, holds 10 s and lands. The verify step prints `PASS: airborne ... landed`.
5. To stop: Ctrl+C in terminal 1, or Enter in terminal 2 (`--soft`): the drone lands. `trials tool estop.sh` without a flag is the emergency: it cuts the motors and the drone falls. The physical stop you use for the drone stays the primary one.
6. After a stop, the next trial is refused for 120 s. When the drone is safe: `rm /tmp/trial_stop_latch`.
7. `trials index --experiment cflib_hover --summary` lists the trial. A stopped or refused trial does not count.

When the radio link is lost in the air, the drone keeps hovering on its last setpoint (the high-level commander of the firmware). cflib can arm the firmware's emergency-stop watchdog, which cuts the motors 1 s after the last message. This example does not use it.
