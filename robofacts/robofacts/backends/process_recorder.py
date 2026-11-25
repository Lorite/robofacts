#!/usr/bin/env python3
"""The recorder of the process backend: follow files until SIGINT, keep what was written.

    python3 -m robofacts.backends.process_recorder --out <directory> <file> [<file> ...]

For every source file it stores, in ``<directory>/data/<file name>``, the bytes that were
appended while it ran. A file that exists at the start is followed from its end, so the
recording holds this trial's part of a log and not the history before it. A file that
appears later is taken from its first byte. On SIGINT or SIGTERM it copies what is left
and writes ``<directory>/recording.json`` last, which marks the recording finalized. The
manifest is written to a temporary file and renamed, so it is complete or absent.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

MANIFEST = "recording.json"
DATA_DIR = "data"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def stored_names(sources: List[str]) -> Dict[str, str]:
    """The file name each source gets in data/, made unique when two sources share one."""
    names: Dict[str, str] = {}
    used: set = set()
    for source in sources:
        name = Path(source).name or "source"
        candidate, n = name, 2
        while candidate in used:
            candidate, n = f"{n}_{name}", n + 1
        used.add(candidate)
        names[source] = candidate
    return names


def record(out: Path, sources: List[str], poll_s: float = 0.2) -> int:
    data = out / DATA_DIR
    data.mkdir(parents=True, exist_ok=True)
    names = stored_names(sources)
    # A file that exists now is followed from its end, a later one from its start.
    offsets = {s: (Path(s).stat().st_size if Path(s).is_file() else 0) for s in sources}
    counts = {s: 0 for s in sources}
    stopping = {"yes": False}

    def _stop(signum, frame):  # noqa: ARG001
        stopping["yes"] = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    def copy_new() -> None:
        for source in sources:
            path = Path(source)
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size < offsets[source]:      # the file was replaced by a shorter one
                offsets[source] = 0
            if size == offsets[source]:
                continue
            with path.open("rb") as src, (data / names[source]).open("ab") as dst:
                src.seek(offsets[source])
                chunk = src.read()
                dst.write(chunk)
            offsets[source] += len(chunk)
            counts[source] += len(chunk)

    started = _now()
    print(f"[recorder] recording {len(sources)} source(s) into {out}", flush=True)
    while not stopping["yes"]:
        copy_new()
        time.sleep(poll_s)
    copy_new()
    manifest = {"started": started, "stopped": _now(),
                "sources": [{"source": s, "file": f"{DATA_DIR}/{names[s]}", "bytes": counts[s]}
                            for s in sources]}
    # Whole or not at all (2026-11-25): a manifest cut by a full disk would mark a recording
    # finalized that is not.
    tmp = out / f"{MANIFEST}.tmp"
    try:
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(tmp, out / MANIFEST)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    print(f"[recorder] finalized: {sum(counts.values())} bytes", flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True, help="the recording directory")
    ap.add_argument("sources", nargs="+", help="the files to follow")
    args = ap.parse_args(argv)
    return record(args.out, args.sources)


if __name__ == "__main__":
    sys.exit(main())
