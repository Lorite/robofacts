# Pre-registration: automated adoption test with a legged robot

SAC 2027 experiments-framework paper. This file, `study.yaml`, `subject_prompt.md` and `analysis/extract_measures.py` are frozen together before the subject starts.

## 1. Freeze point

| item | value |
|---|---|
| freeze commit (this file, study.yaml, subject_prompt.md, analysis/) | `dced1f3bccab527ab5ab4ba933c6ad5ab482e0a3` |
| framework snapshot the subject works on (`framework_ref`) | `ea4106d29b` (main HEAD just before the first pre-registration commit) |
| subject branch and worktree | `study/agent-adoption-spot`, `.worktrees/adoption-subject` (created from `framework_ref`) |
| prepared on | 2026-10-13, before the subject's first tool call |

The freeze is two commits: the first adds the frozen files, the second writes the first one's hash into `study.yaml` and into this table. After the subject starts, only the hash rows and the deviation log (Section 8) may change.

The subject's branch starts at `framework_ref`, not at the freeze commit. So the subject cannot see this study folder, and framework changes that other agents commit to `main` during the run do not move the goalposts.

## 2. Question and framing

Can a newcomer who has only the framework's documentation declare a new robot type as a framework experiment and run a small campaign to verified trials, and what do the framework's tools (the configuration linter, the campaign checker, the planner, the T-0 gate and the verify steps) do for the newcomer on the way?

**This is a proxy for adoption, not a user study.** The subject is one LLM coding agent (n = 1), not a human. No human participant took part, because none could be recruited. The result shows that the documentation and the tooling are sufficient for one capable automated newcomer on one robot, and it shows which refusals a newcomer meets. It does not measure human usability, learnability or satisfaction, and it does not generalize to other agents or models. The second purpose is generality: the robot is a legged robot, while every earlier experiment of the paper uses aerial vehicles.

## 3. Subject and inputs

- **Subject:** one fresh general-purpose Claude Code subagent, spawned once by the orchestrating agent, with no context except the prompt. The model is inherited from the orchestrating session, and the exact model identifier is read from the transcript (`message.model`).
- **Prompt:** `subject_prompt.md`, sent verbatim as the only message. The only run-time substitution is the start timestamp.
- **Allowed inputs:** the user guide `docs/development/experiments_framework_guide.md`, `experiments/AGENTS.md`, every file these two point to (the framework code under `experiments/common/` and the current declaration files of existing experiments, because the guide tells the reader to copy the nearest campaign), the robot packages (`champ_bringup` in `ros2_ws/src/spot_ros2_ign/`, `spot_description`), the live simulation, and vendor documentation on the web.
- **Forbidden inputs:** git history, the recorded data and outputs of other experiments (`bags*/`, `results/`, `runs/`, notebooks), `experiments/llm_design_study/`, the Obsidian vault, other repositories, and subagents.
- **Environment facts given to the subject** (lab facts, not framework help): the container name and the `docker exec` form, the one-line shell setup (ROS, the workspace, the Harmonic overlay of `gz_ros2_control`, `ROS_DOMAIN_ID=63`, `GZ_PARTITION=adoption63`, no `DISPLAY`), and the lab rules (simulation only, headless, no pattern kills in the shared container, write only the own experiment folder, commit on the own branch).

## 4. Task and stop rule

Task, verbatim: "Declare an experiment for Spot in simulation (package `champ_bringup`, launch file `spot_bringup.launch.py`) that walks a 1 m square, with a factor `speed` of two levels and 2 repetitions per level, with a verify step that checks from the recording that the robot moved, then run the campaign until the coverage index shows every cell covered by verified trials."

Expected result: 2 cells, 4 usable trials.

Stop rule: the subject stops at full coverage (the planner shows nothing still to run, counting usable trials only) or 3 hours of wall time after its start, whichever comes first. The orchestrator does not stop the subject earlier, does not answer it, and does not change its environment while it runs.

**Success** is full coverage within 3 hours with zero human interventions.

## 5. Measures

| id | measure | source and rule |
|---|---|---|
| M1 | wall time | first to last transcript timestamp of the subject, in minutes |
| M2 | tool calls | number of `tool_use` blocks in the subject transcript, by tool and by category (a framework component, simulator or ROS, other) |
| M3 | framework refusals | every executed call of the linter, `campaign_spec.py`, `campaign_plan.py`, `trial_runner.py` (configuration refusal, T-0 gate NO-GO, verify step failure), `run_preflight.py` or `trial_index.py` that exits non-zero or prints a refusal marker. `analysis/extract_measures.py` lists the candidates mechanically. Each is then classified by hand, with its verbatim message, into: **caught a real mistake** (the subject's declaration or script was wrong, and the subject changed it to address the message), **false alarm** (the subject's declaration was correct, and the refusal came from the framework or its environment), or **not a refusal** (a marker matched without a refusal). The classification is checked against the subject's next edits and against its own report, and disagreements are reported. |
| M4 | lines of declarations | lines and non-blank non-comment lines of the subject's `config/*` and `campaigns/*` files. Lines of the subject's `scripts/` are reported separately |
| M5 | trials | attempts in `runs/attempts.jsonl`, scratch bags, gate verdicts in `runs/preflight_gate.jsonl`, usable, unverified and excluded trials from `campaign_plan.py` and `trial_index.py` on the subject's worktree at the end |
| M6 | human interventions | every message or action by a human or by the orchestrator in the subject's environment during the run. Target zero |
| M7 | blockers outside the framework | every problem with the simulator, the container, ROS or the environment, listed separately from M3 and never counted as a framework refusal |

Secondary, reported but not tested: the time to the first passing dry run, the time to the first usable trial, and the guide gaps the subject names.

## 6. Analysis

Descriptive only: n = 1 permits no inference. The report gives M1 to M7, one paper paragraph and one table row. The paragraph names the model and says that the subject was an LLM agent and that this is not a human usability study.

## 7. Feasibility check before the freeze (zero subject calls)

Done by the orchestrator on 2026-10-13, about 40 minutes:

- `champ_bringup spot_bringup.launch.py` with `world_file:="<ground_plane.sdf> -s --headless-rendering"`, `use_rviz:=False`, arm off, starts a headless Gazebo Harmonic 8.11 server. The CHAMP `quadruped_controller` takes `geometry_msgs/Twist` on `/vox_nav/cmd_vel`. The ground-truth pose `/model/spot/pose` (`TransformStamped`) runs at about 29 Hz, and `/ground_truth_odom` (`nav_msgs/Odometry`) also exists. A 0.3 m/s forward command for 8 s moved the body 1.74 m, a lateral command and a yaw command also moved it.
- One environment defect, fixed before the freeze and outside the framework: the shared build of `gz_ros2_control` targets Gazebo Fortress (`GzPluginHook` missing under Harmonic), so no controller started. The orchestrator rebuilt it with `GZ_VERSION=harmonic` into a container-local overlay, `/home/vscode/adoption_overlay_ws`, leaving the shared install unchanged.
- Observed and not fixed: stopping `ros2 launch` with SIGINT leaves the `gz sim` server running. The subject is not told this.
- Not chosen: Webots Spot (`webots_spot spot_launch.py`) needs an X display, and the container has no Xvfb.

## 8. Deviation log

1. **M3 extraction (2026-10-13, after the run).** `extract_measures.py` flags refusals from the tool output in the transcript. The subject redirected every `trial_runner.py --execute` run into a log file, so the transcript never showed the gate's NO-GO, and the mechanical candidate list was empty for the one real refusal. M3 was completed from the sources M5 already names: the subject's `runs/preflight_gate.jsonl` and `runs/attempts.jsonl` (copied to `results/subject_*.jsonl`) and the runner logs. The two mechanical candidates were "not a refusal" (a `0 blocked, 0 failed` tally of the dry run and the coverage legend of `trial_index.py`).
2. **Inputs (minor).** The subject ran `git log --oneline -3` on its own branch to confirm its commits. The output also shows the subject line of `framework_ref`, which is one line of git history. No other history was read. The subject wrote its runner logs into the session's temporary scratch directory, which the orchestrator shares. Not a rule violation.
3. **Measurement of motion (addition).** The orchestrator checked the motion independently from `/model/spot/pose` in each bag (the subject's verify step reads `/ground_truth_odom`), to confirm that the verify step's PASS is true.
