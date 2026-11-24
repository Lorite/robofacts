"""Run experiment ROS 2 commands on a remote NVIDIA Jetson via ssh + docker exec.

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
import os
import re
import shlex
import signal
import threading
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


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
    #: workspace that is not part of ros2_ws (OpenVINS in ~/ov_ws, 2026-10-14).
    env_setup: tuple[str, ...] = ()


# pkill patterns for workspace-anchored ros2 processes, scoped by *kind* so a
# recorder stop does not accidentally kill the main launch on the same host.
#
# ros2 launch/bag record/run have the workspace path in their *arguments*
# (a config file or the bag directory under the workspace), so the command token
# comes first: "ros2 launch.*{workspace}".
#
# What only a project knows comes from trials.yaml ``stop_patterns:``:
#
#   stop_patterns:
#     recorder_scripts: [record_my_bag]       # bash wrappers that exec ros2 bag record.
#                                             # They carry the workspace in their path.
#     owned_containers: [my_container]        # composable containers the project's launches
#                                             # start. Their argv holds neither the workspace
#                                             # nor "ros2 launch", so an orphan survives an
#                                             # unclean shutdown and its detectors stack with
#                                             # the next launch's. Matched by node name.
#     kinds:                                  # extra kinds, regex templates with {workspace}
#       my_subsystem: "ros2 launch.*my_subsystem"
#
# The ROS 2 daemon (/usr/bin/python3 -c "from ros2cli.daemon…") is intentionally
# NOT matched by any of these — never kill the daemon.


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
    """The kinds in force: the backend launcher's (for ROS 2: all, recorder, launch) plus
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
    while the interesting half (ROS launch errors, node warnings) was visible
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


def terminate_current_foreground() -> bool:
    """End the running action, if any. Returns True if something was signalled.

    Deliberately a TERMINATE of the action rather than a kill of the stack:
    run_trial's finally then stops the recorder first and the stack after, so
    the bag is finalised and nothing is orphaned. Killing the stack directly
    would truncate the recording — the failure the teardown order exists to
    avoid.
    """
    for proc in list(_CURRENT_FOREGROUND):
        if proc.poll() is None:
            proc.terminate()
            return True
    return False


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
    code — when the trajectory script rejected its arguments (2026-07-29) the
    message existed solely in the terminal, so the UI reported a trial that
    never flew as a successful one.
    """
    final = cmd if target is None else wrap_command(cmd, target)
    kwargs = {"cwd": str(cwd)} if target is None else {}
    proc = subprocess.Popen(final, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, **kwargs)
    _CURRENT_FOREGROUND.append(proc)
    captured: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        captured.append(line)
    rc = proc.wait()
    if proc in _CURRENT_FOREGROUND:
        _CURRENT_FOREGROUND.remove(proc)
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, final, output="".join(captured))
    return subprocess.CompletedProcess(final, rc, stdout="".join(captured), stderr="")


def remote_sh(target: RemoteTarget, script: str,
              timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a plain /bin/sh snippet in the target container — no ROS sourcing.

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
    """
    if target is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return
    # base64 so arbitrary JSON survives two levels of shell quoting intact.
    blob = base64.b64encode(content.encode('utf-8')).decode('ascii')
    script = (f'mkdir -p {shlex.quote(str(path.parent))} && '
              f'printf %s {shlex.quote(blob)} | base64 -d > {shlex.quote(str(path))}')
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

    Package-form launches (``ros2 launch <pkg> <file>.launch.py``) carry no
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
    """Kill workspace-anchored ros2 processes matching ``kind``.

    ``kind`` selects which processes get killed:
      - ``'all'`` (default): every ros2 launch/run/bag record under workspace.
      - ``'recorder'``: only ros2 bag record + the record_*_bag.sh wrappers.
      - ``'launch'``: only ros2 launch / ros2 run (no bag recorders).
      - any kind the project adds in trials.yaml ``stop_patterns.kinds``, for
        example nodes matched by executable name so reparented orphans of a dead
        ``ros2 launch`` are reaped too.

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
    actual ROS 2 process lives in the remote container and must be signalled
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
    pattern that kills every ros2 process on the machine.
    """
    t_int, t_term, t_kill = timeouts
    _t0 = time.monotonic()   # 2026-09-11: report how long each stop took (post-flight profiling)
    if workspace is None and target is not None:
        workspace = target.workspace
    pgid = os.getpgid(proc.pid)
    extra = _launch_file_pattern(proc)

    if target is not None:
        # Signal the real remote processes first; the tunnel will exit naturally
        # once they terminate (TTY close propagates back via -tt).
        print(f'[{name}] SIGINT -> remote processes via pkill (kind={kind})')
        pkill_workspace_launches(target, workspace, 'INT', kind=kind, extra=extra)
    print(f'[{name}] SIGINT -> local process group {pgid}')
    os.killpg(pgid, signal.SIGINT)
    try:
        proc.wait(timeout=t_int)
        _stop_group_survivors(pgid, name, f'stopped gracefully rc={proc.returncode}', _t0,
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
    os.killpg(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=t_term)
        _stop_group_survivors(pgid, name, f'stopped with SIGTERM rc={proc.returncode}', _t0,
                              t_term, t_kill)
        return
    except subprocess.TimeoutExpired:
        print(f'[{name}] SIGKILL fallback')

    if workspace is not None:
        pkill_workspace_launches(target, workspace, 'KILL', kind=kind, extra=extra)
    os.killpg(pgid, signal.SIGKILL)
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
