# The T-0 gate: one script per check, one checklist per experiment

A gate runs seconds before every trial. It reads the experiment's checklist (`config/preflight_checklist.json`), runs the machine checks, asks the operator to confirm the manual entries, and returns one verdict. A gate that cannot run has not passed.

## The protocol of a check

A check is a bash script named `<name>.sh`. It takes `key=value` arguments, prints one line that starts with `PASS:`, `FAIL:` or `SKIP:`, and exits with the matching code:

| Exit code | Meaning |
|---|---|
| `0` | PASS: verified good, now |
| `1` | FAIL: verified bad, or not verifiable. A value that cannot be read is a FAIL, never a skip |
| `2` | usage error (bad arguments), which is also a no-go |
| `3` | SKIP: not applicable to this experiment |

A check sources the helper file that the gate passes as `$PF_LIB` (`lib/common.sh` here, or the superset of the project's backend). The helpers parse the arguments and print the result line.

## Where a check is found

In this order: the project's own checks (`<experiments root>/common/preflight/checks/`), the enabled robot adapters, the project's backend, then the general checks of this package. A project check with the same name as a packaged one wins.

## The general checks

| Check | Verifies |
|---|---|
| `address_on_interface` | an IP address is carried by the network interface that the experiment expects |
| `bag_dir_writable` | the recorder can write the recording directory (a write probe, and a directory that does not exist yet passes when its nearest parent is writable) |
| `disk_space` | enough free space for the recording |
| `file_exists` | a required file is present, and large enough |
| `gpu_memory` | enough free GPU memory |
| `host_reachable` | another machine answers, and the link is not degraded |
| `mem_available` | enough free memory on the recording host, now |
| `stop_latch` | no operator stop latch younger than 120 s |

A backend adds the checks of its middleware. They are documented in the backend's package.

## A checklist entry

A plain string is a manual entry that the operator confirms. An object names the script that verifies it:

```json
{"text": "[HARD] Enough disk for the recording.", "check": "disk_space", "args": {"path": ".", "min_gb": "5"}}
```

A text that starts with `[HARD]` cannot be overridden by the operator. `${name}` in an argument is filled from the checklist's `variables`, from `--var name=value` and from the trial's factor values. `"stage": "pre_launch"` runs an entry before the stack starts. `"speak"` is the sentence the voice says when the entry fails.

## Concurrent machine checks

The gate runs consecutive checks that are read-only probes in one batch (`--jobs`, at most 8 at once). A check that must run alone forms a batch of its own, at its place in the checklist. Which checks may share a batch is decided per script: the sets `PARALLEL_SAFE_CHECKS` and `SEQUENTIAL_CHECKS` in `run_preflight.py`, the same sets of the backend and of each adapter, and `parallel_safe_checks:` in `trials.yaml` for a project's own checks. A check that nobody classified runs alone. The report lines, their order and the verdict are the same in both modes (`--concurrency sequential|concurrent`).

## Commands

```bash
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json --list      # the plan, nothing runs
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json --validate  # is the checklist coherent
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json             # run it
```
