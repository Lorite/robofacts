#!/usr/bin/env bash
# =============================================================================
# Run one DECLARED operator action on one robot, from anywhere
# =============================================================================
# The actuation sibling of estop.sh (2026-08-10): posture and status commands
# (stand, sit, stow, lease, ...) declared per robot in robots.yaml `actions:`.
# Same philosophy as the stops — a NARROW ALLOWLIST: this script runs exactly
# what the config enumerates and nothing else, so the CLI, the web UI and any
# future rqt frontend cannot drift apart.
#
#   ./scripts/robot_action.sh quadruped ready          # claim + power + stand + arm carry
#   ./scripts/robot_action.sh quadruped sit
#   ./scripts/robot_action.sh quadruped lease          # who holds the body lease
#   ./scripts/robot_action.sh --list              # everything configured
#   ./scripts/robot_action.sh --dry-run quadruped sit  # print the wrapped command
#   ./scripts/robot_action.sh --host-level laptop quadruped sit
#
# The host is resolved like estop.sh's (2026-11-16): --host-level, else the
# running hardware trial of this machine, else the robot's host_level.
#
# ⚠️ Actions that CLAIM the lease (ready/stand) trip the observer's hijack
# policy if its node is already up — see the caution in robots.yaml. Keep the
# tablet app closed during rig sessions.
# =============================================================================

set -uo pipefail

# Where robots.yaml is: $TRIALS_ROBOTS_CONFIG, else <experiments root>/common/config/robots.yaml.
# The experiments root is $TRIALS_EXPERIMENTS_ROOT, else experiments/ above this script's
# directory (the project layout, where scripts/ links to the packaged tool), else the nearest
# experiments/ directory at or above the working directory.
find_robots_config() {
    if [ -n "${TRIALS_ROBOTS_CONFIG:-}" ]; then printf '%s\n' "$TRIALS_ROBOTS_CONFIG"; return; fi
    local rel="common/config/robots.yaml" root
    if [ -n "${TRIALS_EXPERIMENTS_ROOT:-}" ]; then printf '%s\n' "$TRIALS_EXPERIMENTS_ROOT/$rel"; return; fi
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    if [ -f "$root/experiments/$rel" ]; then printf '%s\n' "$root/experiments/$rel"; return; fi
    root="$PWD"
    while [ "$root" != "/" ]; do
        if [ -f "$root/experiments/$rel" ]; then printf '%s\n' "$root/experiments/$rel"; return; fi
        if [ -f "$root/$rel" ]; then printf '%s\n' "$root/$rel"; return; fi
        root="$(dirname "$root")"
    done
    printf '%s\n' "experiments/$rel"
}
CONFIG="$(find_robots_config)"
PKG_PARENT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
HOST_LEVEL=""
DRY_RUN=0
LIST=0
TIMEOUT=90

usage() { sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --list) LIST=1; shift ;;
        --config) CONFIG="$2"; shift 2 ;;
        --host-level) HOST_LEVEL="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done

[ -f "$CONFIG" ] || { echo "Robot config not found: $CONFIG" >&2; exit 2; }
if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || [ "$TIMEOUT" -lt 1 ]; then
    echo "--timeout must be a whole number of seconds, at least 1 (got '$TIMEOUT')" >&2
    exit 2
fi
INNER_TIMEOUT=$(( TIMEOUT > 3 ? TIMEOUT - 2 : 1 ))

if [ "$LIST" -eq 1 ]; then
    python3 - "$CONFIG" <<'PY'
import sys, yaml
with open(sys.argv[1]) as fh:
    data = yaml.safe_load(fh) or {}
for name, spec in (data.get("robots") or {}).items():
    actions = (spec or {}).get("actions") or {}
    if not actions:
        continue
    print(f"{name}:")
    for act, entry in actions.items():
        # An action may be a plain command string, the form estop.sh also reads.
        description = entry.get("description", "-") if isinstance(entry, dict) else "-"
        print(f"  {act:<8} {description}")
PY
    exit 0
fi

if [ "${#ARGS[@]}" -ne 2 ]; then
    echo "Usage: robot_action.sh [--dry-run] <robot> <action>   (or --list)" >&2
    exit 2
fi
ROBOT="${ARGS[0]}"
ACTION="${ARGS[1]}"

# Write the action's record to stdout: six fields, each ended by a NUL byte (2026-11-25, the
# reader of estop.sh): kind (ACTION or ERROR), host, container, shell, command, and the hostname
# inside the target's container ("-" when unknown). An ERROR record carries its message in the
# second field. Before 2026-11-25 the record was one TAB-separated line read with
# `IFS=$'\t' read`: a multi-line action ran only its first line, and its container_hostname was
# lost with the rest of the line, so inside any container the action ran right there, in a twin
# of the robot's container. An action written as a plain string crashed the reader.
read_action() {
    python3 - "$CONFIG" "$ROBOT" "$ACTION" "$HOST_LEVEL" "$PKG_PARENT" <<'PY'
import sys, yaml
config_path, robot, action, level, pkg = sys.argv[1:6]
out = sys.stdout.buffer


def emit(kind, *fields):
    record = [kind, *(str(f) for f in fields)]
    record += ["-"] * (6 - len(record))
    for field in record:
        out.write(field.replace("\0", " ").encode("utf-8", "replace") + b"\0")
    out.flush()
    sys.exit(0)


def action_command(entry):
    """The command of an action entry, in either documented form."""
    if isinstance(entry, str):
        command = entry
    elif isinstance(entry, dict):
        command = entry.get("command")
    else:
        raise ValueError(f"expected a command string or a mapping with 'command', "
                         f"got {type(entry).__name__}")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("it has no command")
    return command.strip()


try:
    with open(config_path) as fh:
        data = yaml.safe_load(fh) or {}
    robots = data.get("robots") or {}
    if not isinstance(robots, dict):
        raise ValueError(f"'robots' must map each robot to its settings, got {type(robots).__name__}")
except Exception as exc:  # a config that cannot be read stops the script, loudly
    emit("ERROR", f"cannot read {config_path} ({type(exc).__name__}: {exc})")
if robot not in robots:
    emit("ERROR", f"unknown robot {robot!r}; configured: {', '.join(sorted(map(str, robots)))}")
spec = robots[robot]
if not isinstance(spec, dict):
    emit("ERROR", f"robot {robot!r}: its entry must be a mapping, got {type(spec).__name__}")
actions = spec.get("actions") or {}
if not isinstance(actions, dict) or action not in actions:
    have = ", ".join(sorted(map(str, actions))) if isinstance(actions, dict) else "none"
    emit("ERROR", f"robot {robot!r} has no action {action!r}; declared: {have or 'none'}")
try:
    command = action_command(actions[action])
except Exception as exc:
    emit("ERROR", f"robot {robot!r}: its action {action!r} cannot be used "
                  f"({type(exc).__name__}: {exc})")
try:
    sys.path.insert(0, pkg)
    from robofacts import host_registry as hr, run_marker
    root = hr.experiments_root_of(config_path)
    marker = run_marker.read_live(root) if root else None
    target = hr.robot_targets(config_path, {robot: spec}, level=level or None,
                              marker=marker)[robot]
except Exception as exc:
    emit("ERROR", f"cannot resolve where {robot!r} runs ({type(exc).__name__}: {exc})")
emit("ACTION", target.host or "local", target.container or "-", target.shell or "zsh", command,
     target.container_hostname or "-")
PY
}

# Quote one argument for the next shell, as estop.sh does: printf %q writes a line break as
# $'\n', which a POSIX sh does not read, so a multi-line command is single-quoted instead,
# which every shell reads. A one-line command keeps the printf %q form.
quote() {
    local q
    q="$(printf '%q' "$1")"
    case "$q" in
        *"\$'"*) printf "'%s'" "${1//\'/\'\\\'\'}" ;;
        *) printf '%s' "$q" ;;
    esac
}

REC=()
while IFS= read -r -d '' field; do
    REC+=("$field")
done < <(read_action)
if [ "${#REC[@]}" -ne 6 ]; then
    echo "Could not read the action $ROBOT.$ACTION from $CONFIG (the reader failed)" >&2
    exit 2
fi
if [ "${REC[0]}" = "ERROR" ]; then
    echo "${REC[1]}" >&2
    exit 2
fi
HOST="${REC[1]}" CONTAINER="${REC[2]}" SHELL_BIN="${REC[3]}" COMMAND="${REC[4]}" CHOST="${REC[5]}"

wrapped="$COMMAND"
[ "$CONTAINER" = "-" ] && CONTAINER=""
# Already inside the target's own container (2026-11-17, the rule of estop.sh): run the
# command directly, there is no docker CLI in here. Inside another container, ssh as usual.
DOCKERENV="${TRIALS_DOCKERENV:-/.dockerenv}"
if [ -n "$CONTAINER" ] && [ -f "$DOCKERENV" ] \
    && { [ "${CHOST:--}" = "-" ] || [ "$(hostname)" = "$CHOST" ]; }; then
    # Still a login zsh: a container may default its middleware configuration only in /etc/zsh/zshenv.
    wrapped="timeout -k 2 $INNER_TIMEOUT $SHELL_BIN -lc $(quote "$COMMAND")"
    CONTAINER=""
    HOST="local"
fi
if [ -n "$CONTAINER" ]; then
    # zsh -lc, never bash: the middleware configuration may live in /etc/zsh/zshenv (the
    # documented false-dead trap).
    # The inner `timeout` bounds the command inside the container, because the
    # outer one only kills the local docker or ssh client (see estop.sh).
    wrapped="docker exec $CONTAINER timeout -k 2 $INNER_TIMEOUT $SHELL_BIN -lc $(quote "$COMMAND")"
fi
if [ "$HOST" != "local" ] && [ "$(hostname)" != "$HOST" ]; then
    wrapped="ssh -o ConnectTimeout=5 -o BatchMode=yes -o ForwardX11=no $HOST $(quote "$wrapped")"
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo "[$ROBOT.$ACTION] $wrapped"
    exit 0
fi

echo "=== $ROBOT.$ACTION ==="
timeout "$TIMEOUT" bash -c "$wrapped"
RC=$?
if [ "$RC" = "0" ]; then
    echo "=== OK $ROBOT.$ACTION"
else
    echo "=== FAIL $ROBOT.$ACTION (rc=$RC)" >&2
fi
exit "$RC"
