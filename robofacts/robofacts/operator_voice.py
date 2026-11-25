#!/usr/bin/env python3
"""Spoken operator cues: the client side (2026-10-13).

The laptop speaks, the trial runner does not: the runner lives in a dev
container without audio (on the laptop or on the robot computer), so a cue travels as one
UDP datagram to ``scripts/operator_voice_daemon.py`` on the laptop host, which
speaks it with ``spd-say``. This module is the sending half, stdlib only and
Free of middleware, importable by every framework script.

Two promises the trial depends on:

* **A cue never blocks or breaks a trial.** Sending is one non-blocking UDP
  datagram. Nobody listening, a wrong host, no route, a bad port: the call
  returns False and the trial continues. A host given by NAME is resolved in a
  helper thread with a short deadline, because the rig is air-gapped and a DNS
  lookup there waits for a timeout instead of failing.
* **A cue is always printed too.** Speech is an addition to the terminal line,
  never a replacement, so a run log still shows every cue.

Environment:

* ``TRIALS_VOICE_HOST``: where the daemon listens (default ``127.0.0.1``, the
  laptop's own container, which shares the host network). On the robot computer, the
  laptop's address on the robot network, for example ``192.0.2.30``.
* ``TRIALS_VOICE_PORT``: default 47900.
* ``TRIALS_VOICE=off`` (or 0, false, no): print only, send nothing.
  ``TRIALS_VOICE=on`` forces sending. Unset means on, EXCEPT inside a pytest
  run: the framework's own tests drive the runner with hardware-like configs,
  and a test suite must never talk through the laptop speaker.

CLI::

    python3 experiments/common/scripts/operator_voice.py "Recording. Keep still."
    python3 experiments/common/scripts/operator_voice.py --check    # silent ping, exit 0 if the daemon answers
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

#: Same port as scripts/operator_voice_daemon.py DEFAULT_PORT (a test pins it).
DEFAULT_PORT = 47900
DEFAULT_HOST = "127.0.0.1"
#: Longest text one cue may carry. The daemon applies the same cap.
MAX_CHARS = 200
ENV_HOST = "TRIALS_VOICE_HOST"
ENV_PORT = "TRIALS_VOICE_PORT"
ENV_SWITCH = "TRIALS_VOICE"
OFF_VALUES = {"off", "0", "false", "no", "disabled"}
ON_VALUES = {"on", "1", "true", "yes", "force"}
PING = b"\x00ping"
PONG = b"\x00pong"
#: Deadline for resolving a host NAME. An IP literal needs no lookup at all.
RESOLVE_TIMEOUT_S = 0.3


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """False when ``TRIALS_VOICE`` says off. Unset means on, except under pytest."""
    env = os.environ if env is None else env
    value = str(env.get(ENV_SWITCH, "")).strip().lower()
    if value in OFF_VALUES:
        return False
    if value in ON_VALUES:
        return True
    return "PYTEST_CURRENT_TEST" not in env


def target(env: Optional[Mapping[str, str]] = None) -> Tuple[str, int]:
    """(host, port) from the environment, with the defaults. Never raises."""
    env = os.environ if env is None else env
    host = str(env.get(ENV_HOST) or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = int(env.get(ENV_PORT) or DEFAULT_PORT)
        if not 0 < port < 65536:
            raise ValueError
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return host, port


def clip(text: Any, max_chars: int = MAX_CHARS) -> str:
    """Speakable text: control characters (the bell too) out, whitespace
    collapsed, at most ``max_chars`` characters, cut at a word boundary."""
    chars = [" " if unicodedata.category(c) == "Cc" else c for c in str(text)]
    out = " ".join("".join(chars).split())
    if len(out) > max_chars:
        cut = out[:max_chars]
        space = cut.rfind(" ")
        out = (cut[:space] if space > max_chars // 2 else cut).rstrip(" ,;:")
    return out


def _resolve(host: str, port: int, timeout_s: float) -> Optional[Tuple[str, int]]:
    """An IPv4 address for ``host``, without ever waiting longer than ``timeout_s``."""
    try:
        ipaddress.IPv4Address(host)
        return host, port
    except ValueError:
        pass
    found: Dict[str, Any] = {}

    def lookup() -> None:
        try:
            info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
            found["addr"] = info[0][4]
        except OSError:
            pass

    thread = threading.Thread(target=lookup, daemon=True)
    thread.start()
    thread.join(timeout_s)
    return found.get("addr")


def send(text: Any, *, host: Optional[str] = None, port: Optional[int] = None,
         env: Optional[Mapping[str, str]] = None) -> bool:
    """Send one cue to the voice daemon. True when a datagram left this host.

    Prints nothing and never raises. True does not mean it was spoken: UDP has
    no delivery receipt (use ``check`` for that).
    """
    try:
        if not enabled(env):
            return False
        body = clip(text)
        if not body:
            return False
        default_host, default_port = target(env)
        addr = _resolve(host or default_host, int(port or default_port), RESOLVE_TIMEOUT_S)
        if addr is None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(body.encode("utf-8"), addr)
        return True
    except Exception:  # noqa: BLE001 - a cue never outranks a trial
        return False


def speak(text: Any, *, bell: bool = False, prefix: str = "[voice] ",
          out: Optional[Callable[[str], None]] = None, **kwargs: Any) -> bool:
    """Print the cue (with the terminal bell when asked), then send it."""
    line = f"{prefix}{text}"
    if bell:
        line = "\a" + line
    try:
        if out is not None:
            out(line)
        else:
            print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass
    return send(text, **kwargs)


def check(*, host: Optional[str] = None, port: Optional[int] = None,
          timeout_s: float = 1.0, env: Optional[Mapping[str, str]] = None) -> bool:
    """Ping the daemon (nothing is spoken). True when it answered in time."""
    default_host, default_port = target(env)
    addr = _resolve(host or default_host, int(port or default_port), RESOLVE_TIMEOUT_S)
    if addr is None:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_s)
            sock.sendto(PING, addr)
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                data, _ = sock.recvfrom(64)
                if data == PONG:
                    return True
    except OSError:
        return False
    return False


# --------------------------------------------------------------------------- #
# A timeline of cues, relative to one moment (the runner's recording start)
# --------------------------------------------------------------------------- #
@dataclass
class Cue:
    at_s: float
    text: str


def as_cues(entries: Optional[Sequence[Any]]) -> List[Cue]:
    """Validated campaign entries ({at_s, text}) as Cues, sorted by time.

    The sort is stable, so two cues at the same time are spoken in file order.
    """
    cues = [e if isinstance(e, Cue) else Cue(float(e["at_s"]), str(e["text"]))
            for e in (entries or [])]
    return sorted(cues, key=lambda c: c.at_s)


class CueTimeline:
    """Speak each cue at its time after ``t0``, in a background thread.

    ``stop()`` ends the timeline at once: the runner calls it when the action
    ends, so a cue planned after the trial is reported as not spoken rather
    than spoken over the teardown. Every effect is injectable (``now``,
    ``wait``, ``send``, ``out``), so tests run on a fake clock without sound.
    ``wait(timeout)`` must return True when the timeline was stopped.
    """

    def __init__(self, cues: Sequence[Any], *, send: Optional[Callable[[str], bool]] = None,
                 out: Optional[Callable[[str], None]] = None,
                 now: Callable[[], float] = time.monotonic,
                 wait: Optional[Callable[[float], bool]] = None):
        self.cues = as_cues(cues)
        # The module's send(), looked up at call time (so a test can replace it).
        self._send = send if send is not None else (lambda text: globals()["send"](text))
        self._out = out
        self._now = now
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._thread: Optional[threading.Thread] = None
        self.results: List[Dict[str, Any]] = []

    def _print(self, line: str) -> None:
        try:
            if self._out is not None:
                self._out(line)
            else:
                print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass

    def run(self, t0: Optional[float] = None) -> List[Dict[str, Any]]:
        """Speak the cues in order, blocking, until done or stopped."""
        t0 = self._now() if t0 is None else t0
        for i, cue in enumerate(self.cues):
            remaining = t0 + cue.at_s - self._now()
            if (remaining > 0 and self._wait(remaining)) or self._stop.is_set():
                for left in self.cues[i:]:
                    self.results.append({"at_s": left.at_s, "text": left.text,
                                         "spoken": False, "why": "stopped before its time"})
                break
            self._print(f"[voice] +{cue.at_s:.1f} s  {cue.text}")
            sent = bool(self._send(cue.text))
            self.results.append({"at_s": cue.at_s, "text": cue.text, "spoken": sent,
                                 "t_s": round(self._now() - t0, 2)})
        return self.results

    def start(self) -> "CueTimeline":
        t0 = self._now()
        self._thread = threading.Thread(target=self.run, args=(t0,), name="voice-cues",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, join_s: float = 0.5) -> List[Dict[str, Any]]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(join_s)
        return self.results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Speak one operator cue on the laptop "
                                             "(through scripts/operator_voice_daemon.py).")
    ap.add_argument("text", nargs="*", help="the cue to speak")
    ap.add_argument("--host", help=f"daemon host (default ${ENV_HOST} or {DEFAULT_HOST})")
    ap.add_argument("--port", type=int, help=f"daemon port (default ${ENV_PORT} or {DEFAULT_PORT})")
    ap.add_argument("--check", action="store_true",
                    help="ping the daemon without speaking; exit 0 when it answers")
    args = ap.parse_args(argv)
    host, port = target()
    host, port = args.host or host, args.port or port
    if args.check:
        ok = check(host=args.host, port=args.port)
        print(f"[voice] daemon at {host}:{port}: " + ("answered" if ok else "NO ANSWER (is "
              "the operator-voice daemon (scripts/operator_voice_daemon.py) running on the laptop, and the firewall open for "
              "this source?)"))
        return 0 if ok else 1
    if not args.text:
        ap.error("give the text to speak, or --check")
    text = " ".join(args.text)
    sent = speak(text, host=args.host, port=args.port)
    if not enabled():
        print(f"[voice] not sent: {ENV_SWITCH} is off", file=sys.stderr)
    elif not sent:
        print(f"[voice] not sent to {host}:{port} (unresolvable host or empty text)",
              file=sys.stderr)
    else:
        print(f"[voice] sent to {host}:{port}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
