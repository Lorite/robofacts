#!/usr/bin/env python3
"""Export /rosout entries from a ROS 2 bag into a readable text file.

The runner writes ``rosout.txt`` beside every recorded bag after the metadata, so the
slim copy carries it too (2026-10-14). Before that, only the run notebooks exported it, and
no runner-written bag had one, although the trial rules of the project that grew this framework ask for it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterator

import rosbag2_py
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message


def stamp_to_float(stamp) -> float:
    return float(stamp.sec) + float(stamp.nanosec) * 1e-9


def detect_storage_id(bag_dir: Path) -> str:
    metadata_file = bag_dir / "metadata.yaml"
    if metadata_file.exists():
        for line in metadata_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("storage_identifier:"):
                _, value = stripped.split(":", 1)
                storage_id = value.strip().strip("\"'")
                if storage_id:
                    return storage_id
    if any(bag_dir.glob("*.db3")):
        return "sqlite3"
    return "mcap"


def validate_bag_dir(bag_dir: Path) -> None:
    if not bag_dir.exists() or not bag_dir.is_dir():
        raise FileNotFoundError(f"Invalid bag directory: {bag_dir}")

    has_metadata = (bag_dir / "metadata.yaml").exists()
    has_mcap = any(bag_dir.glob("*.mcap"))
    has_db3 = any(bag_dir.glob("*.db3"))

    if not (has_metadata or has_mcap or has_db3):
        raise FileNotFoundError(
            f"No ROS 2 bag files found in {bag_dir}. Expected metadata.yaml, *.mcap, or *.db3"
        )


def read_rosout_messages(bag_dir: Path) -> Iterator[object]:
    reader = rosbag2_py.SequentialReader()
    storage_id = detect_storage_id(bag_dir)
    reader.open(
        rosbag2_py.StorageOptions(uri=str(bag_dir), storage_id=storage_id),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr",
            output_serialization_format="cdr",
        ),
    )

    topic_types = {topic.name: topic.type for topic in reader.get_all_topics_and_types()}
    rosout_type = topic_types.get("/rosout")
    if rosout_type is None:
        raise RuntimeError(f"Topic /rosout not found in bag: {bag_dir}")

    msg_type = get_message(rosout_type)

    # Read only /rosout: a flight bag holds gigabytes of images the export never needs.
    reader.set_filter(rosbag2_py.StorageFilter(topics=["/rosout"]))
    while reader.has_next():
        topic, data, _ = reader.read_next()
        if topic == "/rosout":
            yield deserialize_message(data, msg_type)


def export_rosout_text(bag_dir: Path, output_path: Path) -> int:
    import itertools

    # Open the bag before the output file: a bag without /rosout raises here, and must not
    # leave an empty rosout.txt behind that claims the node logged nothing.
    messages = read_rosout_messages(bag_dir)
    first = next(messages, None)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0

    with output_path.open("w", encoding="utf-8") as out_file:
        for msg in itertools.chain([first] if first is not None else [], messages):
            timestamp = stamp_to_float(msg.stamp)
            line = (
                f"{timestamp:.9f} | level={msg.level} | name={msg.name} | "
                f"function={msg.function} | file={msg.file}:{msg.line} | {msg.msg}"
            )
            out_file.write(line + "\n")
            count += 1

    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Export /rosout messages from a ROS 2 bag")
    parser.add_argument("bag_dir", help="Path to bag directory")
    parser.add_argument(
        "--output",
        default=None,
        help="Output text file path (default: <bag_dir>/rosout.txt)",
    )
    args = parser.parse_args()

    bag_dir = Path(args.bag_dir).expanduser().resolve()
    validate_bag_dir(bag_dir)

    output_path = Path(args.output).expanduser().resolve() if args.output else bag_dir / "rosout.txt"

    count = export_rosout_text(bag_dir, output_path)
    print(f"Exported {count} /rosout messages to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
