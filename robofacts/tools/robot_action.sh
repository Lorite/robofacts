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
#   ./scripts/robot_action.sh spot ready          # claim + power + stand + arm carry
#   ./scripts/robot_action.sh spot sit
#   ./scripts/robot_action.sh spot lease          # who holds the body lease
#   ./scripts/robot_action.sh --list              # everything configured
#   ./scripts/robot_action.sh --dry-run spot sit  # print the wrapped command
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
        print(f"  {act:<8} {entry.get('description', '-')}")
PY
    exit 0
fi

if [ "${#ARGS[@]}" -ne 2 ]; then
    echo "Usage: robot_action.sh [--dry-run] <robot> <action>   (or --list)" >&2
    exit 2
fi
ROBOT="${ARGS[0]}"
ACTION="${ARGS[1]}"

# One TAB-separated line: host, container, shell, command (same emit rules as
# estop.sh — never an empty field, tabs collapse under IFS).
LINE=$(python3 - "$CONFIG" "$ROBOT" "$ACTION" <<'PY'
import sys, yaml
config_path, robot, action = sys.argv[1], sys.argv[2], sys.argv[3]
with open(config_path) as fh:
    data = yaml.safe_load(fh) or {}
robots = data.get("robots") or {}
spec = robots.get(robot)
if spec is None:
    print(f"__ERROR__\tunknown robot {robot!r}; configured: {', '.join(sorted(robots))}")
    sys.exit(0)
actions = spec.get("actions") or {}
entry = actions.get(action)
if not entry or not entry.get("command"):
    have = ", ".join(sorted(actions)) or "none"
    print(f"__ERROR__\trobot {robot!r} has no action {action!r}; declared: {have}")
    sys.exit(0)
print("\t".join([
    str(spec.get("host", "local")) or "local",
    str(spec.get("container", "")) or "-",
    str(spec.get("shell", "zsh")) or "zsh",
    entry["command"].strip(),
]))
PY
)

case "$LINE" in
    __ERROR__*) echo "${LINE#__ERROR__	}" >&2; exit 2 ;;
esac

IFS=$'\t' read -r HOST CONTAINER SHELL_BIN COMMAND <<<"$LINE"

wrapped="$COMMAND"
[ "$CONTAINER" = "-" ] && CONTAINER=""
if [ -n "$CONTAINER" ]; then
    # zsh -lc, never bash: CYCLONEDDS_URI lives in /etc/zsh/zshenv (the
    # documented false-dead trap).
    # The inner `timeout` bounds the command inside the container, because the
    # outer one only kills the local docker or ssh client (see estop.sh).
    wrapped="docker exec $CONTAINER timeout -k 2 $INNER_TIMEOUT $SHELL_BIN -lc $(printf '%q' "$COMMAND")"
fi
if [ "$HOST" != "local" ] && [ "$(hostname)" != "$HOST" ]; then
    wrapped="ssh -o ConnectTimeout=5 -o BatchMode=yes -o ForwardX11=no $HOST $(printf '%q' "$wrapped")"
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
