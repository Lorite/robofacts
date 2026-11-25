"""Run experiment commands on a remote computer via ssh + docker exec.

Pass `target=None` for local execution (behavior identical to plain subprocess).
Pass a `RemoteTarget` to wrap each command as

    ssh -tt <opts> <host> docker exec -i <container> zsh -c \
        'source <ros_setup> && source <ws_setup> && cd <workspace> && exec <argv>'

The workspace bind-mount path is assumed identical on laptop and Jetson dev
containers, so every Path passed to the wrapped command resolves the same on
both sides — only the command needs wrapping, plus a final rsync to pull bag
files back.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import shlex
import signal
import threading
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class RemoteTarget:
    host: str
    container: str
    workspace: Path
    ros_setup: str
    ws_setup: str
    shell: str = 'zsh'
    ssh_opts: tuple[str, ...] = field(default_factory=lambda: (
        '-o', 'ServerAliveInterval=15',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'StrictHostKeyChecking=accept-new',
        # No X11: the host ssh config enables ForwardX11, which prepends
        # "Warning: No xauth data..." to every remote command's output — noise
        # in exactly the places an operator reads for a pass/fail verdict.
        '-x',
    ))
    #: Further files to source after ros_setup and ws_setup, such as an overlay
    #: workspace beside the project's own (an estimator in ~/ov_ws, 2026-10-14).
    env_setup: tuple[str, ...] = ()


# pkill patterns for workspace-anchored processes, scoped by *kind* so a
# recorder stop does not accidentally kill the main launch on the same host.
#
# The backend's launcher supplies the patterns of its kinds (backend.Launcher.stop_patterns).
# What only a project knows comes from trials.yaml ``stop_patterns:``, whose ``kinds`` are
# extra kinds, regex templates with {workspace}:
#
#   stop_patterns:
#     kinds:
#       my_subsystem: "my_daemon.*{workspace}"
#
# A backend may read further keys of that block (wrapper scripts, containers it owns).


def _stop_settings() -> dict:
    try:
        from . import roots
    except ImportError:  # pragma: no cover - a copy outside the package
        return {}
    raw = roots.setting("stop_patterns") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"trials.yaml: stop_patterns must be a mapping, got {raw!r}")
    return raw


def _alternation(names: Iterable[str]) -> str:
    return "|".join(re.escape(str(n)) for n in names)


def _pkill_pattern_templates() -> dict[str, str]:
    """The kinds in force: the backend launcher's (all, recorder, launch) plus
    the project's ``stop_patterns.kinds``."""
    from . import backend
    settings = _stop_settings()
    templates = dict(backend.active().launcher.stop_patterns())
    extra = settings.get("kinds") or {}
    if not isinstance(extra, dict):
        raise ValueError(f"trials.yaml: stop_patterns.kinds must be a mapping, got {extra!r}")
    templates.update({str(k): str(v) for k, v in extra.items()})
    return templates


def _source_chain(files: Iterable[str]) -> str:
    return ''.join(f'source {shlex.quote(str(f))} && ' for f in files if f)


def wrap_command(cmd: list[str], target: RemoteTarget) -> list[str]:
    """Build the local argv that runs `cmd` inside the Jetson container."""
    inner = (
        _source_chain((target.ros_setup, target.ws_setup, *target.env_setup))
        + f'cd {shlex.quote(str(target.workspace))} && '
        f'exec {shlex.join(cmd)}'
    )
    docker_str = (
        f'docker exec -i {shlex.quote(target.container)} '
        f'{shlex.quote(target.shell)} -c {shlex.quote(inner)}'
    )
    return ['ssh', '-tt', *target.ssh_opts, target.host, docker_str]


def local_setup_command(cmd: list[str], files: Iterable[str], shell: str = 'zsh') -> list[str]:
    """The argv that runs `cmd` on THIS machine after sourcing `files`.

    For a runner that executes locally although the host spec names setup
    files: the rig cockpit runs the runner inside the rig's own container,
    where the ssh+docker wrapper (and with it the host's ros_setup, ws_setup
    and env_setup) is skipped. Without this, an overlay workspace declared
    for the host was sourced only when the trial ran remotely (2026-10-14).
    ``exec`` keeps the command's own argv, so the process-group stop and the
    pkill patterns see the same process as before.
    """
    chain = _source_chain(files)
    if not chain:
        return list(cmd)
    return [shell, '-c', chain + f'exec {shlex.join(cmd)}']


def start_background(
    cmd: list[str],
    target: RemoteTarget | None,
    *,
    cwd: Path,
    label: str = "",
    quiet_sink=None,
) -> subprocess.Popen:
    """Start a long-running process, mirroring its output through this process.

    The stack, the aux launches and the recorder used to inherit stdout
    directly, which put their output on the terminal of whoever started the
    runner and NOWHERE else — so the web UI showed a handful of runner prints
    while the interesting half (launch errors, node warnings) was visible
    only to a terminal nobody was watching.

    Piping means someone MUST drain the pipe or the child blocks once the OS
    buffer fills, so each process gets a daemon pump thread that re-prints its
    lines through ``print`` — reaching the terminal as before, and any stdout
    redirection (the UI's log tee) as well. Lines are labelled so a reader can
    tell the stack from the recorder.

    ``quiet_sink`` (2026-08-11, the cockpit-readability request): a writable
    file object that receives the tagged lines INSTEAD of ``print``. With the
    runner's stdout tee'd into the run log, every child line used to reach the
    operator terminal too, drowning the campaign narrative (gate verdicts,
    trial status, dwell countdown) in `[stack]` noise and making the cockpit's
    runner pane a duplicate of its log pane. Passing the run-log file here
    keeps the log complete while the terminal keeps only the narrative.
    """
    if target is None:
        proc = subprocess.Popen(cmd, cwd=str(cwd), preexec_fn=os.setsid,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    else:
        proc = subprocess.Popen(wrap_command(cmd, target), preexec_fn=os.setsid,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)

    tag = f"[{label}] " if label else ""

    def _pump() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if quiet_sink is not None:
                    quiet_sink.write(f"{tag}{line.rstrip()}\n")
                    quiet_sink.flush()
                else:
                    print(f"{tag}{line.rstrip()}", flush=True)
        except Exception:  # noqa: BLE001 - a dead pipe must never kill the run
            pass

    threading.Thread(target=_pump, name=f"pump-{label or 'proc'}", daemon=True).start()
    return proc


#: The foreground process currently running (the trial's action), so an
#: operator stop can end it and let the runner's ordered teardown do the rest.
#: Only one trial runs at a time, which is what makes a module-level handle
#: safe here.
_CURRENT_FOREGROUND: list = []
#: The pids of those processes that lead their own process group (a local action).
_OWN_GROUP: set = set()

#: How long an interrupted action may take to end (a flight lands) before the runner sends
#: SIGTERM, then SIGKILL. The same bound as an exploration's stop. No other signal reaches the
#: action before it: an action that lands in its Ctrl-C handler keeps the whole bound (until
#: the second review of 2026-11-25, SIGTERM followed after 5 s and cut such a landing short).
LAND_WAIT_S = 90.0
#: How long a process may take to end after SIGTERM before it gets SIGKILL.
TERM_GRACE_S = 5.0
#: The manual windows in progress (the action step disabled: the stack stays up for a
#: bounded time). A stop ends them like it ends an action (see hold_window).
_HOLDS: list = []


class RunnerSignal(KeyboardInterrupt):
    """A stop signal other than Ctrl-C (SIGTERM, SIGHUP) that the runner turns into the
    same path as a Ctrl-C: the action ends, the stack stops, the trial is written as
    aborted. ``signum`` names the signal."""

    def __init__(self, signum: int):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


class _HangupTolerant:
    """A standard stream whose writes never raise once its terminal is gone.

    After a hangup (the terminal closed) every write to it fails with EIO. Before the second
    review of 2026-11-25 an unguarded print then broke the interrupt path: the forwarded
    stop raised before the runner waited for the landing, each teardown step raised before
    it signalled its process, and the "teardown done" line raised out of the finally, so the
    stack and the recorder ran on and the trial got no metadata. The first failed write marks
    the stream gone, and later writes are dropped. The run log (a separate file) keeps them.
    """

    def __init__(self, stream):
        self._stream = stream
        self.gone = False

    def write(self, text):
        if not self.gone:
            try:
                return self._stream.write(text)
            except (OSError, ValueError):
                self.gone = True
        return len(text)

    def flush(self):
        if not self.gone:
            try:
                self._stream.flush()
            except (OSError, ValueError):
                self.gone = True

    def __getattr__(self, name):
        return getattr(self._stream, name)


@contextlib.contextmanager
def output_survives_hangup() -> Iterator[None]:
    """While the block runs, a write to stdout or stderr never raises (see _HangupTolerant).
    A stream that failed is left as os.devnull afterwards: flushing it again at interpreter
    exit would replace the runner's exit code with 120."""
    saved = (sys.stdout, sys.stderr)
    wrapped = (_HangupTolerant(sys.stdout), _HangupTolerant(sys.stderr))
    sys.stdout, sys.stderr = wrapped
    try:
        yield
    finally:
        restored = []
        for original, wrapper in zip(saved, wrapped):
            if wrapper.gone:
                try:
                    original.flush()
                except (OSError, ValueError):
                    pass
                restored.append(open(os.devnull, "w", encoding="utf-8"))
            else:
                restored.append(original)
        sys.stdout, sys.stderr = restored


def interrupt_signal_name(exc: BaseException) -> str:
    """SIGINT for a Ctrl-C, else the name of the signal a RunnerSignal carries."""
    return signal.Signals(getattr(exc, "signum", signal.SIGINT)).name


_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def raise_runner_signal(signum: int, frame=None) -> None:
    """The handler of SIGTERM and SIGHUP while the runner runs: the same path as a Ctrl-C."""
    raise RunnerSignal(signum)


@contextlib.contextmanager
def stop_signals_interrupt() -> Iterator[None]:
    """While the block runs, SIGTERM and SIGHUP raise RunnerSignal, a KeyboardInterrupt.
    Before 2026-11-25 they ended the runner at once: the stack and the recorder, each in
    its own session, ran on, and the trial got no metadata and no ledger line. A closed
    terminal sends SIGHUP, a service manager or another program SIGTERM."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    for s in old:
        signal.signal(s, raise_runner_signal)
    try:
        yield
    finally:
        _restore_handlers(old)


def _restore_handlers(old: dict) -> None:
    """Put the handlers back with the stop signals blocked, so a signal that arrives in the
    middle cannot leave some of them on a handler whose block has ended (a Ctrl-C there
    left SIGTERM and SIGHUP on a dead hold, second review of 2026-11-25). A signal that
    arrived meanwhile is delivered to the restored handler when they are unblocked."""
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set(old))
    try:
        for s, handler in old.items():
            signal.signal(s, handler)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


@contextlib.contextmanager
def interrupts_held(what: str) -> Iterator[list]:
    """Ctrl-C, SIGTERM and SIGHUP wait while the block runs, and the list yielded collects
    them: the caller raises the first one afterwards (see deferred_interrupt). Used for the
    teardown and the writing of an interrupted trial, which a second Ctrl-C cut short
    before 2026-11-25 (the stack stayed up after the runner exited)."""
    pending: list = []
    if threading.current_thread() is not threading.main_thread():
        yield pending
        return

    def hold(signum, frame):
        if not pending:
            print(f"[runner] {signal.Signals(signum).name} received: {what} first, "
                  f"then the run stops", flush=True)
        pending.append(signum)

    old = {s: signal.getsignal(s) for s in _STOP_SIGNALS}
    for s in old:
        signal.signal(s, hold)
    try:
        yield pending
    finally:
        _restore_handlers(old)


def deferred_interrupt(signum: int) -> KeyboardInterrupt:
    """The exception a held signal becomes: KeyboardInterrupt for SIGINT, else RunnerSignal."""
    return KeyboardInterrupt() if signum == signal.SIGINT else RunnerSignal(signum)


def _signal_process(proc: subprocess.Popen, sig: int) -> None:
    """``sig`` to the process, or to its whole group when it leads one of its own. The group
    gets the signal also after its leader has exited, while members remain."""
    try:
        if proc.pid in _OWN_GROUP:
            os.killpg(proc.pid, sig)
        else:
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    """Whether process group ``pgid`` still has a live member (zombies do not count).
    Without /proc (not Linux), whether the group can still be signalled."""
    if os.path.isdir("/proc"):
        return bool(_group_members(pgid))
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _running(proc: subprocess.Popen) -> bool:
    """The process runs, or, for an action that leads its own group, any process of that
    group runs: a wrapper that started the flight in the background and exited still has a
    flight to stop (found by the second review of 2026-11-25: the stop and the action timeout
    looked only at the wrapper and missed the flight)."""
    return proc.poll() is None or (proc.pid in _OWN_GROUP and _group_alive(proc.pid))


def terminate_current_foreground() -> bool:
    """End the running action, or the manual window, if any. Returns True if something was
    signalled.

    Deliberately a TERMINATE of the action rather than a kill of the stack:
    run_trial's finally then stops the recorder first and the stack after, so
    the bag is finalised and nothing is orphaned. Killing the stack directly
    would truncate the recording, the failure the teardown order exists to
    avoid. A local action leads its own process group, and the whole group gets
    the signal: a wrapper (a shell script, a launcher) does not pass SIGTERM on
    to the flight it started (found 2026-11-25), and a flight it started in the
    background outlives the wrapper. A manual window (see hold_window) ends too.
    """
    for event in list(_HOLDS):
        event.set()
        return True
    for proc in list(_CURRENT_FOREGROUND):
        if _running(proc):
            # Marked first, so the action's own end sees it: members of the group that
            # outlive the leader are then awaited and ended (_run_streamed), not left.
            proc._robofacts_stopped = True
            _signal_process(proc, signal.SIGTERM)
            return True
    return False


def end_current_action(reason: BaseException) -> bool:
    """From another thread, end the action in flight the way an interrupt of the runner ends
    it (see _end_interrupted: the signal, the wait for the landing, then SIGTERM and SIGKILL),
    and end a manual window. Returns True if there was one. The web interface calls it when
    its server is stopped with Ctrl-C: the worker thread that runs the trial gets no signal,
    and before the second review of 2026-11-25 the server exited with the action still
    flying."""
    found = False
    for event in list(_HOLDS):
        event.set()
        found = True
    for proc in list(_CURRENT_FOREGROUND):
        if _running(proc):
            found = True
            _end_interrupted(proc, reason, action=getattr(proc, "_robofacts_action", False))
    return found


def hold_window(seconds: float) -> bool:
    """Wait ``seconds`` while the stack stays up (the manual window of a trial whose action
    step is disabled). Returns True when a stop (terminate_current_foreground) ended the
    window early. A Ctrl-C interrupts the wait as before. Before the second review of
    2026-11-25 the window was a plain sleep: the web interface's stop did not end it, and the
    trial was written as a complete one."""
    event = threading.Event()
    _HOLDS.append(event)
    try:
        return event.wait(max(0.0, seconds))
    finally:
        _HOLDS.remove(event)


def _drain(proc: subprocess.Popen, captured: list) -> threading.Thread:
    """Keep printing the output of a process whose reader was interrupted, so it never
    blocks on a full pipe while it ends."""
    def _pump():
        try:
            for line in proc.stdout:
                print(line, end="", flush=True)
                captured.append(line)
        except (OSError, ValueError):
            pass
    thread = threading.Thread(target=_pump, name="drain-foreground", daemon=True)
    thread.start()
    return thread


def _end_interrupted(proc: subprocess.Popen, exc: BaseException, *, action: bool,
                     signalled: bool = False) -> None:
    """The runner leaves a foreground process early (Ctrl-C, SIGTERM, SIGHUP, an error).

    An action must END before the runner's teardown stops the recorder and the stack: a
    flight still landing needs its stack up, and a live action must never be left behind
    (found 2026-11-25: on Ctrl-C the stack went down during the landing, and after a
    decoding error the action kept flying into the next trial). So the signal that
    interrupted the runner goes on to the action (a Ctrl-C at the terminal no longer
    reaches an action that leads its own group, and any other cause becomes SIGTERM), the
    runner waits up to LAND_WAIT_S for the whole group to end, then sends SIGTERM, and
    SIGKILL after TERM_GRACE_S. A second Ctrl-C kills it at once. Any other command gets
    the same sequence with a 5 s wait instead of LAND_WAIT_S.

    The signals go to the action's process group as long as any member lives, also after
    the group leader has exited: a wrapper that starts the flight in the background and
    exits leaves the flight in the group (second review of 2026-11-25).

    ``signalled``: the group already got its stop (terminate_current_foreground), so the
    first signal is not sent again: a second SIGTERM can restart a landing."""
    if not _running(proc):
        return
    # A Ctrl-C goes on as SIGINT, which the action got from the terminal before it led its own
    # group. Any other cause gets SIGTERM, the signal an action lands on (a closed terminal's
    # SIGHUP would end most scripts without a landing).
    first = (signal.SIGINT if isinstance(exc, KeyboardInterrupt)
             and getattr(exc, "signum", signal.SIGINT) == signal.SIGINT else signal.SIGTERM)
    wait_s = LAND_WAIT_S if action else 5.0
    if signalled:
        first = signal.SIGTERM
    else:
        _signal_process(proc, first)
    if action:
        print(f"[runner] waiting up to {wait_s:.0f} s for the action to end (a flight lands) "
              f"before the stack stops; Ctrl-C again kills it now", flush=True)
    t0 = time.monotonic()
    try:
        while _running(proc) and time.monotonic() - t0 < wait_s:
            time.sleep(0.1)
        if _running(proc) and first != signal.SIGTERM:
            print(f"[runner] the action did not end within {time.monotonic() - t0:.0f} s: "
                  f"SIGTERM", flush=True)
            _signal_process(proc, signal.SIGTERM)
            t1 = time.monotonic()
            while _running(proc) and time.monotonic() - t1 < TERM_GRACE_S:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("[runner] second interrupt: the action is killed now", flush=True)
    if _running(proc):
        print(f"[runner] the action did not end within {time.monotonic() - t0:.0f} s: SIGKILL",
              flush=True)
        _signal_process(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        if proc.pid in _OWN_GROUP:
            _await_group(proc.pid, 2.0)


def _end_stopped_leftovers(proc: subprocess.Popen) -> None:
    """A stop (the web interface's stop, the action timeout) reached the action's group, its
    leader has exited, and members of the group still run: a flight that a wrapper started
    in the background, still landing. They end before the runner goes on to the teardown,
    the same way an interrupted action ends (landing time, then SIGKILL)."""
    members = _group_members(proc.pid)
    print(f"[runner] the action was stopped and its leader exited, but "
          f"{len(members) or 'some'} process(es) of its group still run"
          f"{': ' + _describe(members) if members else ''}. The runner waits for them "
          f"before the stack stops", flush=True)
    _end_interrupted(proc, RunnerSignal(signal.SIGTERM), action=True, signalled=True)


def _report_leftovers(proc: subprocess.Popen, rc: int) -> None:
    """The action ended on its own and left processes in its group: only a warning.

    Until the third review of 2026-11-25 they were stopped and the trial failed. But a
    process the action leaves on purpose is not a flight: a middleware's command-line tools
    can start a shared daemon on first use, without a new session, so the daemon stays in
    the caller's process group. Every trial whose action asked such a tool a question
    failed, and the shared daemon was killed. An action that must not leave a flight behind
    waits for it before it exits.
    """
    members = _group_members(proc.pid)
    print(f"[runner] WARNING the action exited (rc={rc}) and left {len(members) or 'some'} "
          f"process(es) of its group running{': ' + _describe(members) if members else ''}. "
          f"They are not stopped (a daemon that a tool of the action started, for example). "
          f"If one of them flies, stop it before the next trial", flush=True)


def _run_streamed(cmd: list[str], target: RemoteTarget | None, *, cwd: Path, check: bool,
                  action: bool) -> subprocess.CompletedProcess:
    final = cmd if target is None else wrap_command(cmd, target)
    kwargs: dict = {"cwd": str(cwd)} if target is None else {}
    own_group = action and target is None
    if own_group:
        kwargs["start_new_session"] = True
    # errors="replace": one byte that is not UTF-8 in the output must not end the reading
    # and abandon a running action (found 2026-11-25).
    proc = subprocess.Popen(final, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", bufsize=1, **kwargs)
    if own_group:
        _OWN_GROUP.add(proc.pid)
    proc._robofacts_action = action
    _CURRENT_FOREGROUND.append(proc)
    captured: list[str] = []
    assert proc.stdout is not None
    try:
        try:
            for line in proc.stdout:
                print(line, end="", flush=True)
                captured.append(line)
            rc = proc.wait()
            if own_group and _group_alive(proc.pid):
                if getattr(proc, "_robofacts_stopped", False):
                    _end_stopped_leftovers(proc)
                    # A stop ended the action, so it did not complete, whatever its leader
                    # returned.
                    rc = rc or -signal.SIGTERM
                else:
                    _report_leftovers(proc, rc)
        except BaseException as exc:
            drain = _drain(proc, captured)
            _end_interrupted(proc, exc, action=action)
            drain.join(timeout=1.0)
            raise
    finally:
        if proc in _CURRENT_FOREGROUND:
            _CURRENT_FOREGROUND.remove(proc)
        _OWN_GROUP.discard(proc.pid)
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, final, output="".join(captured))
    return subprocess.CompletedProcess(final, rc, stdout="".join(captured), stderr="")


def run_foreground(
    cmd: list[str],
    target: RemoteTarget | None,
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run to completion, streaming output live AND capturing it.

    Streaming matters because the action is a flight: an operator watching the
    terminal must see it as it happens, not in one dump at the end. Capturing
    matters because whoever is watching the *web UI* only ever saw a return
    code: when the trajectory script rejected its arguments (2026-07-29) the
    message existed solely in the terminal, so the UI reported a trial that
    never flew as a successful one.
    """
    return _run_streamed(cmd, target, cwd=cwd, check=check, action=False)


def run_action(
    cmd: list[str],
    target: RemoteTarget | None,
    *,
    cwd: Path,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """run_foreground for the trial's action. Locally the action leads its own process
    group, so a stop (the action timeout, the web interface's stop) reaches the whole
    flight and not only a wrapper. When the runner is interrupted, the action ends first
    (see _end_interrupted), then the runner's teardown stops the recorder and the stack."""
    return _run_streamed(cmd, target, cwd=cwd, check=check, action=True)


def remote_sh(target: RemoteTarget, script: str,
              timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a plain /bin/sh snippet in the target container — no setup file sourced.

    Separate from `wrap_command` on purpose: these are small filesystem
    questions ("is there a bag here?", "what is checked out here?"), and
    sourcing a workspace overlay to ask them would make the answer depend on
    whether the overlay builds.

    `timeout` raises subprocess.TimeoutExpired — for callers whose question is
    optional and must not hold up a finished trial.
    """
    docker_str = (f'docker exec -i {shlex.quote(target.container)} '
                  f'sh -c {shlex.quote(script)}')
    return subprocess.run(['ssh', *target.ssh_opts, target.host, docker_str],
                          capture_output=True, text=True, timeout=timeout)


#: Pre-2026-08-04 name, kept so nothing that reached for the private helper breaks.
_remote_sh = remote_sh


def write_trial_file(target: RemoteTarget | None, path: Path, content: str) -> None:
    """Write `content` to `path` on the host that recorded the bag.

    A remote trial records its bag on the rig, so its metadata belongs there
    too. Writing it next to the *runner* instead leaves a metadata-only
    directory on the laptop that is indistinguishable from a finished trial —
    which is exactly how a remote run that recorded nothing at all once
    reported itself as satisfied coverage.

    The file is written whole or not at all (2026-11-25, review B2-5): into a temporary
    file in the same directory, then renamed over the final name, on the rig in the
    shell as well. A disk that filled during the write used to leave a 0-byte
    trial_metadata.json, which hid the trial from the index and kept the runner from
    ever repairing it. A failed write raises and leaves no file.
    """
    if target is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'{path.name}.tmp.{os.getpid()}')
        try:
            tmp.write_text(content, encoding='utf-8')
            fd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return
    # base64 so arbitrary JSON survives two levels of shell quoting intact.
    blob = base64.b64encode(content.encode('utf-8')).decode('ascii')
    final = shlex.quote(str(path))
    script = (f'mkdir -p {shlex.quote(str(path.parent))} && t={final}.tmp.$$ && '
              f'printf %s {shlex.quote(blob)} | base64 -d > "$t" && mv -f "$t" {final} '
              f'|| {{ rm -f "$t"; exit 1; }}')
    result = _remote_sh(target, script)
    if result.returncode != 0:
        raise RuntimeError(
            f'could not write {path} on {target.host}: {result.stderr.strip()}')


def recording_has_data(target: RemoteTarget | None, directory: Path) -> tuple[bool, str]:
    """Did the recorder leave a complete recording in ``directory``, asked on the host that
    recorded? The backend's recorder answers (backend.Recorder.has_data)."""
    from . import backend
    return backend.active().recorder.has_data(target, directory)


#: The name from before the backends existed.
bag_has_data = recording_has_data


#: The kinds every caller may name. A backend whose launcher gives no pattern for one of
#: them (stopping the process group is enough for it) makes that sweep a no-op.
STANDARD_STOP_KINDS = ('all', 'recorder', 'launch')


def _resolve_pkill_pattern(kind: str, workspace: Path) -> str | None:
    try:
        templates = _pkill_pattern_templates()
        if kind not in templates and kind in STANDARD_STOP_KINDS:
            return None
        template = templates[kind]
    except KeyError as exc:
        valid = ', '.join(sorted(templates))
        raise ValueError(f'pkill kind must be one of {{{valid}}}, got {kind!r}') from exc
    return template.format(workspace=str(workspace))


def _pkill_local(pattern: str, sig: str) -> None:
    try:
        subprocess.run(
            ['pkill', f'-{sig}', '-f', pattern],
            check=False, timeout=10,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        pass


def _pkill_remote(target: RemoteTarget, pattern: str, sig: str) -> None:
    pkill_cmd = ['pkill', f'-{sig}', '-f', pattern]
    wrapped = wrap_command(pkill_cmd, target)
    try:
        subprocess.run(wrapped, check=False, timeout=10,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        pass


def _launch_file_pattern(proc: subprocess.Popen) -> str | None:
    """The launch-file token from ``proc.args``, pkill-ready, or None.

    Package-form launches (a launch file named inside a package) carry no
    workspace path in their remote argv, so the workspace-anchored templates
    alone cannot match them — a teardown that relies only on those templates
    leaves the whole remote stack running. The launch file name is the one
    distinctive token that IS in the remote cmdline.
    """
    args = proc.args if isinstance(proc.args, (list, tuple)) else [str(proc.args)]
    for tok in ' '.join(map(str, args)).split():
        tok = tok.strip('\'"')
        if '.launch.' in tok:
            return re.escape(tok)
    return None


def pkill_workspace_launches(
    target: RemoteTarget | None,
    workspace: Path,
    sig: str = 'INT',
    *,
    kind: str = 'all',
    extra: str | None = None,
) -> None:
    """Kill workspace-anchored processes matching ``kind``.

    ``kind`` selects which processes get killed:
      - ``'all'`` (default): every launch, run and recorder process under workspace.
      - ``'recorder'``: only the recorder and the project's recorder wrappers.
      - ``'launch'``: only the launch and run processes (no recorders).
      - any kind the project adds in trials.yaml ``stop_patterns.kinds``, for
        example nodes matched by executable name so reparented orphans of a dead
        launch are reaped too.

    Safe to call even when no Popen handle is available (e.g. after a kernel
    restart that lost ACTIVE_PROCESSES). The pkill pattern is anchored to
    ``workspace`` so unrelated processes are never touched. ``extra`` extends
    the pattern with one more alternation (e.g. the launch file name from
    ``_launch_file_pattern``, whose docstring explains why the anchored
    templates alone are not enough for package-form launches).
    """
    pattern = _resolve_pkill_pattern(kind, target.workspace if target is not None else workspace)
    if pattern is None and not extra:
        return
    if extra:
        pattern = f'{pattern}|{extra}' if pattern else extra
    if target is None:
        _pkill_local(pattern, sig)
    else:
        _pkill_remote(target, pattern, sig)


def _group_members(pgid: int) -> list[tuple[int, str]]:
    """(pid, command line) of every live process in process group ``pgid``.

    Reads /proc (Linux), skips zombies (exited, only waiting to be reaped: the
    dev container's PID 1 is a shell that never reaps them) and returns [] when
    /proc cannot be read.
    """
    members: list[tuple[int, str]] = []
    try:
        pids = [e for e in os.listdir('/proc') if e.isdigit()]
    except OSError:
        return members
    for pid in pids:
        try:
            with open(f'/proc/{pid}/stat', encoding='utf-8', errors='replace') as handle:
                fields = handle.read().rsplit(')', 1)[-1].split()
            if len(fields) < 3 or fields[0] == 'Z' or int(fields[2]) != pgid:
                continue
            with open(f'/proc/{pid}/cmdline', 'rb') as handle:
                cmd = handle.read().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
        except (OSError, ValueError):
            continue
        members.append((int(pid), cmd))
    return members


def _await_group(pgid: int, timeout_s: float) -> list[tuple[int, str]]:
    """Wait up to ``timeout_s`` for group ``pgid`` to empty. Returns the survivors."""
    deadline = time.monotonic() + timeout_s
    while True:
        members = _group_members(pgid)
        if not members or time.monotonic() >= deadline:
            return members
        time.sleep(0.1)


def _describe(members: list[tuple[int, str]]) -> str:
    return ', '.join(f'{pid} ({cmd[:60] or "?"})' for pid, cmd in members[:4]) + \
        (f' and {len(members) - 4} more' if len(members) > 4 else '')


def _stop_group_survivors(pgid: int, name: str, after: str, t0: float,
                          t_term: float, t_kill: float) -> None:
    """The leader has exited. Report the stop truthfully and finish the group.

    2026-10-13 (adoption test of the SAC 2027 framework paper): the run log said
    "stopped gracefully rc=0 in 5.1 s" while the real ``gz sim`` server, a member
    of the same process group, survived the SIGINT as an orphan, because only
    the group leader was awaited. A process that left the group (setsid) is
    not seen here.
    """
    survivors = _await_group(pgid, t_term)
    if not survivors:
        print(f'[{name}] {after} in {time.monotonic() - t0:.1f} s, process group {pgid} empty')
        return
    print(f'[{name}] group leader {after}, but {len(survivors)} process(es) of group {pgid} '
          f'survived: {_describe(survivors)} - SIGTERM to the group')
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    survivors = _await_group(pgid, t_kill)
    if survivors:
        print(f'[{name}] SIGKILL to group {pgid}: {_describe(survivors)}')
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        survivors = _await_group(pgid, 2.0)
    if survivors:
        print(f'[{name}] WARNING: {len(survivors)} process(es) of group {pgid} are still alive '
              f'after SIGKILL: {_describe(survivors)}')
    else:
        print(f'[{name}] orphaned group members stopped, NOT a graceful stop, '
              f'{time.monotonic() - t0:.1f} s in total')


def _process_group(proc: subprocess.Popen) -> int:
    """The process group to signal for ``proc``.

    Every process that start_background starts leads its own group (setsid), so its pid is the
    group id. os.getpgid asks the kernel only while the process is not yet reaped: after
    that it raises ProcessLookupError, and the pid may already belong to another process
    (third review of 2026-11-25: the stop of a stack whose ssh client had died and been
    polled raised there, and the remote pkill that should have reached the launch in the
    container never left)."""
    if proc.poll() is None:
        try:
            return os.getpgid(proc.pid)
        except ProcessLookupError:
            pass
    return proc.pid


def _killpg(pgid: int, sig: int) -> None:
    """``sig`` to group ``pgid``, if it still exists. An empty group is not an error: the
    remote pkill and the rest of the stop must still run."""
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop_remote_process(
    proc: subprocess.Popen,
    target: RemoteTarget | None,
    *,
    name: str,
    timeouts: tuple[int, int, int] = (20, 8, 3),
    kind: str = 'all',
    workspace: Path | None = None,
) -> None:
    """SIGINT -> SIGTERM -> SIGKILL on local PGID.

    When a remote target is set the local proc is just an SSH tunnel — the
    actual process lives in the remote container and must be signalled
    via pkill at each escalation level before (or instead of) touching the
    tunnel, otherwise only the tunnel dies and the remote process keeps running.

    ``kind`` scopes the remote pkill so a per-recorder stop does not also kill
    the long-running main launch on the same host. Pass ``'recorder'`` when
    stopping a bag recorder, ``'launch'`` for a standalone launch, or leave
    the default ``'all'`` for the full-stack teardown.

    For a **local** stop (``target is None``) the pkill backstop only runs when
    an explicit ``workspace`` is passed: the process-group kill already covers
    the local tree, and a guessed anchor (the old ``Path(proc.args[0]).parent``
    fallback could resolve to ``'.'``) turns the pkill regex into a match-anything
    pattern that kills every matching process on the machine.

    The remote pkill leaves before anything touches the local group, and a local client that
    has already exited (an ssh connection lost with the Wi-Fi) does not stop the stop: the
    remote processes still get their signal, and the report says that their end could not
    be watched (third review of 2026-11-25).
    """
    t_int, t_term, t_kill = timeouts
    _t0 = time.monotonic()   # 2026-09-11: report how long each stop took (post-flight profiling)
    if workspace is None and target is not None:
        workspace = target.workspace
    extra = _launch_file_pattern(proc)
    client_gone = proc.poll() is not None

    if target is not None:
        # Signal the real remote processes first; the tunnel will exit naturally
        # once they terminate (TTY close propagates back via -tt).
        print(f'[{name}] SIGINT -> remote processes via pkill (kind={kind})')
        pkill_workspace_launches(target, workspace, 'INT', kind=kind, extra=extra)
    pgid = _process_group(proc)
    # Signal first, then say so: after a hangup a print could fail before the signal left.
    _killpg(pgid, signal.SIGINT)
    print(f'[{name}] SIGINT -> local process group {pgid}')
    if client_gone and target is not None:
        print(f'[{name}] its ssh client had already exited (rc={proc.returncode}) before the '
              f'stop: the remote processes got SIGINT via pkill, but whether they ended '
              f'cannot be watched from here')
    try:
        proc.wait(timeout=t_int)
        _stop_group_survivors(pgid, name, (f'had already exited rc={proc.returncode}'
                                           if client_gone else
                                           f'stopped gracefully rc={proc.returncode}'), _t0,
                              t_term, t_kill)
        return
    except subprocess.TimeoutExpired:
        print(f'[{name}] SIGTERM fallback')

    # Escalate: for remote, escalate pkill to TERM; for local, retry INT as a
    # backstop for child processes that may have been missed.
    if workspace is not None:
        pkill_workspace_launches(
            target, workspace,
            'INT' if target is None else 'TERM',
            kind=kind,
            extra=extra,
        )
    _killpg(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=t_term)
        _stop_group_survivors(pgid, name, f'stopped with SIGTERM rc={proc.returncode}', _t0,
                              t_term, t_kill)
        return
    except subprocess.TimeoutExpired:
        print(f'[{name}] SIGKILL fallback')

    if workspace is not None:
        pkill_workspace_launches(target, workspace, 'KILL', kind=kind, extra=extra)
    _killpg(pgid, signal.SIGKILL)
    proc.wait(timeout=t_kill)
    print(f'[{name}] killed rc={proc.returncode}')


def rsync_back(
    target: RemoteTarget,
    paths: Iterable[Path],
) -> dict[Path, int]:
    """Pull each `path` from <host>:<path>/ to <path>/ via rsync.

    Uses `--rsync-path='docker exec -i <container> rsync'` so the remote rsync
    runs inside the dev container (where the bag files live as the container
    user). Returns {path: returncode}.
    """
    results: dict[Path, int] = {}
    rsync_path = f'docker exec -i {target.container} rsync'
    ssh_e = 'ssh ' + ' '.join(shlex.quote(o) for o in target.ssh_opts) if target.ssh_opts else 'ssh'
    for path in paths:
        src = f'{target.host}:{path}/'
        dst = f'{path}/'
        Path(path).mkdir(parents=True, exist_ok=True)
        cmd = [
            'rsync', '-a', '--info=progress2', '--partial', '--human-readable',
            '--rsync-path', rsync_path,
            '-e', ssh_e,
            src, dst,
        ]
        completed = subprocess.run(cmd, check=False)
        results[Path(path)] = completed.returncode
    return results


def package_module_command(project_root: Path, module: str, args: list,
                           package: str = "robofacts") -> list:
    """A command that runs one package module on another host.

    The project's shim ``experiments/common/scripts/<module>.py`` when the checkout has one
    (it finds the package in the source tree without a build), else ``python3 -m`` on the
    installed package, so a project without the shims works too.
    """
    shim = shlex.quote(str(Path(project_root) / "experiments" / "common" / "scripts" / f"{module}.py"))
    rest = " ".join(shlex.quote(a) for a in args)
    return ["sh", "-c", f"if [ -f {shim} ]; then exec python3 {shim} {rest}; "
                        f"else exec python3 -m {package}.{module} {rest}; fi"]
