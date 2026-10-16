#!/usr/bin/env python3
"""Print a campaign's operator_live_steps as TSV: index<TAB>title<TAB>run<TAB>text_b64.

The in-flight handover tmux window (scripts/rig_live_steps.sh) is built from
this output: one pane per step, the step's run command pre-typed and never
executed. Steps without a run command are skipped here on purpose: they are
prose for the runner narrative, not something a pane can pre-type.

The title is the first line of the step's text, whitespace-squashed and
truncated, because it becomes a tmux pane border label. text_b64 is the FULL
step text base64-encoded, so the shell layer can display it in the pane
without any quoting hazards (the text carries quotes, braces and newlines).

``annotate()`` is the single extractor behind both surfaces: the cockpit's
panes (via ``live_steps()`` / the TSV) and the web UI's live-steps panel
import it, so which steps are runnable, what their pane-label title is and
what one-liner gets pre-typed/copied can never differ between the two.
"""
import argparse
import base64
import sys

import yaml


def annotate(steps, max_title=80):
    """The runnable view of a steps list: one dict per step that has a run command.

    Each row keeps ``index``, the step's ORIGINAL position in the campaign
    list (prose-only steps included), because that position is the allowlist
    key the UI's /operator_step endpoint executes by. ``title`` is the pane
    border label (first text line, squashed, truncated), ``cmd`` the
    whitespace-squashed one-liner a pane pre-types and a browser copies, and
    ``text`` the full untouched step text.
    """
    rows = []
    for index, step in enumerate(steps or []):
        if not isinstance(step, dict) or not step.get("run"):
            continue
        text = str(step.get("text", "")).strip()
        first_line = text.splitlines()[0] if text else ""
        rows.append({
            "index": index,
            "title": " ".join(first_line.split())[:max_title],
            "cmd": " ".join(str(step["run"]).split()),
            "text": text,
        })
    return rows


def live_steps(campaign_path, max_title=80):
    with open(campaign_path) as f:
        campaign = yaml.safe_load(f)
    rows = annotate((campaign or {}).get("operator_live_steps"), max_title)
    return [(i + 1, row["title"], row["cmd"],
             base64.b64encode(row["text"].encode()).decode())
            for i, row in enumerate(rows)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", help="path to a campaign yaml")
    parser.add_argument("--max-title", type=int, default=80)
    args = parser.parse_args()
    for idx, title, run, text_b64 in live_steps(args.campaign, args.max_title):
        print(f"{idx}\t{title}\t{run}\t{text_b64}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
