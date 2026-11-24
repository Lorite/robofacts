"""The ``trials ui`` command of the ROS 2 backend."""
from __future__ import annotations

import sys


def main(argv):
    # The parser lives apart from the app, so the help needs no Flask.
    from robofacts_ros2.ui.arguments import build_parser
    if any(a in ("-h", "--help") for a in argv):
        build_parser().print_help()
        return 0
    try:
        from robofacts_ros2.ui import app
    except ImportError as exc:
        if "flask" in str(exc).lower():
            print("trials ui: the web interface needs Flask (`pip install flask`), which the dev "
                  "container has. The other commands do not need it.", file=sys.stderr)
            return 2
        raise
    return app.main(argv)
