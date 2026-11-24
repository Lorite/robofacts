# Example project of the general package

A complete project for the `trials` command on the built-in `process` backend: the shared configuration under `experiments/common/config/` and one experiment, `demo_log_recorder`. Its "system under test" is a small script that appends a line to a log file many times per second. The recorder stores what the script writes during a trial, a verify step reads the recording, and the coverage index counts the trial. Nothing here needs a robot or any middleware.

```bash
cd robofacts/example_project
trials lint                                                    # every declaration against the rules
trials plan experiments/demo_log_recorder/campaigns/grid.yaml  # a grid of two factors, four cells
trials run  experiments/demo_log_recorder/campaigns/grid.yaml --limit 1             # dry run
trials run  experiments/demo_log_recorder/campaigns/grid.yaml --execute --no-pause  # the four trials
trials index --experiment demo_log_recorder --summary
```

`../quickstart.sh` runs these steps in order. A backend package ships an example of its own.
