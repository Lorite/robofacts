#!/usr/bin/env python3
"""Print every parameter event of the ROS graph as one JSON line (robofacts Explore).

Explore mode (explore.py) starts this BEFORE the stack. A change made from
Foxglove arrives on ``/parameter_events`` as ``changed_parameters``. The
starting value comes from two sources, because neither one is enough alone:

* the ``new_parameters`` events a node publishes as it declares parameters.
  Most of them are LOST: a node declares at construction, before DDS discovery
  has matched its publisher to this subscriber, and the topic is volatile
  (measured 2026-10-16, demo talker: ``use_sim_time`` never arrived);
* so each node is also READ once, through its own ``list_parameters`` and
  ``get_parameters`` services, the first time it appears in the graph. These
  lines have the kind ``initial``.

The pair of the starting value and the last change is what explore.py writes
to ``parameter_changes.yaml`` when the exploration stops.

Self-contained on purpose (no robofacts import): for a local exploration the
runner starts it by FILE path, so it works before the package is rebuilt, and
for a remote host it runs as ``python3 -m robofacts.explore_params`` from the
installed package there.

Output, one object per line, flushed:

    {"t": 1760612345.12, "node": "/talker", "name": "rate", "kind": "changed", "value": 2.0}

``kind`` is ``initial``, ``new``, ``changed`` or ``deleted``. Anything else this process
prints (rclpy warnings) is not JSON, and the reader skips it.
"""

import json
import signal
import sys
import time

NODE_NAME = "robofacts_explore_params"


def parameter_value(pv):
    """A rcl_interfaces/ParameterValue as a plain, YAML-friendly Python value."""
    kind = pv.type
    if kind == 1:
        return bool(pv.bool_value)
    if kind == 2:
        return int(pv.integer_value)
    if kind == 3:
        return float(pv.double_value)
    if kind == 4:
        return str(pv.string_value)
    if kind == 5:
        return [int(b) for b in pv.byte_array_value]
    if kind == 6:
        return [bool(v) for v in pv.bool_array_value]
    if kind == 7:
        return [int(v) for v in pv.integer_array_value]
    if kind == 8:
        return [float(v) for v in pv.double_array_value]
    if kind == 9:
        return [str(v) for v in pv.string_array_value]
    return None   # PARAMETER_NOT_SET


def event_lines(event, own_name: str = "/" + NODE_NAME):
    """The JSON lines for one rcl_interfaces/ParameterEvent (pure, for tests)."""
    if event.node == own_name:
        return []
    stamp = event.stamp.sec + event.stamp.nanosec * 1e-9 if event.stamp.sec else time.time()
    out = []
    for kind, params in (("new", event.new_parameters), ("changed", event.changed_parameters),
                         ("deleted", event.deleted_parameters)):
        for p in params:
            out.append(json.dumps({"t": round(stamp, 3), "node": event.node, "name": p.name,
                                   "kind": kind, "value": parameter_value(p.value)}))
    return out


def initial_line(node_name: str, name: str, value) -> str:
    """One ``initial`` line: a value read from the node's parameter services."""
    return json.dumps({"t": round(time.time(), 3), "node": node_name, "name": name,
                       "kind": "initial", "value": value})


def main() -> int:
    import rclpy
    from rcl_interfaces.msg import ParameterEvent
    from rcl_interfaces.srv import GetParameters, ListParameters
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

    rclpy.init()
    node = rclpy.create_node(NODE_NAME)
    own = node.get_fully_qualified_name()
    # The QoS every node publishes /parameter_events with (rmw_qos_profile_parameter_events).
    qos = QoSProfile(depth=1000, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.VOLATILE)

    def on_event(event):
        for line in event_lines(event, own):
            print(line, flush=True)

    node.create_subscription(ParameterEvent, "/parameter_events", on_event, qos)

    # Read each node once, the first time it appears. Asynchronous, so a node
    # without parameter services (or a slow one) never blocks the event log.
    # One client pair per node, kept: a new client is never ready at once
    # (service discovery takes a moment), so the call waits for a later scan.
    pending = {}   # fqn -> [lister, getter, scans left]
    done = set()

    def read(fqn: str, lister, getter) -> None:
        def got_names(future):
            result = future.result()
            names = list(result.result.names) if result else []
            if not names:
                return

            def got_values(f):
                values = f.result().values if f.result() else []
                for n, v in zip(names, values):
                    print(initial_line(fqn, n, parameter_value(v)), flush=True)

            getter.call_async(GetParameters.Request(names=names)).add_done_callback(got_values)

        lister.call_async(ListParameters.Request(depth=0)).add_done_callback(got_names)

    def scan() -> None:
        for name, namespace in node.get_node_names_and_namespaces():
            fqn = namespace.rstrip("/") + "/" + name
            if fqn in done or fqn in pending or fqn == own or name.startswith("robofacts_explore"):
                continue
            pending[fqn] = [node.create_client(ListParameters, f"{fqn}/list_parameters"),
                            node.create_client(GetParameters, f"{fqn}/get_parameters"), 10]
        for fqn, entry in list(pending.items()):
            lister, getter, left = entry
            if lister.service_is_ready() and getter.service_is_ready():
                read(fqn, lister, getter)
            elif left > 1:
                entry[2] = left - 1
                continue
            # Read, or 10 scans without parameter services: either way this node is done.
            # The clients stay alive (in `kept`) until their calls have answered.
            done.add(fqn)
            kept.extend([lister, getter])
            del pending[fqn]

    kept = []
    node.create_timer(1.0, scan)
    print(f"[explore_params] listening on /parameter_events as {own}", file=sys.stderr, flush=True)

    stop = {"now": False}

    def _stop(*_):
        stop["now"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while not stop["now"] and rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.2)
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
