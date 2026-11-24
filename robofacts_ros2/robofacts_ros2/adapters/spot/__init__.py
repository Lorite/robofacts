"""The Spot adapter: Boston Dynamics Spot through spot_ros2.

Checks: ``graph_nav_localized`` (Spot is localized to its GraphNav map, read from the status
of a node that publishes the map alignment). UI: ``ui.lease_owners`` turns an action's echo
of ``<robot>/status/leases`` into a table of who holds each lease resource.
"""

from robofacts_ros2.adapters.spot.ui import lease_owners as LEASE_TABLE  # noqa: E402,F401 (read by the web interface)

#: The default namespace of the robot the record profiles build topics from.
NAMES = {"spot_name": "spot"}

PARALLEL_SAFE_CHECKS = frozenset({"graph_nav_localized"})
SEQUENTIAL_CHECKS: dict = {}
GATE_SENTENCES: dict = {}
