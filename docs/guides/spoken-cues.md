# Spoken operator cues

During a handheld or walking trial, the operator holds the robot and looks at it, not at a screen, so a cue printed in a terminal is useless at that moment. A [[campaigns|campaign]] can therefore declare spoken cues, and the [[runner]] says them at the right time and announces its own moments, such as the gate verdict.

## How a cue travels

The runner often runs in a container without audio, or on another machine. So it sends each cue as one UDP datagram to a small voice daemon on the machine with the speaker, which speaks it.

- One datagram carries one cue as UTF-8 text, at most 200 characters. The sender removes control characters and cuts a longer text at a word boundary.
- Sending never blocks and never raises. A daemon that is down costs one lost datagram, and the trial continues.
- Each cue is also printed, so the run log shows every cue.

The environment sets the target:

| Variable | Meaning |
| --- | --- |
| `TRIALS_VOICE_HOST` | where the daemon listens, default `127.0.0.1` |
| `TRIALS_VOICE_PORT` | its UDP port, default 47900 |
| `TRIALS_VOICE` | `off` (or `0`, `false`, `no`) prints only, `on` forces sending |

A pytest run sends nothing unless `TRIALS_VOICE=on`. `trials run --no-voice` prints the cues and sends none.

## The voice daemon

The package ships the sending side, `robofacts.operator_voice`, and not the daemon, because the speech tool depends on the machine. The daemon is a small program that does three things:

- It listens on the UDP port.
- It speaks the text of each datagram with the machine's speech tool, for example `spd-say` on Linux.
- It answers a ping: a datagram of one zero byte followed by `ping` gets the reply of one zero byte followed by `pong`.

A good daemon also speaks one cue at a time, in order of arrival, drops a cue that waited too long, and accepts datagrams only from the private networks that the runner uses.

Test the path from the machine and the container that run the runner:

```bash
python3 -m robofacts.operator_voice --check                       # a silent ping, exit 0 when the daemon answers
python3 -m robofacts.operator_voice "Voice cues work from here."   # speaks one sentence
```

Before an executed run that has something to say, the runner prints where the cues go and pings the daemon for at most 1 second. When nobody answers, it warns once, and the cues are only printed. A missing voice never refuses a trial.

## What a campaign declares

```yaml
voice_cues:                     # a timeline relative to the start of the action
  - {at_s: 0,   text: "Recording. Keep still."}
  - {at_s: 8,   text: "Pick up and walk."}
  - {at_s: 88,  text: "Back on the stand."}
  - {at_s: 101, text: "Done. Thank you."}
voice_announce:                 # the runner's own lines at fixed moments
  before_launch: "Put the robot on the stand. Recording starts in about twenty seconds."
```

The runner starts the recorder, waits 1 second, and then starts the cue timeline together with the action. A `{factor}` placeholder in a text is filled with the trial's level, as in the operator steps.

The moments of `voice_announce` are `before_launch`, `record_start`, `record_end`, `gate_blocked` and `gate_passed`.

## The defaults

A hardware experiment (one whose `runner.yaml` does not say `simulation: true`) speaks these lines without any declaration:

| Moment | Default line |
| --- | --- |
| `record_start` | "Recording." |
| `record_end` | "Recording stopped." |
| `gate_blocked` | "The gate says no go. Nothing was recorded.", then up to three failed checks |
| `gate_passed` | "The gate passed. Type fly when you are ready." |

Without the `fly` pause (`--no-pause`), the GO line is only "The gate passed.". A shadow gate's recorded NO-GO is not announced as a pass. A campaign cue at 0 seconds replaces the default "Recording.". The sentence of a failed check comes from the checklist entry's `"speak"`, or from a built-in sentence per check.

A simulated experiment says nothing unless its campaign asks. `voice_announce: false` silences every moment, and a moment set to `null` silences that one.

## Timing rules

- The runner stops the timeline when the action ends. It reports a cue that it did not reach as not spoken, in the run log and in the attempt ledger, `runs/attempts.jsonl`.
- Speaking never changes the operator steps or the [[gate|T-0 gate]].
- A dry run prints the voice plan and sends nothing.
- Lint rules L10 and L15 check the cues: well formed, with placeholders that name declared factors, and inside the action's time limit. See [[lint-rules]].
