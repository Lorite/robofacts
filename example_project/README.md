# Example project

A complete project for the `trials` command: the shared configuration under `experiments/common/config/` and one experiment without a robot, `demo_talker_listener`, whose "robot" is `demo_nodes_cpp talker`. It is what the package's tests and the quickstart run.

```bash
cd example_project
trials lint                                                   # every declaration against the rules
trials plan experiments/demo_talker_listener/campaigns/rates.yaml
trials run  experiments/demo_talker_listener/campaigns/rates.yaml --limit 1             # dry run
trials run  experiments/demo_talker_listener/campaigns/rates.yaml --execute --no-pause  # the four trials
trials index --experiment demo_talker_listener --summary
```
