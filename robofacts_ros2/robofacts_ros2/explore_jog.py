#!/usr/bin/env python3
"""One jog step of robofacts Explore: publish a velocity for a moment, then a zero twist.

The Explore run page's jog pad (2026-10-17) starts this for each press, in the
same one-at-a-time slot as the ``explore.actions``, so the red stop ends it too. The
runner, not the browser, publishes: the Foxglove bridge of an exploration never
publishes on a topic. The topic and the limits come from runner.yaml's
``explore.jog``, and explore.py clips every value to them before this starts.

It publishes ``geometry_msgs/Twist`` at ``--rate`` Hz for ``--seconds``, then a zero
twist a few times, and exits 0. A SIGTERM or SIGINT ends the step early, also with
the zero twist, so a stop never leaves a velocity behind. A node that keeps the last
command it received (a simulated drone controller, for example) then holds its position.

Self-contained on purpose (no robofacts import), like explore_params.py: run by file
path locally, as ``python3 -m robofacts_ros2.explore_jog`` on a remote host.

    python3 explore_jog.py --topic /cmd_vel --vx 0.2 --seconds 1.0
"""

import argparse
import signal
import sys
import time

ZERO_REPEATS = 5


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--topic", required=True)
    ap.add_argument("--vx", type=float, default=0.0)
    ap.add_argument("--vy", type=float, default=0.0)
    ap.add_argument("--vz", type=float, default=0.0)
    ap.add_argument("--yaw", type=float, default=0.0, help="angular.z [rad/s]")
    ap.add_argument("--seconds", type=float, default=1.0)
    ap.add_argument("--rate", type=float, default=10.0)
    args = ap.parse_args(argv)

    import rclpy
    from geometry_msgs.msg import Twist

    stop = {"now": False}

    def _stop(*_):
        stop["now"] = True

    try:
        from rclpy.signals import SignalHandlerOptions
        rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    except (ImportError, TypeError):
        rclpy.init()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    node = rclpy.create_node("robofacts_explore_jog")
    pub = node.create_publisher(Twist, args.topic, 10)
    move = Twist()
    move.linear.x, move.linear.y, move.linear.z = args.vx, args.vy, args.vz
    move.angular.z = args.yaw
    # Give discovery a moment, or the first messages go to nobody.
    t0 = time.monotonic()
    while pub.get_subscription_count() == 0 and time.monotonic() - t0 < 2.0 and not stop["now"]:
        rclpy.spin_once(node, timeout_sec=0.05)
    print(f"jog: {args.topic} vx={args.vx} vy={args.vy} vz={args.vz} yaw={args.yaw} "
          f"for {args.seconds} s ({pub.get_subscription_count()} subscriber(s))", flush=True)
    period = 1.0 / max(args.rate, 1.0)
    t0 = time.monotonic()
    while not stop["now"] and time.monotonic() - t0 < args.seconds:
        pub.publish(move)
        time.sleep(period)
    for _ in range(ZERO_REPEATS):
        pub.publish(Twist())
        time.sleep(0.02)
    print("jog: " + ("stopped" if stop["now"] else "done") + ", zero twist sent", flush=True)
    node.destroy_node()
    rclpy.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
