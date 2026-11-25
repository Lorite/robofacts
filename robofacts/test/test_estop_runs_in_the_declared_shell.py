"""estop.sh runs a stop typed inside the robot's own container in the declared login shell.

Review finding (2026-11-25). Inside the target's container estop.sh ran the stop command in the
plain ``bash -c`` that runs every route, not in ``zsh -lc`` as its own comment, hosts.yaml and
robot_action.sh require. A container that sets its middleware configuration only in
/etc/zsh/zshenv then sent the stop without discovery: it reached nothing and could still report
OK. These tests use stubs of docker, ssh, hostname and the declared shell, so nothing outside the
test's own folder is reached.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ESTOP = Path(__file__).resolve().parents[1] / "robofacts" / "tools" / "estop.sh"

#: The declared shell. It records how it was called, then runs the command like a shell would.
FAKE_SHELL = """#!/usr/bin/env bash
printf '%s\\n' "$@" >"$SHELL_LOG"
[ "$1" = -lc ] || exit 91
exec bash -c "$2"
"""

#: Any docker or ssh call would mean the stop left this container: refuse it loudly.
REFUSE = "#!/usr/bin/env bash\necho \"stub refuses: $0 $*\" >&2\nexit 97\n"


def run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    (tmp_path / "hosts.yaml").write_text("""
version: 1
rig: robot
hosts:
  laptop: {local: true}
  robot:
    host: my-robot-host
    container: robot_dev
    container_hostname: robot-dev
    shell: fakezsh
""", encoding="utf-8")
    marker = tmp_path / "stopped"
    (tmp_path / "robots.yaml").write_text(f"""
version: 1
robots:
  drone:
    host_level: robot
    verified: true
    estop: {{description: cut, command: 'echo stop > {marker}'}}
""", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("fakezsh", FAKE_SHELL), ("docker", REFUSE), ("ssh", REFUSE),
                       ("hostname", "#!/usr/bin/env bash\necho robot-dev\n")):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    (tmp_path / "dockerenv").write_text("", encoding="utf-8")   # inside a container
    env = {k: v for k, v in os.environ.items()
           if k not in ("RIG_HOST", "RIG_CONTAINER", "TRIALS_EXPERIMENTS_ROOT", "TRIALS_ROBOTS_CONFIG")}
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["TRIALS_DOCKERENV"] = str(tmp_path / "dockerenv")
    env["SHELL_LOG"] = str(tmp_path / "shell.log")
    result = subprocess.run(["bash", str(ESTOP), "--config", str(tmp_path / "robots.yaml"), *args],
                            capture_output=True, text=True, env=env, timeout=60)
    return result, marker


def test_the_dry_run_shows_the_declared_login_shell_and_the_inner_limit(tmp_path):
    dry, _ = run(tmp_path, "--dry-run")
    assert dry.returncode == 0, dry.stdout + dry.stderr
    line = next(line for line in dry.stdout.splitlines() if line.startswith("  [drone]"))
    assert line.startswith("  [drone] timeout -k 2 13 fakezsh -lc "), line
    assert "ssh" not in line and "docker" not in line, line
    listed, _ = run(tmp_path, "--list")
    assert "route:  runs directly here, in this container (fakezsh -lc)" in listed.stdout, listed.stdout


def test_the_stop_runs_in_the_declared_login_shell(tmp_path):
    result, marker = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text().strip() == "stop", "the stop command did not run"
    log = tmp_path / "shell.log"
    assert log.exists(), "the stop ran in bash, not in the declared login shell"
    assert log.read_text().splitlines()[0] == "-lc"
