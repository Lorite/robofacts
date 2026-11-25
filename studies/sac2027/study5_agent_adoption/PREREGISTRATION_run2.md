# Amendment: run 2 of the adoption test, on the current framework (2026-10-15)

Written and committed before the run-2 subject starts. Run 1 (`PREREGISTRATION.md`, results in `results/`) is unchanged and stays the record of the first run.

## Why a second run

Run 1 (2026-10-13, framework `ea4106d29b`) reached full coverage in 35.4 min and 94 tool calls, and it found six framework defects. Since then, four of the six defects were fixed (`4c8a577e34`), the framework became the package `robofacts`, and on 2026-10-14 the package gained `trials new <name> --from <existing>` (a renamed copy of the nearest experiment) and `trials validate` (one READY or NOT READY verdict in seconds), with a "fast path" section in the guide. Run 2 asks the same question of the framework as a newcomer meets it today. It does not replace run 1: the defects run 1 found are a result in their own right.

## Freeze point

| item | value |
|---|---|
| framework snapshot (`framework_ref`) | `befa9c0d3c` (main HEAD before this amendment, package submodule `4bb9cfb`) |
| subject branch and worktree | `study/agent-adoption-spot-run2`, `.worktrees/adoption-subject-run2` (created from `framework_ref`) |
| prompt | `subject_prompt_run2.md` |
| analysis | `analysis/extract_measures.py`, unchanged |
| results | `results/run2/` |

On the subject's branch, one commit removes `experiments/agent_adoption_study/` before the start, because `framework_ref` already contains run 1's study folder and results.

## What is unchanged

The question and framing (Section 2 of `PREREGISTRATION.md`), the subject kind (one fresh general-purpose Claude Code subagent, model inherited and read from the transcript), the task verbatim, the stop rule (full coverage or 3 h), the success criterion, the measures M1 to M7 and their rules, and the analysis (descriptive only, n = 1).

## What changes, and why

- The prompt's worktree, branch, `ROS_DOMAIN_ID=64` and `GZ_PARTITION=adoption64` (run 1's leftovers cannot interfere), and the location of the framework code (now the package). Nothing else in the prompt changes.
- The robot environment is the same: the Harmonic overlay `/home/vscode/adoption_overlay_ws` from run 1 is reused unchanged.

## Feasibility check (orchestrator, zero subject calls)

In the run-2 worktree, inside `ros2_humble_dev` with the subject's setup line: `ros2 run robofacts trials new zz_feas --from px4_sitl_offboard_demo` created the copy, and `trials validate experiments/zz_feas` printed READY in 0.6 s. The folder was deleted before the freeze. The `trials` shell function does not exist in a non-interactive `zsh -lc`, and the guide names `ros2 run robofacts trials` as the alternative. The subject is not told this.

## Prediction (descriptive, not a hypothesis test)

Full coverage with zero interventions, with a wall time below run 1's 35.4 min. The simulator time per trial is unchanged, so the floor is the time of four or more simulated trials. Known confounds, reported with the result: the framework differs in more than the two new commands (four defect fixes, the package), and a different subagent instance makes different choices. A difference between two runs with n = 1 each is a demonstration, not an effect estimate.

## Deviation log

1. **Attempt A discarded (2026-10-15).** The first subject started at 08:26:20 UTC. The orchestrating session crashed at about 08:41 UTC, which stopped the subject (last transcript timestamp 08:41:13 UTC, 239 transcript lines). It had written its declarations, a walker script and a README in `experiments/spot_sim_square_walk/`, uncommitted, and had a probe simulation running (domain 64, partition `adoption64`). It had run no trial. An interruption by the orchestrator is not a measurement of the framework, and resuming the subject would count as an intervention and add the crash gap to its wall time. So the orchestrator stopped the subject's six processes by PID, removed the uncommitted folder (a copy is kept outside the repository), and started a new fresh subject (attempt B) with the same prompt and a new start time. Attempt B is the run that is reported. Attempt A is reported only as this entry.
2. **Inputs (minor), as in run 1.** The subject ran `git log --oneline -1` and `-3` on its own branch after its commits, which also shows the subject lines of `53a5e3389d` and `befa9c0d3c`. No other history was read. It kept helper scripts and runner logs in the orchestrating session's scratch directory, as in run 1. It ran the package from its worktree through `PYTHONPATH` (the same commit as the installed package). Compliance was checked by a pattern scan of every tool input of the transcript.
3. **Measurement of motion (addition, as in run 1).** The orchestrator measured the motion from `/model/spot/pose` in each bag.
