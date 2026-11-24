#!/usr/bin/env python3
"""Slim an already-recorded heavy bag: drop the image/depth topics + zstd.

A 6-10 GB flight bag with a depth camera is ~99 % raw colour + aligned depth. Every
other question ("where did the estimator think the drone was", "which gate
fired", "what did the safety latch do") is answerable from the remaining few
tens of MB — which fit in a scp/rsync of a few seconds instead of a
half-hour copy off the rig.

This wraps ``ros2 bag convert``: it reads the input bag's topic list, drops the
heavy ones (``record_profiles.heavy_topic_patterns()`` by default), and writes a
compressed copy. An explicit INCLUDE list is generated rather than an exclude
regex, so the result is deterministic and the script prints exactly what it kept.

    # on the rig, next to the bag:
    python3 experiments/common/scripts/slim_bag.py bags/2026-07-23T18-12-03_hover06
    #  -> bags/2026-07-23T18-12-03_hover06_slim   (plain .mcap, zstd-compressed
    #     chunks — opens directly in Foxglove Studio, no decompression step)

    # keep the overlay but also drop /rosout, and preview first:
    python3 .../slim_bag.py <bag> --exclude rosout --dry-run

Then either copy the slim bag to the laptop, or leave it on the rig and play it
there while RViz runs on the laptop (see
``docs/development/cross_host_ros2_dds_discovery.md`` -> "Remote bag inspection").
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional


from robofacts_ros2.record_profiles import is_heavy_topic  # noqa: E402


def bag_topics(bag_dir: Path) -> List[str]:
    """Topic names in a bag.

    Prefers ``rosbag2_py.Info`` (authoritative, storage-agnostic); falls back to
    scraping ``metadata.yaml`` so the script still works in a plain Python env
    (e.g. reading a bag copied to a machine without the ROS overlay sourced).
    """
    try:
        import rosbag2_py
        meta = rosbag2_py.Info().read_metadata(str(bag_dir), "")
        names = [t.topic_metadata.name for t in meta.topics_with_message_count]
        if names:
            return names
    except Exception:
        pass
    return _bag_topics_from_yaml(bag_dir)


def _bag_topics_from_yaml(bag_dir: Path) -> List[str]:
    meta = bag_dir / "metadata.yaml"
    if not meta.exists():
        raise FileNotFoundError(f"{bag_dir} has no metadata.yaml (not a bag directory?)")
    topics: List[str] = []
    for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("name:") and "topic_metadata" not in stripped:
            name = stripped.split("name:", 1)[1].strip().strip("'\"")
            if name.startswith("/"):
                topics.append(name)
    # dedupe, keep order
    seen = set()
    return [t for t in topics if not (t in seen or seen.add(t))]


def dir_size_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def select_topics(all_topics: List[str], extra_excludes: List[str],
                  keep: List[str]) -> List[str]:
    """Keep everything that is neither heavy nor explicitly excluded."""
    kept = []
    for topic in all_topics:
        if topic in keep:
            kept.append(topic)
            continue
        if is_heavy_topic(topic):
            continue
        if any(pattern in topic for pattern in extra_excludes):
            continue
        kept.append(topic)
    return kept


# MCAP compresses its own chunks. Asking the *storage plugin* to do it keeps the
# output a real .mcap container; asking rosbag2 to do it (compression_mode=file)
# wraps the finished file in a zstd blob named .mcap.zstd, which is NOT an MCAP
# file — Foxglove Studio, the `mcap` CLI and every MCAP-native tool refuse to
# open it until you manually `zstd -d` it. Measured on a real 16.1 GB flight bag:
#   internal zstd  ->  5.1 MB, opens everywhere        <- what we do
#   file-level     ->  3.6 MB, opens nowhere natively
#   uncompressed   -> 30.7 MB
# 1.4 MB is a cheap price for a file that just opens.
MCAP_STORAGE_CONFIG = {"compression": "Zstd", "compressionLevel": "Default"}


def write_storage_config(path: Path) -> Path:
    """Write the MCAP storage-plugin options (JSON is valid YAML)."""
    path.write_text(json.dumps(MCAP_STORAGE_CONFIG, indent=2), encoding="utf-8")
    return path


def write_convert_yaml(out_uri: Path, topics: List[str], storage: str,
                       compress: bool, path: Path,
                       storage_config_uri: Optional[Path] = None) -> Path:
    """Write the ``ros2 bag convert`` output-options YAML.

    rosbag2 (Humble, 0.15.x) parses this into StorageOptions/RecordOptions, so
    the keys are their snake_case field names. JSON is valid YAML, which keeps
    topic names with slashes quoted correctly without a yaml dependency.

    Compression is requested from the MCAP storage plugin when possible (see
    MCAP_STORAGE_CONFIG). sqlite3 has no equivalent, so it falls back to
    rosbag2's file-level compression there.
    """
    entry = {
        "uri": str(out_uri),
        "storage_id": storage,
        "all": False,
        "topics": topics,
    }
    if compress:
        if storage == "mcap" and storage_config_uri is not None:
            entry["storage_config_uri"] = str(storage_config_uri)
        else:
            entry["compression_mode"] = "file"
            entry["compression_format"] = "zstd"
    path.write_text(json.dumps({"output_bags": [entry]}, indent=2), encoding="utf-8")
    return path


def slim(bag_dir: Path, out_dir: Optional[Path] = None, storage: str = "mcap",
         extra_excludes: Optional[List[str]] = None,
         keep: Optional[List[str]] = None, compress: bool = True,
         dry_run: bool = False) -> Optional[Path]:
    bag_dir = bag_dir.resolve()
    out_dir = (out_dir or bag_dir.parent / f"{bag_dir.name}_slim").resolve()
    topics = bag_topics(bag_dir)
    kept = select_topics(topics, extra_excludes or [], keep or [])
    dropped = [t for t in topics if t not in kept]

    print(f"input : {bag_dir}  ({human(dir_size_bytes(bag_dir))}, {len(topics)} topics)")
    print(f"output: {out_dir}  (storage={storage}, zstd={compress})")
    print(f"keep  ({len(kept)}):")
    for t in kept:
        print(f"    + {t}")
    print(f"drop  ({len(dropped)}):")
    for t in dropped:
        print(f"    - {t}")
    if not kept:
        print("nothing left to keep — refusing to write an empty bag", file=sys.stderr)
        return None
    if dry_run:
        return None
    if out_dir.exists():
        print(f"refusing to overwrite existing {out_dir}", file=sys.stderr)
        return None

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
        yaml_path = Path(tmp.name)
    storage_cfg_path = None
    if compress and storage == "mcap":
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
            storage_cfg_path = write_storage_config(Path(tmp.name))
    write_convert_yaml(out_dir, kept, storage, compress, yaml_path, storage_cfg_path)
    cmd = ["ros2", "bag", "convert", "-i", str(bag_dir), "-o", str(yaml_path)]
    print("$ " + " ".join(cmd))
    rc = subprocess.call(cmd)
    yaml_path.unlink(missing_ok=True)
    if storage_cfg_path is not None:
        storage_cfg_path.unlink(missing_ok=True)
    if rc != 0:
        print(f"ros2 bag convert failed (rc={rc})", file=sys.stderr)
        return None
    before, after = dir_size_bytes(bag_dir), dir_size_bytes(out_dir)
    ratio = (before / after) if after else float("inf")
    print(f"done: {human(before)} -> {human(after)}  ({ratio:.0f}x smaller)")
    return out_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bag", type=Path, help="input bag directory")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="output bag directory (default: <bag>_slim)")
    ap.add_argument("--storage", default="mcap", choices=["mcap", "sqlite3"])
    ap.add_argument("--exclude", action="append", default=[],
                    help="extra substring to drop (repeatable)")
    ap.add_argument("--keep", action="append", default=[],
                    help="topic to keep even if it matches a heavy pattern (repeatable)")
    ap.add_argument("--no-compress", action="store_true", help="skip zstd compression")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    args = ap.parse_args(argv)
    out = slim(args.bag, args.output, args.storage, args.exclude, args.keep,
               compress=not args.no_compress, dry_run=args.dry_run)
    return 0 if (out is not None or args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())
