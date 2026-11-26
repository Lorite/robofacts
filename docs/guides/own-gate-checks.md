# Write your own gate checks

The [[gate|T-0 gate]] runs machine checks seconds before every trial. The package and its [[backends|backend]] ship general checks, and a project adds the checks that only it knows. This page shows the protocol of a check, where the gate finds it, and how to add one to a checklist.

## The protocol of a check

A check is a bash script named `<name>.sh`. It takes `key=value` arguments, prints one line that starts with `PASS:`, `FAIL:` or `SKIP:`, and exits with the matching code:

| Exit code | Meaning |
| --- | --- |
| `0` | PASS: verified good, now |
| `1` | FAIL: verified bad, or not verifiable |
| `2` | usage error (bad arguments), which is also a NO-GO |
| `3` | SKIP: not applicable to this experiment |

Three rules make a check trustworthy:

- **Measure now.** Sample fresh data when the check runs. A value read minutes ago proves nothing about the trial.
- **A process that runs is not evidence.** Measure the data that actually flows, for example the rate of a topic or the growth of a log file, not the presence of a process.
- **A value that cannot be read is a FAIL, never a SKIP.** Silence must never pass. Use SKIP only when the check does not apply to the experiment at all.

## Where the gate finds a check

The gate looks for `<name>.sh` in this order:

1. the project's own checks, `<experiments root>/common/preflight/checks/`
2. the checks of the enabled [[robot-adapters|robot adapters]]
3. the checks of the project's backend
4. the general checks of the package

A project check with the same name as a packaged one wins. [[gate-checks]] lists the packaged checks.

## An example

This check passes when the log file of the system under test grows during a short window. It goes into `experiments/common/preflight/checks/log_grows.sh`:

```bash
#!/usr/bin/env bash
# The log file of the system under test grows: the system writes now.
#
#   log_grows.sh path=<file> [window_s=2]
source "$PF_LIB"
PF_HELP="log_grows.sh path=<file> [window_s=2]"
pf_parse_args "$@"
pf_require_args path

FILE="$(pf_arg path)"
WINDOW="$(pf_arg window_s 2)"

[ -f "$FILE" ] || pf_fail "no file at '$FILE': the system has not started"
BEFORE="$(stat -c %s "$FILE")" || pf_fail "cannot read the size of '$FILE'"
sleep "$WINDOW"
AFTER="$(stat -c %s "$FILE")" || pf_fail "cannot read the size of '$FILE'"

[ "$AFTER" -gt "$BEFORE" ] && pf_pass "'$FILE' grew by $((AFTER - BEFORE)) bytes in ${WINDOW} s"
pf_fail "'$FILE' did not grow in ${WINDOW} s: the system writes nothing"
```

The gate passes the helper file as `$PF_LIB`: the package's `lib/common.sh`, or the superset of the project's backend. The helpers used here:

| Helper | Does |
| --- | --- |
| `pf_parse_args "$@"` | reads the `key=value` arguments, and prints `PF_HELP` for `--help` |
| `pf_require_args a b` | a usage error (exit 2) unless every named argument is given. Call it in the main shell, before `pf_arg` |
| `pf_arg key [default]` | the value of an argument, or the default |
| `pf_pass`, `pf_fail`, `pf_skip` | print the result line and exit with its code |
| `pf_ge a b` | true when the number `a` is at least `b` |

## Add it to a checklist

A checklist entry names the check and its arguments. Argument values are strings:

```json
{"text": "[HARD] The system writes its log now.", "check": "log_grows", "args": {"path": "experiments/my_experiment/runs/system.log", "window_s": "2"}}
```

- A text that starts with `[HARD]`, or `"hard": true`, makes a failure that no override passes.
- `${name}` in an argument is filled from the checklist's `variables`, from `--var name=value`, and from the trial's factor levels.
- `"stage": "pre_launch"` runs the entry before the stack starts.
- `"speak"` is the sentence that the voice says when the entry fails (see [[spoken-cues]]).

[[checklist-json]] describes every key.

## Run alone or in a batch

The gate runs consecutive checks that are read-only probes together, in one batch, and a check that must run alone forms a batch of its own. A check that nobody classified runs alone. When your check only reads (it starts, stops or signals no process, and shares no file with another check), list it in `trials.yaml`:

```yaml
parallel_safe_checks: [log_grows]
```

`"parallel": false` on one checklist entry makes that entry run alone. The report lines, their order and the verdict are the same in both modes.

## Test a check

Run the check through the gate, which sets `$PF_LIB` for you:

```bash
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json --list              # the plan, nothing runs
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json --validate          # is the checklist coherent
trials gate --checklist experiments/my_experiment/config/preflight_checklist.json --only log_grows    # run one check
```

`--list` also shows which entries run alone. Run the check once against a healthy system and once against a broken one: a check that has never failed has not been tested.
