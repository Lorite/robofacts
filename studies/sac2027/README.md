# Pre-registration of the SAC 2027 studies

The SAC 2027 paper "RoboFACTS: A Declarative Experiment Design Framework for Multi-Robot Trials in Simulation and on Hardware" says that for the fault, team and LLM studies we committed the hypotheses, the predicted outcomes and the analysis plan before the main runs, logged every deviation, and release these files with the code. This folder holds them, together with the protocol of the adoption study.

The studies ran in our laboratory's private repository, where the framework was developed. The files here are verbatim copies from that repository at commit `5c5c220c72` (2026-11-25). Nothing in them was edited. A `protocol.md` file holds verbatim sections of an experiment's README (the hypotheses, the pilot, the frozen predictions and the deviation log), with one comment line on top that says where they come from.

## Files, studies and dates

The commit times are those of the laboratory repository, in UTC. A file with one commit is unchanged since its freeze. A protocol with more commits received its deviation log and its results after the freeze, as its own text records.

| Folder | Study in the paper | File | Committed | First trial or model call of the main run |
|---|---|---|---|---|
| `study2_fault_injection` | Study 2, the fault study (PX4 in Gazebo, seven faults, blocking and non-blocking gate) | `predictions.yaml`, `protocol.md` | 2026-10-01 22:19 (`1493dee098`, one commit) | main run from 2026-10-01 22:41 |
| `study2_lighting` | Study 2, "Lighting" (a simulated quadrotor at four light levels, a gate check on the image brightness) | `predictions.yaml`, `protocol.md` | 2026-10-06 15:26 (`bc6d4b6727`, one commit) | calibration from 15:32, first trial 15:39 (a refused dry run at 15:18 is deviation D5 of the protocol) |
| `study2_closing_the_gaps` | Study 2, "Closing the gaps" (two simulated drones, fresh stop latch, grounded lead) | `predictions.yaml`, `protocol.md` | 2026-10-13 07:15 (`33de19f9b5`, one commit) | pilot from 07:26 in a scratch campaign, main campaign from 07:36 |
| `study3_fleet` | Study 3, the fleet campaign (5, 10 or 20 drones, one faulty agent) | `predictions.yaml`, `protocol.md` | 2026-10-15 13:46 (`cf66f3022f`, one commit) | main run from 2026-10-15 16:26, re-flight after deviation D5 on 2026-10-23 |
| `study4_llm_design` | Study 4, the LLM study | `PREREGISTRATION.md` | first freeze 2026-10-01 22:12 (`3b0bdd8756`), re-freeze 2026-10-02 01:28 (`f89f5d8a15`), deviation log and results until 07:45 (9 commits) | first pilot call 2026-10-02 01:29, first main call 01:34 |
| `study5_agent_adoption` | Study 5, the adoption study | `PREREGISTRATION.md` (run 1), `PREREGISTRATION_run2.md` (run 2) | run 1: 2026-10-13 06:50 (`dced1f3bcc`), results added 07:29. Run 2: 2026-10-15 08:26 (`5c85915a82`), results added 09:10, the package name changed in it on 2026-10-16 | run 1: the subject's first action 2026-10-13 06:51. Run 2: 2026-10-15 08:43 |

The paper names pre-registration for the fault, team and LLM studies, and says that the lighting bands were calibrated and committed before the first trial. The first campaign of Study 3 (one to three PX4 quadrotors) has no prediction file. The adoption study wrote its protocol before each run, and it is included here for completeness.

## What these files refer to

The files name scripts, folders and notes of the laboratory repository, which is not public: the tally scripts (`e1_table.py`, `fleet_tally.py`, `gaps_tally.py`, `lighting_analysis.py`), the harness, tasks, gold answers and scorer of the LLM study, result folders, design notes, and issue numbers such as `#150`. They explain how a prediction was checked or why a deviation happened. They are not needed to read the predictions, and the outcome classes are defined in each file. The framework itself is this repository: what the files call `experiments/common/` or `declarative_robot_trials` became the package `robofacts`.
