# Coverage and records

Coverage answers one question: which trials do you already have, and which are still missing? This page explains which trials count, and the three commands that show what is wanted, covered and missing.

## Which trials count

Every recorded trial falls into one of three classes:

| Class | Meaning | Counts? |
| --- | --- | --- |
| **usable** | it satisfies the metadata contract, it was not aborted, and no person excluded it | yes |
| **unverified** | its metadata breaks the [[metadata-contract|metadata contract]], so it cannot prove which cell it belongs to | no |
| **excluded** | it was aborted, a person quarantined it (see [[decisions|A person's decision about a trial]]), or its recording holds no data | no |

Only usable trials are coverage. An aborted or contract-violating recording never quietly fills a cell. The index keeps the classes apart on purpose: a cell with 20 recordings that are all aborted is worse than a cell with none, because it looks full. Many unverified trials simply predate a field of the contract, so `--include-unverified` (on `trials plan`, `trials run` and `trials analyze`) counts them too, and the output says so.

Scratch recordings never count, and the index never reads their folder (see [[scratch-trials|Scratch trials]]).

## What a campaign still needs: trials plan

```bash
trials plan experiments/demo_log_recorder/campaigns/grid.yaml
```

```text
Campaign: grid  (demo_log_recorder)

  wanted 4 · covered 2 · STILL MISSING 2
  (1 matching trial(s) are unverified and NOT counted; pass --include-unverified to count them)

  (* marks a non-default value)

  STILL MISSING (2 cell(s)):
    x1  duration=long* execution_host=laptop rate=fast* record_profile=custom   [none yet, want 1]
    x1  duration=long* execution_host=laptop rate=slow record_profile=custom   [have 0u/1un, want 1]

  COVERED (2 cell(s)):
    ok   duration=short execution_host=laptop rate=fast* record_profile=custom   [have 1 usable]
    ok   duration=short execution_host=laptop rate=slow record_profile=custom   [have 1 usable]
```

- **wanted**: the trials the [[campaigns|campaign]] asks for, cells times repetitions.
- **covered**: the usable trials that match those cells, up to the number each cell wants.
- **still missing**: the difference, per cell. `x1` is the number still needed, and `have 0u/1un` counts the usable and the unverified trials already there.

A trial matches a cell through its recorded levels, not through the campaign that ran it. Every factor with a metadata key takes part, and a factor the campaign does not name must be at its default. So a campaign pinned to one computer is never satisfied by trials from another, and two campaigns that ask for the same cell share its trials. Older spellings of a level match through the factor's `aliases`. `--json` prints the plan for a program, and `--schedule` the order the runner will use.

## Every trial grouped: trials index

```bash
trials index --experiment demo_log_recorder
```

`trials index` groups the recorded trials by the coverage axes (the contract's `factor: true` fields) and counts each class per combination. It prints Markdown:

```text
## demo_log_recorder

4 trial(s). Axes: `duration`, `execution_host`, `rate`

| duration | execution_host | rate | usable | unverified | excluded | total |
|---|---|---|---|---|---|---|
| long | — | slow | 0 | 1 | 0 | 1 | _(all unverified — metadata gap, not necessarily bad data)_
| short | laptop | fast | 1 | 0 | 0 | 1 |
| short | laptop | slow | 1 | 0 | 0 | 1 |
| long | laptop | fast | 0 | 0 | 1 | 1 | **nothing usable here**

⚠️ 1 trial(s) violate the metadata contract — `execution_host` ×1
```

It also lists the combinations of levels already in use that have no trial at all. Useful options:

- `--summary`: only what varies per experiment, with each axis and its levels.
- `--factors a,b`: the axes of the grid, instead of the contract's.
- `--report <file>`: write the report to a Markdown file.
- `--condition-space <yaml>`: one condition across experiments, per level and per domain (simulation or hardware). By default, it counts a usable trial for a level only when its gate confirmed that level with a passing factor check and no override.

Without `--experiment`, the index covers every experiment of the project.

## Everything about each trial: trials records

```bash
trials records demo_log_recorder
```

```text
2026-01-01 10:03:00 UTC  unverified          20260101T100300_duration-long_rep01
      why: execution_host: required but missing — Which machine actually ran it. [...]
2026-01-01 10:02:00 UTC  excluded (aborted)  20260101T100200_duration-long_rate-fast_rep01
      why: verify step log_was_recorded failed (rc=1)
2026-01-01 10:01:00 UTC  usable              20260101T100100_rate-fast_rep01
2026-01-01 10:00:00 UTC  usable              20260101T100000_rep01
4 record(s): 2 usable, 1 unverified, 1 excluded (1 aborted)
```

`trials records` lists every trial of an experiment, newest first, with its class and the reason why it does not count. It joins what lies in several places: the recording and its metadata, the campaign that ran it, the gate verdict, the run log, the analyses that used it, and a person's decisions. It also lists attempts that left no trial: a **blocked** attempt (the gate said NO-GO), a **failed** one, or an **interrupted** one.

Filters:

- `--campaign <name>`: the trials that campaign ran or counts,
- `--status <status>`: a class (`usable`, `unverified`, `excluded`), the reason of an excluded trial (`aborted`, `quarantined`, `no-data`, `scratch`), an attempt outcome (`blocked`, `failed`, `interrupted`), `attempt` for every attempt without a trial, or `excluded-any` for everything that does not count,
- `--where factor=value`: one cell, repeatable,
- `--no-scratch` or `--scratch-only`,
- `--json`: the records for a program.

The web interface shows the same records (see [[web-ui|The web interface]]), and so does the MCP server's `list_trials` tool (see [[mcp-server|The MCP server for agents]]).

## The domain of a trial

Every trial records its domain, `simulation` or `hardware`, from `simulation:` in `runner.yaml`, unless a factor's `domain:` map decides it for some levels. `trials index --condition-space` counts the two domains apart, so you can see which conditions you have tested only in simulation.
