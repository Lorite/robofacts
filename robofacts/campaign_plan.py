#!/usr/bin/env python3
"""The planner: which cells of a campaign are still missing (phase 3).

Joins the Phase 2 campaign (what we *want*) to the Phase 1 index (what we
*have*) and answers the operational question directly: **you already have 6 of
these, run the other 12.** The words are the SAC 2027 paper's (2026-10-16): a
campaign is *wanted*, *covered* and *still missing* per cell.

The join
--------
A planned combination is keyed by factor *name*; a recorded trial is keyed by
the ``metadata_key`` that factor declares. Matching translates between them, so
renaming a factor without renaming its metadata key does not silently orphan a
campaign's history. Values compare as strings, because JSON round-trips lose the
distinction between ``720`` and ``"720"`` and a coverage count must not.

What counts as already covered
------------------------------
Only **usable** trials, by default — the Phase 1 three-state split exists
precisely so an aborted or contract-violating bag cannot quietly satisfy a cell.
``--include-unverified`` relaxes that for pre-contract data, and says so in the
output rather than blurring the two.

A factor that is *fixed* rather than swept still participates in the match: a
campaign pinned to ``execution_host=rig`` must not be satisfied by bags flown
on the laptop.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from . import campaign_spec as cs
from . import trial_index as ti
from . import roots


def __getattr__(name):
    """``EXPERIMENTS_ROOT`` and ``REPO_ROOT`` for callers of the old constants, resolved on access."""
    return roots.module_root_attribute(name)


@dataclass
class PlannedCell:
    """One combination of the campaign, with what already exists against it."""
    values: Dict[str, Any]
    non_defaults: List[str]
    wanted: int
    usable: int = 0
    unverified: int = 0
    excluded: int = 0
    matched_bags: List[str] = field(default_factory=list)
    #: Matched bags whose cell comes from a person's correction (corrections.json,
    #: `trials amend`), not from what the runner recorded.
    corrected_bags: List[str] = field(default_factory=list)

    @property
    def covered(self) -> int:
        return self.usable

    @property
    def recorded(self) -> int:
        """Every recorded trial of this cell, whatever its status.

        The repetition numbers of the trials still to fly continue after these
        (2026-10-13): numbering restarted at 1 in every runner invocation, so
        two trials of one cell flown on different days both said
        ``repetition: 1`` (the adoption test of the SAC 2027 framework paper).
        A blocked attempt records no bag and is not counted, so its retry gets
        the same number.
        """
        return self.usable + self.unverified + self.excluded

    def needed(self, include_unverified: bool = False) -> int:
        have = self.usable + (self.unverified if include_unverified else 0)
        return max(0, self.wanted - have)

    def describe(self) -> str:
        return " ".join(f"{k}={self.values[k]}{'*' if k in self.non_defaults else ''}"
                        for k in sorted(self.values))


@dataclass
class Plan:
    campaign: cs.Campaign
    cells: List[PlannedCell]
    include_unverified: bool = False
    unmatchable_factors: List[str] = field(default_factory=list)

    @property
    def wanted(self) -> int:
        return sum(c.wanted for c in self.cells)

    @property
    def needed(self) -> int:
        return sum(c.needed(self.include_unverified) for c in self.cells)

    @property
    def covered(self) -> int:
        return self.wanted - self.needed

    def to_dict(self) -> Dict[str, Any]:
        """Machine-readable plan, for the runner (Phase 4) and the UI (Phase 5)."""
        return {
            "campaign": self.campaign.name,
            "experiment": self.campaign.experiment,
            "wanted": self.wanted,
            "covered": self.covered,
            "needed": self.needed,
            "include_unverified": self.include_unverified,
            "unmatchable_factors": list(self.unmatchable_factors),
            "cells": [
                {
                    "values": cell.values,
                    "non_defaults": cell.non_defaults,
                    "wanted": cell.wanted,
                    "usable": cell.usable,
                    "unverified": cell.unverified,
                    "excluded": cell.excluded,
                    "needed": cell.needed(self.include_unverified),
                    "matched_bags": cell.matched_bags,
                    "corrected_bags": cell.corrected_bags,
                }
                for cell in self.cells
            ],
        }


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _match_keys(campaign: cs.Campaign) -> Tuple[Dict[str, str], List[str]]:
    """factor name -> metadata key, plus the factors that cannot be matched.

    Every factor of the experiment that has a metadata key takes part, not only
    the ones the campaign names: a factor the campaign leaves out is planned at
    its default, so a trial recorded at another level of it is not this
    campaign's trial (2026-10-21: ``combined_map`` names no ``world`` and claimed
    the industrial-world flights). Unmatchable factors are reported only when
    the campaign names them, since only then could they narrow the match."""
    mapping: Dict[str, str] = {}
    unmatchable: List[str] = []
    considered = set(campaign.sweep) | set(campaign.fixed)
    for name in sorted(campaign.factors):
        factor = campaign.factors[name]
        if factor.metadata_key:
            mapping[name] = factor.metadata_key
        elif name in considered:
            # Not fatal: it just cannot narrow the match, so the cell will match
            # more broadly. Reported so the number is never quietly optimistic.
            unmatchable.append(name)
    return mapping, unmatchable


def _recorded_level(factor: cs.Factor, recorded: Any, named: bool) -> str:
    """A trial's level of one factor. A key the trial does not record means it
    predates the factor: for a factor the campaign does not name that is the
    factor's default, unless an alias maps the missing key elsewhere (``None``),
    as when a factor's default changed after older trials were recorded."""
    level = factor.canonical(recorded)
    if recorded is None and not named and level == "None":
        return str(factor.default)
    return level


def build_plan(campaign: cs.Campaign, trials: List[ti.Trial],
               include_unverified: bool = False) -> Plan:
    mapping, unmatchable = _match_keys(campaign)
    named = set(campaign.sweep) | set(campaign.fixed)

    # Bucket the recorded trials by the campaign's own match key, once.
    buckets: Dict[Tuple[Tuple[str, str], ...], List[ti.Trial]] = defaultdict(list)
    for trial in trials:
        if trial.experiment != campaign.experiment:
            continue
        key = tuple(sorted(
            (name, _recorded_level(campaign.factors[name], trial.metadata.get(meta_key), name in named))
            for name, meta_key in mapping.items()))
        buckets[key].append(trial)

    cells: Dict[Tuple, PlannedCell] = {}
    for planned in campaign.combinations():
        key = tuple(sorted((name, str(planned.values[name])) for name in mapping))
        cell = cells.get(key)
        if cell is None:
            cell = PlannedCell(
                values={k: v for k, v in planned.values.items()},
                non_defaults=list(planned.non_defaults),
                wanted=0,
            )
            for trial in buckets.get(key, []):
                setattr(cell, trial.status, getattr(cell, trial.status) + 1)
                if trial.status in ("usable", "unverified"):
                    cell.matched_bags.append(trial.bag_dir.name)
                    if trial.corrections:
                        cell.corrected_bags.append(trial.bag_dir.name)
            cells[key] = cell
        cell.wanted += 1

    ordered = sorted(cells.values(),
                     key=lambda c: (-c.needed(include_unverified), c.describe()))
    return Plan(campaign=campaign, cells=ordered,
                include_unverified=include_unverified,
                unmatchable_factors=unmatchable)


# --------------------------------------------------------------------------- #
# Execution order (2026-10-01, randomized complete blocks)
# --------------------------------------------------------------------------- #
@dataclass
class ScheduledTrial:
    """One trial the runner will execute, in the order it will execute it."""
    cell: PlannedCell
    repetition: int
    #: seed/block/position, or None when the campaign declares no ``order``.
    order: Optional[Dict[str, Any]] = None


def execution_order(plan: Plan) -> List[ScheduledTrial]:
    """The order in which the runner executes what the plan still needs.

    Without a campaign ``order`` this is exactly the loop the runner always
    ran: each cell in plan order, its repetitions in sequence. With one:

    * ``blocks`` (true, or an explicit count B): randomized complete blocks.
      ``true`` means B = the smallest number still needed by any cell, so every
      block holds every cell at least once. Repetition r of a cell goes to
      block ((r - 1) mod B) + 1, so a cell that needs 10 when B = 5 appears
      twice in each block.
    * ``shuffle`` draws the order inside each block (or of the whole list when
      there are no blocks) from ``random.Random(seed)``, block by block, over
      the cells sorted by their description. The schedule therefore depends
      only on the seed and on what is still needed, never on the planner's
      display order.

    Coverage arithmetic is untouched: the same (cell, repetition) pairs are
    returned, only their order changes. A resumed campaign is re-blocked from
    what is still needed, so its order differs from the first session's. The
    attempt ledger records what actually ran.

    Repetition numbers (2026-10-13): the k-th trial a cell still needs gets
    ``repetition = cell.recorded + k``, so the number counts the cell's trials
    across invocations. The blocks and the shuffle still use k, so the order
    of any schedule is exactly what it was before this change.
    """
    order = plan.campaign.order or {}
    pending = [(cell, rep) for cell in plan.cells
               for rep in range(1, cell.needed(plan.include_unverified) + 1)]
    if not order:
        return [ScheduledTrial(cell, cell.recorded + rep) for cell, rep in pending]

    shuffle = bool(order.get("shuffle", False))
    seed = order.get("seed")
    blocks_opt = order.get("blocks", False)
    canonical = sorted(pending, key=lambda cr: (cr[0].describe(), cr[1]))
    if blocks_opt:
        counts = [c.needed(plan.include_unverified) for c in plan.cells
                  if c.needed(plan.include_unverified) > 0]
        n_blocks = (int(blocks_opt) if not isinstance(blocks_opt, bool)
                    else (min(counts) if counts else 1))
        n_blocks = max(1, n_blocks)
    else:
        n_blocks = 1
    grouped: Dict[int, List[tuple]] = {b: [] for b in range(1, n_blocks + 1)}
    for cell, rep in canonical:
        grouped[((rep - 1) % n_blocks) + 1].append((cell, rep))
    rng = random.Random(seed)
    total = len(canonical)
    out: List[ScheduledTrial] = []
    for block in range(1, n_blocks + 1):
        members = grouped[block]
        if shuffle:
            rng.shuffle(members)
        for i, (cell, rep) in enumerate(members, 1):
            out.append(ScheduledTrial(cell, cell.recorded + rep, {
                "shuffle": shuffle, "seed": seed,
                "blocks": n_blocks if blocks_opt else None,
                "block": block if blocks_opt else None,
                "position": len(out) + 1, "position_in_block": i if blocks_opt else None,
                "of": total,
            }))
    return out


def render_schedule(plan: Plan, limit: int = 200) -> str:
    """The execution order, one line per trial (``--schedule``)."""
    sched = execution_order(plan)
    order = plan.campaign.order or {}
    lines = [f"Execution order: {len(sched)} trial(s)"
             + (f", seed {order.get('seed')}, shuffle {bool(order.get('shuffle'))}, "
                f"{sched[0].order['blocks'] or 'no'} block(s)" if order and sched else
                " (the planner's order: no campaign `order` declared)")]
    last_block = None
    for item in sched[:limit]:
        block = item.order.get("block") if item.order else None
        if block is not None and block != last_block:
            lines.append(f"  -- block {block}")
            last_block = block
        pos = item.order["position"] if item.order else len(lines)
        lines.append(f"  {pos:>4}  rep{item.repetition:02d}  {item.cell.describe()}")
    if len(sched) > limit:
        lines.append(f"  … and {len(sched) - limit} more")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(plan: Plan, limit: int = 40) -> str:
    campaign = plan.campaign
    lines = [f"Campaign: {campaign.name}  ({campaign.experiment})"]
    if campaign.describe:
        lines.append(f"  {campaign.describe}")
    lines.append("")
    lines.append(f"  wanted {plan.wanted} · covered {plan.covered} · "
                 f"STILL MISSING {plan.needed}")
    if plan.include_unverified:
        lines.append("  (counting unverified trials as coverage — metadata cannot prove them)")
    else:
        unverified = sum(c.unverified for c in plan.cells)
        if unverified:
            lines.append(f"  ({unverified} matching trial(s) are unverified and NOT counted; "
                         f"pass --include-unverified to count them)")
    if campaign.order:
        lines.append(f"  order: shuffle={bool(campaign.order.get('shuffle'))} "
                     f"seed={campaign.order.get('seed')} blocks={campaign.order.get('blocks', False)} "
                     f"(--schedule prints the execution order)")
    corrected = [b for c in plan.cells for b in c.corrected_bags]
    if corrected:
        lines.append(f"  ({len(corrected)} matching trial(s) are matched through a person's "
                     f"correction, corrections.json: {', '.join(corrected[:5])}"
                     f"{' …' if len(corrected) > 5 else ''})")
    if plan.unmatchable_factors:
        lines.append(f"  ⚠ not matchable against recorded data: "
                     f"{', '.join(plan.unmatchable_factors)} — counts may be optimistic")
    lines.append("")
    lines.append("  (* marks a non-default value)")

    todo = [c for c in plan.cells if c.needed(plan.include_unverified) > 0]
    done = [c for c in plan.cells if c.needed(plan.include_unverified) == 0]

    if todo:
        lines.append("")
        lines.append(f"  STILL MISSING ({len(todo)} cell(s)):")
        for cell in todo[:limit]:
            n = cell.needed(plan.include_unverified)
            have = f"have {cell.usable}u/{cell.unverified}un" if (cell.usable or cell.unverified) else "none yet"
            lines.append(f"    x{n}  {cell.describe()}   [{have}, want {cell.wanted}]")
        if len(todo) > limit:
            lines.append(f"    … and {len(todo) - limit} more")
    if done:
        lines.append("")
        lines.append(f"  COVERED ({len(done)} cell(s)):")
        for cell in done[:limit]:
            fixed = f", {len(cell.corrected_bags)} corrected" if cell.corrected_bags else ""
            lines.append(f"    ok   {cell.describe()}   [have {cell.usable} usable{fixed}]")
        if len(done) > limit:
            lines.append(f"    … and {len(done) - limit} more")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("campaign", type=Path, help="campaign YAML")
    roots.add_root_arguments(ap)
    ap.add_argument("--set", action="append", default=[], metavar="name=a[,b]",
                    help="override a factor (Hydra-shaped); repeatable")
    ap.add_argument("--include-unverified", action="store_true",
                    help="count trials whose metadata cannot prove them as coverage")
    ap.add_argument("--json", action="store_true", help="emit the machine-readable plan")
    ap.add_argument("--schedule", action="store_true",
                    help="also print the execution order the runner will use "
                         "(the campaign's `order`: seed, blocks, shuffle)")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args(argv)
    roots.resolve_root_arguments(args)

    try:
        campaign = cs.load_campaign(args.campaign, args.experiments_root, args.set)
    except cs.SpecError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    refusal = cs.contract_refusal(campaign, args.experiments_root)
    if refusal:
        print(f"ERROR: {refusal}", file=sys.stderr)
        return 2
    warnings: List[str] = []
    trials = ti.load_trials(args.experiments_root, campaign.experiment)
    plan = build_plan(campaign, trials, args.include_unverified)

    if args.json:
        print(json.dumps(plan.to_dict(), indent=2))
        return 0

    if warnings:
        print("⚠ coverage warnings:")
        for warning in warnings:
            print(f"    {warning}")
        print("")
    print(render(plan, args.limit))
    if args.schedule:
        print("")
        print(render_schedule(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
