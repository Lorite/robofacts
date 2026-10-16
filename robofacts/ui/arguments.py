"""The web interface's command-line arguments, kept apart from the app so that
``trials ui --help`` works on a machine without Flask."""
from __future__ import annotations

import argparse

from .. import roots

DESCRIPTION = "Experiments control surface: the coverage grid, runs with a confirm step, the analyses and the stop bar."


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="trials ui", description=DESCRIPTION)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address. Localhost by default ON PURPOSE: reach it "
                         "with `ssh -N -L 8080:localhost:8080 <host>`.")
    ap.add_argument("--port", type=int, default=8080)
    roots.add_root_arguments(ap)
    ap.add_argument("--no-auth", action="store_true",
                    help="run without a password (only sensible on 127.0.0.1)")
    return ap
