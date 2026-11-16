#!/usr/bin/env bash
# =============================================================================
# Stop every configured robot, from the laptop, in one word
# =============================================================================
# Replaces having to remember, correctly, under pressure:
#
#   ssh <rig-host> \
#     'docker exec ros2_jetson_dev zsh -lc "ros2 service call /all/emergency std_srvs/srv/Empty"'
#
# Robots and their stop commands live in experiments/common/config/robots.yaml,
# so adding one is a config edit, and the future web UI drives the same file -
# its buttons and this script cannot drift apart.
#
#   ./scripts/estop.sh              # E-STOP every robot, in parallel
#   ./scripts/estop.sh --soft       # land / halt gracefully instead
#   ./scripts/estop.sh --robot robot1
#   ./scripts/estop.sh --host-level laptop   # the stack runs on this level, for this call
#   ./scripts/estop.sh --list       # show what is configured, do nothing
#   ./scripts/estop.sh --dry-run    # print the exact commands, run nothing
#
# WHERE THE STOP GOES (2026-11-16): --host-level, else the running HARDWARE
# trial of this machine (its run marker names its execution_host), else the
# robot's host_level in robots.yaml, resolved through hosts.yaml next to it.
# --list prints the host and the reason for each robot.
#
# THE DEFAULT IS THE EMERGENCY. Typing one word must stop everything, because
# that is the moment you have. Inspection is what needs a flag, not the stop.
#
# THIS IS NOT THE PRIMARY KILL. It travels laptop -> ssh -> container -> DDS,
# and every hop can fail: on 2026-07-22 the e-stop terminal had no DDS discovery
# while everything else looked healthy. The physical kill switch stays primary,
# and the estop_reachable check must pass before every flight.
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
# The directory that holds the robofacts package, for the host resolver. readlink -f, because
# scripts/estop.sh is usually a symlink to this file.
PKG_PARENT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../.." && pwd)"
HOST_LEVEL=""
ACTION="estop"
ROBOT=""
DRY_RUN=0
LIST=0
TIMEOUT=15

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --soft) ACTION="soft_stop"; shift ;;
        --estop) ACTION="estop"; shift ;;
        --robot) ROBOT="$2"; shift 2 ;;
        --host-level) HOST_LEVEL="$2"; shift 2 ;;
        --config) CONFIG="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --list) LIST=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -f "$CONFIG" ] || { echo "Robot config not found: $CONFIG" >&2; exit 2; }

# Emit one TAB-separated line per robot:
# name, host, container, shell, verified, command, description, why this host
read_config() {
    python3 - "$CONFIG" "$ACTION" "$ROBOT" "$HOST_LEVEL" "$PKG_PARENT" <<'PY'
import sys, yaml
config_path, action, only, level, pkg = sys.argv[1:6]
with open(config_path) as fh:
    data = yaml.safe_load(fh) or {}
robots = data.get("robots") or {}
if only:
    if only not in robots:
        print(f"__ERROR__\tunknown robot {only!r}; configured: {', '.join(sorted(robots))}")
        sys.exit(0)
    robots = {only: robots[only]}
try:
    sys.path.insert(0, pkg)
    from robofacts import host_registry as hr, run_marker
    root = hr.experiments_root_of(config_path)
    marker = run_marker.read_live(root) if root else None
    targets = hr.robot_targets(config_path, robots, level=level or None, marker=marker)
except Exception as exc:  # an unresolvable target must stop the script, loudly
    print(f"__ERROR__\tcannot resolve where the robots run ({type(exc).__name__}: {exc})")
    sys.exit(0)
for name, spec in robots.items():
    entry = (spec or {}).get(action)
    if not entry or not entry.get("command"):
        print(f"__SKIP__\t{name}\tno {action} configured")
        continue
    # Never emit an EMPTY field: bash `read` with IFS=tab collapses consecutive
    # tabs (tab is whitespace), so a robot with no container would silently
    # shift every later field left - which produced `docker exec zsh ...`.
    target = targets[name]
    print("\t".join([
        name,
        target.host or "local",
        target.container or "-",
        target.shell or "zsh",
        "yes" if spec.get("verified") else "NO",
        entry["command"].strip(),
        str(entry.get("description", "")).strip() or "-",
        target.source.replace("\t", " ") or "-",
    ]))
PY
}

# Build the full command for one robot, honouring host + container.
#
# RIG_HOST overrides the rig's ssh alias (the resolver applies it, host_registry
# robot_targets: the registry's rig level, or every non-local host of a robot
# without host_level), the same knob rig_tmux.sh grew in 36925616ac. The rig moves between networks (rig router
# on-site, a VPN when remote, the old lab LAN as legacy) and each has its
# own ssh alias, so the config cannot name one alias that is always right.
# Retarget the whole e-stop with one variable instead of editing this file
# under pressure:
#
#   RIG_HOST=<rig-host-vpn> ./scripts/estop.sh
#
# RIG_CONTAINER overrides the container the same way (2026-09-23). Needed when
# the stack runs on the LAPTOP (bench tests, campaigns with execution_host
# laptop): the config names the rig's ros2_jetson_dev, which does not exist
# here, so without this the stop would ssh to the rig and reach a server that
# is not flying the drone. With the laptop's container named, the local docker
# inspect below succeeds and the stop goes straight into it:
#
#   RIG_CONTAINER=ros2_humble_dev ./scripts/estop.sh --robot robot1
#
build_command() {
    local host="$1" container="$2" shell="$3" inner="$4"
    local wrapped="$inner"
    [ -n "${RIG_CONTAINER:-}" ] && [ "$container" != "-" ] && container="$RIG_CONTAINER"
    [ "$container" = "-" ] && container=""

    # WHERE ARE WE? The config names the host as seen FROM THE LAPTOP, but the
    # e-stop gets typed wherever the operator happens to be standing, and on
    # 2026-08-14 that was a tmux pane ON THE RIG: `ssh orin-rig` does not
    # resolve there (the alias lives in the laptop's ssh config, and the rig
    # router has no DNS), so the e-stop FAILED during a real flyaway. Detect the
    # two on-rig cases and skip the hops that would fail — fewer hops is also
    # strictly better for a stop signal:
    #
    #   1. Already INSIDE a container (/.dockerenv exists): run the command
    #      directly. There is no docker CLI in here and no ssh alias either;
    #      the DDS environment is exactly the one the rig uses.
    #   2. On a host where THIS ROBOT'S container is running (docker inspect
    #      succeeds): we ARE the rig host, so docker exec locally and skip ssh.
    #
    # Neither can false-positive on the laptop: its devcontainer is
    # ros2_humble_dev, not ros2_jetson_dev, so inspect fails and the ssh path
    # is taken as before. RIG_HOST keeps overriding the remote alias for the
    # laptop-side cases (rig router vs VPN vs old lab LAN).
    if [ -n "$container" ] && [ -f /.dockerenv ]; then
        printf '%s' "$inner"
        return
    fi
    if [ -n "$container" ]; then
        # `zsh -lc` is required, not bash: CYCLONEDDS_URI is defaulted only in
        # /etc/zsh/zshenv, and a bash login shell silently loses DDS discovery,
        # so the call would reach nothing while appearing to succeed.
        #
        # The inner `timeout` bounds the command inside the container. The outer
        # `timeout` only kills the local docker or ssh client, and the process
        # that `docker exec` started keeps running in the container after its
        # client dies, so a hung stop would hold DDS state until the container
        # restarts. The inner limit ends 2 s before the outer one, so a hung stop
        # still exits with 124 and the FAIL line keeps its explanation. `-k 2`
        # sends SIGKILL to a command that ignores SIGTERM.
        wrapped="docker exec $container timeout -k 2 $INNER_TIMEOUT $shell -lc $(printf '%q' "$inner")"
        if docker container inspect "$container" >/dev/null 2>&1; then
            printf '%s' "$wrapped"
            return
        fi
    fi
    if [ "$host" != "local" ]; then
        # ForwardX11=no: an ssh config with X11 forwarding prints xauth warnings
        # into the captured output, burying the real error under pressure.
        wrapped="ssh -o ConnectTimeout=5 -o BatchMode=yes -o ForwardX11=no $host $(printf '%q' "$wrapped")"
    fi
    printf '%s' "$wrapped"
}

if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || [ "$TIMEOUT" -lt 1 ]; then
    echo "--timeout must be a whole number of seconds, at least 1 (got '$TIMEOUT')" >&2
    exit 2
fi
INNER_TIMEOUT=$(( TIMEOUT > 3 ? TIMEOUT - 2 : 1 ))

MAPFILE=$(read_config)
if [ -z "$MAPFILE" ]; then
    echo "No robots configured in $CONFIG" >&2
    exit 2
fi
if grep -q '^__ERROR__' <<<"$MAPFILE"; then
    echo "$(grep '^__ERROR__' <<<"$MAPFILE" | cut -f2)" >&2
    exit 2
fi

if [ "$LIST" -eq 1 ]; then
    echo "Configured robots ($CONFIG):"
    while IFS=$'\t' read -r name host container shell verified command desc source; do
        [ "$name" = "__SKIP__" ] && { echo "  - $host: $container"; continue; }
        # The host that will actually be dialled and why, so --list cannot reassure
        # you about an alias that RIG_HOST or a running trial has replaced.
        echo "  $name  [$ACTION]  host=$host container=$([ "$container" = "-" ] && echo none || echo "$container") verified=$verified"
        echo "      target: $source"
        echo "      $desc"
        echo "      $command"
    done <<<"$MAPFILE"
    exit 0
fi

echo "=== ${ACTION^^} ==="
FAILED=0
PIDS=()
NAMES=()
TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT

while IFS=$'\t' read -r name host container shell verified command desc source; do
    if [ "$name" = "__SKIP__" ]; then
        echo "  SKIP  $host ($container)"
        continue
    fi
    full="$(build_command "$host" "$container" "$shell" "$command")"
    if [ "$verified" = "NO" ]; then
        echo "  ⚠ $name: this entry is NOT verified against the real robot (robots.yaml)"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  [$name] $full"
        continue
    fi
    # Run robots in PARALLEL: one hanging ssh must not delay stopping the others.
    ( timeout "$TIMEOUT" bash -c "$full" >"$TMPDIR_RUN/$name.out" 2>&1; echo $? >"$TMPDIR_RUN/$name.rc" ) &
    PIDS+=("$!")
    NAMES+=("$name")
    echo "  ... $name ($desc) -> $host$([ "$container" = "-" ] || echo "/$container"), $source"
done <<<"$MAPFILE"

[ "$DRY_RUN" -eq 1 ] && { echo ""; echo "--dry-run: nothing sent."; exit 0; }

for i in "${!PIDS[@]}"; do
    wait "${PIDS[$i]}"
done

for name in "${NAMES[@]}"; do
    rc="$(cat "$TMPDIR_RUN/$name.rc" 2>/dev/null || echo 1)"
    if [ "$rc" = "0" ]; then
        echo "  OK    $name"
    else
        FAILED=$((FAILED + 1))
        # rc=124 is `timeout` killing the command. When the tell-tale is a
        # service that never appeared, the robot's driver is not running —
        # almost always because the robot is switched off. Say so, because
        # "FAIL" alone reads as "the stop was refused" and sends an operator
        # hunting for a fault on a robot that was never powered. It still
        # counts as NOT CONFIRMED: this cannot prove the robot is safe, only
        # that nothing answered.
        if [ "$rc" = "124" ] && grep -q "waiting for service" "$TMPDIR_RUN/$name.out" 2>/dev/null; then
            echo "  FAIL  $name (rc=124: no service — driver not running, robot likely powered OFF)"
        else
            echo "  FAIL  $name (rc=$rc)"
        fi
        sed 's/^/          /' "$TMPDIR_RUN/$name.out" 2>/dev/null | tail -4
    fi
done

echo "==="
if [ "$FAILED" -gt 0 ]; then
    echo "$FAILED robot(s) DID NOT CONFIRM THE STOP — use the physical kill switch." >&2
    exit 1
fi
echo "All robots confirmed the ${ACTION}."
