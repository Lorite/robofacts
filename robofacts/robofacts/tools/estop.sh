#!/usr/bin/env bash
# =============================================================================
# Stop every configured robot, from the laptop, in one word
# =============================================================================
# Replaces having to remember, correctly, under pressure:
#
#   ssh <rig-host> \
#     'docker exec <container> zsh -lc "<the robot's stop command>"'
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
# THIS IS NOT THE PRIMARY KILL. It travels laptop -> ssh -> container -> the robot's link,
# and every hop can fail: on 2026-07-22 the e-stop terminal had no discovery of the robot's network
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

# Write one record per robot to stdout: ten fields, each ended by a NUL byte (2026-11-25).
# The fields: kind (ROBOT, SKIP, BAD or ERROR), name, host, container, shell, verified, command,
# description, why this host, and the hostname inside the target's container ("-" when unknown).
# A SKIP or BAD record carries its reason in the third field, an ERROR record its message in the
# second. Before 2026-11-25 the records were TAB-separated lines: a multi-line command ran only
# its first line, its second line became a robot of its own, and a stop written as a plain
# string (the form the skeleton robots.yaml documents) crashed the reader, so the robots after it
# got no stop while the script reported success.
read_config() {
    python3 - "$CONFIG" "$ACTION" "$ROBOT" "$HOST_LEVEL" "$PKG_PARENT" <<'PY'
import sys, yaml
config_path, action, only, level, pkg = sys.argv[1:6]
out = sys.stdout.buffer


def emit(kind, *fields):
    record = [kind, *(str(f) for f in fields)]
    record += ["-"] * (10 - len(record))
    for field in record:
        out.write(field.replace("\0", " ").encode("utf-8", "replace") + b"\0")
    out.flush()


def stop_entry(entry):
    """The command and the description of a stop entry, in either documented form."""
    if isinstance(entry, str):
        command, description = entry, ""
    elif isinstance(entry, dict):
        command, description = entry.get("command"), entry.get("description")
    else:
        raise ValueError(f"expected a command string or a mapping with 'command', "
                         f"got {type(entry).__name__}")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("it has no command")
    return command.strip(), str(description or "").strip() or "-"


try:
    with open(config_path) as fh:
        data = yaml.safe_load(fh) or {}
    robots = data.get("robots") or {}
    if not isinstance(robots, dict):
        raise ValueError(f"'robots' must map each robot to its settings, got {type(robots).__name__}")
except Exception as exc:  # a config that cannot be read must stop the script, loudly
    emit("ERROR", f"cannot read {config_path} ({type(exc).__name__}: {exc})")
    sys.exit(0)
if only:
    if only not in robots:
        emit("ERROR", f"unknown robot {only!r}; configured: {', '.join(sorted(map(str, robots)))}")
        sys.exit(0)
    robots = {only: robots[only]}
try:
    sys.path.insert(0, pkg)
    from robofacts import host_registry as hr, run_marker
    root = hr.experiments_root_of(config_path)
    marker = run_marker.read_live(root) if root else None
    targets = hr.robot_targets(config_path, robots, level=level or None, marker=marker)
except Exception as exc:  # an unresolvable target must stop the script, loudly
    emit("ERROR", f"cannot resolve where the robots run ({type(exc).__name__}: {exc})")
    sys.exit(0)
for name, spec in robots.items():
    name = str(name)
    try:
        if spec is None or (isinstance(spec, dict) and spec.get(action) is None):
            emit("SKIP", name, f"no {action} configured")
            continue
        if not isinstance(spec, dict):
            raise ValueError(f"the robot entry must be a mapping, got {type(spec).__name__}")
        command, description = stop_entry(spec[action])
        target = targets[name]
        emit("ROBOT", name, target.host or "local", target.container or "-", target.shell or "zsh",
             "yes" if spec.get("verified") else "NO", command, description,
             target.source or "-", target.container_hostname or "-")
    except Exception as exc:  # this robot gets no stop, and the script reports a failure
        emit("BAD", name, f"its {action} entry cannot be used ({type(exc).__name__}: {exc})")
PY
}

# Read one record of read_config into the array REC. Returns 1 at the end of the records, and
# then REC holds the fields of a record that was cut short (none at a clean end).
read_record() {
    local field
    REC=()
    while [ "${#REC[@]}" -lt 10 ]; do
        IFS= read -r -d '' field || return 1
        REC+=("$field")
    done
}

# Quote one argument for the next shell. printf %q writes a line break as $'\n', which a POSIX sh
# does not read, so a multi-line command is single-quoted instead, which every shell reads. A
# one-line command keeps the printf %q form.
quote() {
    local q
    q="$(printf '%q' "$1")"
    case "$q" in
        *"\$'"*) printf "'%s'" "${1//\'/\'\\\'\'}" ;;
        *) printf '%s' "$q" ;;
    esac
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
# Where the stop runs, "here" detection (2026-11-17): TRIALS_DOCKERENV only lets the tests
# point the container check at a file of their own.
DOCKERENV="${TRIALS_DOCKERENV:-/.dockerenv}"
build_command() {
    local host="$1" container="$2" shell="$3" inner="$4" chost="${5:--}"
    local wrapped="$inner"
    [ -n "${RIG_CONTAINER:-}" ] && [ "$container" != "-" ] && container="$RIG_CONTAINER"
    [ "$container" = "-" ] && container=""

    # WHERE ARE WE? The config names the host as seen FROM THE LAPTOP, but the
    # e-stop gets typed wherever the operator happens to be standing, and on
    # 2026-08-14 that was a tmux pane ON THE RIG: `ssh <robot host>` does not
    # resolve there (the alias lives in the laptop's ssh config, and the rig
    # router has no DNS), so the e-stop FAILED during a real flyaway. Detect the
    # two on-rig cases and skip the hops that would fail — fewer hops is also
    # strictly better for a stop signal:
    #
    #   1. Already INSIDE THE TARGET'S container (/.dockerenv exists and the
    #      hostname is the target's container_hostname, or the target names no
    #      container_hostname): run the command directly. There is no docker
    #      CLI in here; the network environment is exactly the one the rig uses.
    #      Before 2026-11-17 ANY container counted, so a stop typed in the
    #      laptop's container, or the web interface's stop
    #      button served from it, ran the Orin's stop on the laptop and relied on
    #      Wi-Fi discovery, which the robot computer's unicast-only config does not offer. Inside
    #      another container the ssh path below is taken (the container has the
    #      host's ssh config, the runner uses the same path).
    #   2. On a host where THIS ROBOT'S container is running (docker inspect
    #      succeeds, and the container's hostname is the target's
    #      container_hostname, or the target names no container_hostname): we
    #      ARE the rig host, so docker exec locally and skip ssh.
    #
    # Case 2 used to trust the container's name alone. The documented examples
    # give the robot's container the same name as a common local dev container,
    # so on a laptop with that container a stop for the robot computer went
    # into the laptop's own container (2026-11-25). Now a container of that
    # name whose hostname differs from the declared container_hostname is
    # another machine's twin, and the stop goes over ssh to the declared host.
    # RIG_CONTAINER names a container on purpose, so it skips that check. A
    # target without container_hostname keeps the old rule, so declare it.
    # RIG_HOST keeps overriding the remote alias for the
    # laptop-side cases (rig router vs VPN vs old lab LAN).
    #
    # Case 1 still runs the command in the declared login shell, bounded by the inner limit, as
    # robot_action.sh does. Before 2026-11-25 it ran in the `bash -c` of the caller below, so in
    # a container that sets its middleware configuration only in /etc/zsh/zshenv (see the zsh
    # note below) the stop ran without discovery: it reached nothing and could still exit 0.
    if [ -n "$container" ] && [ -f "$DOCKERENV" ] \
        && { [ "$chost" = "-" ] || [ "$(hostname)" = "$chost" ]; }; then
        printf '%s' "timeout -k 2 $INNER_TIMEOUT $shell -lc $(quote "$inner")"
        return
    fi
    if [ -n "$container" ]; then
        # `zsh -lc` is required, not bash: a container may default its middleware's
        # configuration only in /etc/zsh/zshenv, and a bash login shell then silently loses discovery,
        # so the call would reach nothing while appearing to succeed.
        #
        # The inner `timeout` bounds the command inside the container. The outer
        # `timeout` only kills the local docker or ssh client, and the process
        # that `docker exec` started keeps running in the container after its
        # client dies, so a hung stop would hold middleware state until the container
        # restarts. The inner limit ends 2 s before the outer one, so a hung stop
        # still exits with 124 and the FAIL line keeps its explanation. `-k 2`
        # sends SIGKILL to a command that ignores SIGTERM.
        wrapped="docker exec $container timeout -k 2 $INNER_TIMEOUT $shell -lc $(quote "$inner")"
        if docker container inspect "$container" >/dev/null 2>&1 \
            && { [ "$chost" = "-" ] || [ -n "${RIG_CONTAINER:-}" ] \
                 || [ "$(docker container inspect -f '{{.Config.Hostname}}' "$container" 2>/dev/null)" = "$chost" ]; }; then
            printf '%s' "$wrapped"
            return
        fi
    fi
    if [ "$host" != "local" ]; then
        # ForwardX11=no: an ssh config with X11 forwarding prints xauth warnings
        # into the captured output, burying the real error under pressure.
        wrapped="ssh -o ConnectTimeout=5 -o BatchMode=yes -o ForwardX11=no $host $(quote "$wrapped")"
    fi
    printf '%s' "$wrapped"
}

if ! [[ "$TIMEOUT" =~ ^[0-9]+$ ]] || [ "$TIMEOUT" -lt 1 ]; then
    echo "--timeout must be a whole number of seconds, at least 1 (got '$TIMEOUT')" >&2
    exit 2
fi
INNER_TIMEOUT=$(( TIMEOUT > 3 ? TIMEOUT - 2 : 1 ))

TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT

read_config >"$TMPDIR_RUN/records"
READER_RC=$?
KIND=(); NAME=(); HOST=(); CONTAINER=(); SHELL_IN=(); VERIFIED=(); COMMAND=(); DESC=(); SOURCE=(); CHOST=()
while read_record; do
    KIND+=("${REC[0]}"); NAME+=("${REC[1]}"); HOST+=("${REC[2]}"); CONTAINER+=("${REC[3]}")
    SHELL_IN+=("${REC[4]}"); VERIFIED+=("${REC[5]}"); COMMAND+=("${REC[6]}"); DESC+=("${REC[7]}")
    SOURCE+=("${REC[8]}"); CHOST+=("${REC[9]}")
done <"$TMPDIR_RUN/records"
# A record cut short means that the reader died while it wrote it.
if [ "${#REC[@]}" -gt 0 ] && [ "$READER_RC" -eq 0 ]; then READER_RC=1; fi

ERRORS=0
for i in "${!KIND[@]}"; do
    if [ "${KIND[$i]}" = "ERROR" ]; then echo "${NAME[$i]}" >&2; ERRORS=1; fi
done
[ "$ERRORS" -eq 1 ] && exit 2
if [ "${#KIND[@]}" -eq 0 ]; then
    if [ "$READER_RC" -ne 0 ]; then
        echo "Could not read the robots in $CONFIG (the reader exited with $READER_RC)" >&2
    else
        echo "No robots configured in $CONFIG" >&2
    fi
    exit 2
fi
# The reader died after some robots: those still get their stop, and the script reports a
# failure, because the robots after them got none.
READER_FAILED=0
if [ "$READER_RC" -ne 0 ]; then
    READER_FAILED=1
    echo "The robot config reader failed (exit $READER_RC) after ${#KIND[@]} robot(s): any robot after them gets NO stop." >&2
fi
# Robots whose stop entry cannot be used (2026-11-25): they get no stop, and each one counts
# as a failed stop, in --list, in --dry-run and in the real stop.
BAD=()
for i in "${!KIND[@]}"; do
    [ "${KIND[$i]}" = "BAD" ] && BAD+=("$i")
done

if [ "$LIST" -eq 1 ]; then
    echo "Configured robots ($CONFIG):"
    for i in "${!KIND[@]}"; do
        name="${NAME[$i]}" host="${HOST[$i]}" container="${CONTAINER[$i]}"
        case "${KIND[$i]}" in
            SKIP) echo "  - $name: $host"; continue ;;
            BAD) echo "  $name  [$ACTION]  NO STOP: $host"; continue ;;
        esac
        # The route from HERE, from the same function that builds the real stop.
        probe="$(build_command "$host" "$container" "${SHELL_IN[$i]}" "PROBE" "${CHOST[$i]}")"
        case "$probe" in
            PROBE) route="runs directly here, in this container" ;;
            timeout\ *) route="runs directly here, in this container (${SHELL_IN[$i]} -lc)" ;;
            docker\ exec*) route="docker exec $container on this machine" ;;
            ssh*) route="ssh $host, then docker exec $container" ;;
            *) route="$probe" ;;
        esac
        # The host that will actually be dialled and why, so --list cannot reassure
        # you about an alias that RIG_HOST or a running trial has replaced.
        echo "  $name  [$ACTION]  host=$host container=$([ "$container" = "-" ] && echo none || echo "$container") verified=${VERIFIED[$i]}"
        echo "      target: ${SOURCE[$i]}"
        echo "      route:  $route"
        echo "      ${DESC[$i]}"
        printf '%s\n' "${COMMAND[$i]}" | sed 's/^/      /'
    done
    [ "${#BAD[@]}" -gt 0 ] || [ "$READER_FAILED" -eq 1 ] && exit 1
    exit 0
fi

echo "=== ${ACTION^^} ==="
FAILED=0
PIDS=()
SENT=()

for i in "${!KIND[@]}"; do
    name="${NAME[$i]}" host="${HOST[$i]}" container="${CONTAINER[$i]}"
    case "${KIND[$i]}" in
        SKIP) echo "  SKIP  $name ($host)"; continue ;;
        BAD) echo "  !!    $name: $host, NO STOP SENT"; continue ;;
    esac
    full="$(build_command "$host" "$container" "${SHELL_IN[$i]}" "${COMMAND[$i]}" "${CHOST[$i]}")"
    if [ "${VERIFIED[$i]}" = "NO" ]; then
        echo "  ⚠ $name: this entry is NOT verified against the real robot (robots.yaml)"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  [$name] $full"
        continue
    fi
    # Run robots in PARALLEL: one hanging ssh must not delay stopping the others. The files
    # are named by the record's index, because a robot's name may hold any character.
    ( timeout "$TIMEOUT" bash -c "$full" >"$TMPDIR_RUN/$i.out" 2>&1; echo $? >"$TMPDIR_RUN/$i.rc" ) &
    PIDS+=("$!")
    SENT+=("$i")
    echo "  ... $name (${DESC[$i]}) -> $host$([ "$container" = "-" ] || echo "/$container"), ${SOURCE[$i]}"
done

if [ "$DRY_RUN" -eq 1 ]; then
    echo ""
    echo "--dry-run: nothing sent."
    if [ "${#BAD[@]}" -gt 0 ] || [ "$READER_FAILED" -eq 1 ]; then
        echo "$((${#BAD[@]} + READER_FAILED)) problem(s) above: the real stop would NOT reach every robot." >&2
        exit 1
    fi
    exit 0
fi

for pid in "${PIDS[@]}"; do
    wait "$pid"
done

for i in "${SENT[@]}"; do
    name="${NAME[$i]}"
    rc="$(cat "$TMPDIR_RUN/$i.rc" 2>/dev/null || echo 1)"
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
        if [ "$rc" = "124" ] && grep -q "waiting for service" "$TMPDIR_RUN/$i.out" 2>/dev/null; then
            echo "  FAIL  $name (rc=124: no service — driver not running, robot likely powered OFF)"
        else
            echo "  FAIL  $name (rc=$rc)"
        fi
        sed 's/^/          /' "$TMPDIR_RUN/$i.out" 2>/dev/null | tail -4
    fi
done
for i in "${BAD[@]}"; do
    FAILED=$((FAILED + 1))
    echo "  FAIL  ${NAME[$i]} (no stop sent: ${HOST[$i]})"
done
if [ "$READER_FAILED" -eq 1 ]; then
    FAILED=$((FAILED + 1))
    echo "  FAIL  the robot config reader (exit $READER_RC): a robot after the ones above got no stop"
fi

echo "==="
if [ "$FAILED" -gt 0 ]; then
    echo "$FAILED robot(s) DID NOT CONFIRM THE STOP — use the physical kill switch." >&2
    exit 1
fi
echo "All robots confirmed the ${ACTION}."
