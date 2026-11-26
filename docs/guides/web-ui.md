# The web interface

`trials ui` serves a web interface for a project: the plans, the coverage grid, every trial, the gate verdicts, the runs and a stop bar. It calls the same functions as the command line, so everything it shows, `trials` can also print. If the interface can do something that the command line cannot, that is a bug.

## Install and start

The interface needs Flask, which the optional extra installs:

```bash
pip install 'robofacts[ui]'
EXPERIMENTS_UI_PASSWORD='<pick one>' trials ui
```

Then open `http://localhost:8080`. The interface refuses to start without `EXPERIMENTS_UI_PASSWORD`, unless you pass `--no-auth`. When `EXPERIMENTS_UI_USER` is set, the user name must match too.

| Option | Effect |
| --- | --- |
| `--host` | the address to bind, `127.0.0.1` by default |
| `--port` | the port, 8080 by default |
| `--no-auth` | no password (only sensible on `127.0.0.1`) |
| `--view-only` | an archive viewer that runs and stops nothing (also `TRIALS_UI_VIEW_ONLY=1`) |
| `--cache-dir` | where the poster images go when the experiments tree is read-only |
| `--experiments-root` | the project, when you start it from another directory |

## Reach it from another machine

The interface binds `127.0.0.1` on purpose. To use it from another machine, open an SSH tunnel and browse to `http://localhost:8080` there:

```bash
ssh -N -L 8080:localhost:8080 <trial-host>
```

SSH authenticates and encrypts the connection before the interface sees the password.

!!! warning "Never expose the interface to a wide network"
    The interface can start a trial, run the declared robot actions and stop robots. Do not bind `0.0.0.0`, and do not forward its port to a public or shared network. It refuses a non-localhost address without a password, and it warns about any non-localhost address. When a browser must reach it without a tunnel, bind the one address of an isolated network that only the operators use.

## The pages

Every page has the same structure: breadcrumbs from the experiments to one trial, the Journal in the top bar, and the stop bar pinned under it.

- **Experiments** (`/`): one row per experiment, the most recently run first, with the trials still missing and the last trial. Below: the robots that the stop bar reaches and their declared actions.
- **An experiment** (`/e/<exp>`): its campaigns as plans, then every trial in one table, newest first, with its cell, class, campaign, gate verdict, run log and metadata. Blocked attempts are rows too. One filter per factor works like `--where`, and the page prints the matching command. Scratch recordings have their own table.
- **A campaign** (`/e/<exp>/c/<campaign>`): wanted, covered and still missing, the blocked attempts, the gate's recent verdicts and the checks that failed most, the cell grid, and the run form.
- **A trial** (`/e/<exp>/t/<trial>`): its class and the reason, the facts, the verify report, the analyses that include it, the end of its run log, and a person's decisions with the exclude or restore form.
- **Analyses** and **Files**, two more tabs of an experiment: the declared [[analyses]] with a preview of a cohort, and a read-only view of the experiment's files.
- **Journal**: what the interface did: runs, confirmations, stops and decisions.

Each trials list is a table or, with the switch above it, cards with a still from the trial's video. `trials records <exp> --posters` makes the stills ahead of time. `/api/records/<exp>.json` serves the same records as `trials records <exp> --json`.

## Run a campaign from the browser

- The dry run button shows the plan and launches nothing.
- EXECUTE runs only when the arm checkbox next to it is ticked.
- An executed run waits before anything launches, until you press "Steps done" for the operator steps.
- With "pause before recording" ticked (the default), the run waits again after the gate passes, so that you can pose the robot.
- Live operator steps appear on the run page while the trial runs. A step that declares a `run` command has a button, and the campaign file is the only source of such commands.
- Abort ends the campaign after the current trial and never interrupts a trial in progress. To stop a robot, use the stop bar.
- The interface runs one stack at a time. A second EXECUTE is refused while a run or an exploration holds it.

A hardware trial follows the rules of [[hardware-runs]]: "skip the gate" and a campaign without a checklist are refused for it.

## The stop bar

"E-STOP ALL" and "Soft stop all" are on every page. They call `estop.sh` with the project's `robots.yaml`, and they also end the running campaign. See [[stopping-robots]].

## View-only: an archive viewer

With `--view-only`, the routes that run a trial, stop a robot, exclude or restore a trial, or run a backend action are not registered (they answer 404), and the pages show no control for them. A password is still required for every page and every download.

## With the ROS 2 backend

The [[ros2-backend|ROS 2 backend]] extends the interface:

- Each slim bag gets links that replay it in Foxglove and a download link. Each link carries a token (`?t=`) that opens that one bag only, because Foxglove cannot send the password. After a restart of the server, reload the page to get new tokens.
- An experiment's Foxglove layouts (`config/foxglove*.json`) appear beside its bags.
- A Viewing page lists every trial with a slim copy.
- A "Kill ROS" button runs the clean slate on the hosts (refused while a trial runs, unless forced).
- The Explore buttons start an [[explore|exploration]] of one condition.
- A camera topic declared in `config/trial_media.yaml` gives the cards and trial pages stills and a video from that camera (`trials records <exp> --stills`).

For live viewing through the tunnel, also forward the Foxglove bridge port: `ssh -N -L 8080:localhost:8080 -L 8765:localhost:8765 <trial-host>`.
