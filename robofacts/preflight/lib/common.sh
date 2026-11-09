#!/usr/bin/env bash
# =============================================================================
# Shared helpers for the modular pre-flight checks
# =============================================================================
# Every check script sources this, parses key=value arguments, and exits with:
#
#   0  PASS  - verified good, right now
#   1  FAIL  - verified bad, or unverifiable (UNREADABLE IS A FAIL, never a skip)
#   3  SKIP  - genuinely not applicable to this experiment (e.g. no camera)
#
# Exit code 2 is reserved for usage errors (bad arguments), which are also a
# no-go: a check that cannot run has not passed.
#
# Design rules, all learned the hard way (2026-07-22 / 2026-07-23 incidents):
#   - Every check samples FRESH data at run time. A check that passed minutes
#     ago is worthless; T-0 means seconds before arming.
#   - "Node is up" is never evidence. Measure the data actually flowing.
#   - Unreadable == FAIL. Silence must never be mistaken for consent.
# =============================================================================

# --- exit codes ---------------------------------------------------------------
PF_PASS=0
PF_FAIL=1
PF_USAGE=2
PF_SKIP=3

# `ros2 topic echo` spends roughly this long establishing the subscription
# before the first sample arrives. Ignoring it made a healthy 100 Hz stream
# read exactly-at-threshold and NO-GO two good gates (2026-07-23).
PF_ECHO_CONNECT_S=1.2

# --- result helpers -----------------------------------------------------------
pf_pass() { echo "PASS: $*"; exit "$PF_PASS"; }
pf_fail() { echo "FAIL: $*"; exit "$PF_FAIL"; }
pf_skip() { echo "SKIP: $*"; exit "$PF_SKIP"; }
pf_usage_error() { echo "USAGE: $*" >&2; exit "$PF_USAGE"; }

# --- argument parsing ---------------------------------------------------------
# Checks are called as:  check.sh topic=/poses min_hz=85
# pf_parse_args "$@" populates PF_ARG_<key>; pf_arg <key> [default] reads it.
declare -A PF_ARGS=()

pf_parse_args() {
    local kv
    for kv in "$@"; do
        case "$kv" in
            --help|-h) pf_print_help; exit 0 ;;
            *=*) PF_ARGS["${kv%%=*}"]="${kv#*=}" ;;
            *) pf_usage_error "arguments must be key=value (got '$kv')" ;;
        esac
    done
}

# Abort unless every named argument was supplied.
#
# This MUST be called from the check's main shell, before the pf_arg reads.
# pf_arg is almost always used as CF="$(pf_arg cf)", i.e. inside a command
# substitution, so an `exit` from within it only kills that subshell and the
# check carries on with an empty value - it still fails, but with a misleading
# message and the wrong exit code.
pf_require_args() {
    local key
    local -a missing=()
    for key in "$@"; do
        [ -n "${PF_ARGS[$key]+x}" ] || missing+=("$key=")
    done
    [ ${#missing[@]} -gt 0 ] && pf_usage_error "missing required argument(s): ${missing[*]}"
    return 0
}

pf_arg() {
    local key="$1" default="${2-}"
    if [ -n "${PF_ARGS[$key]+x}" ]; then
        printf '%s' "${PF_ARGS[$key]}"
    elif [ $# -ge 2 ]; then
        printf '%s' "$default"
    else
        pf_usage_error "missing required argument '$key='"
    fi
}

# Checks override this to document themselves; run_preflight.sh --list uses it.
pf_print_help() { echo "${PF_HELP:-No help available for this check.}"; }

# --- environment --------------------------------------------------------------
pf_require_ros() {
    command -v ros2 >/dev/null 2>&1 \
        || pf_fail "ros2 is not on PATH - the workspace is not sourced in this shell"
}

# --- measurement helpers ------------------------------------------------------
# Count messages on a topic over a window, correcting for the subscription
# connect time. Echoes the measured Hz (may be 0).
#
# Counts the '---' separator ros2 topic echo prints after EVERY message, NOT
# header stamps: `--field header.stamp` yields nothing for headerless types
# (std_msgs/String — e.g. an estimator's ~/status), which made a
# topic_rate check read a permanent 0.0 Hz "stalled or dead" against a node
# that was demonstrably firing (2026-07-31, a shadow campaign's T-0
# gate). --no-arr keeps image/array payloads cheap to render.
#
#   pf_topic_hz <topic> <window_s>
pf_topic_hz() {
    local topic="$1" window="$2" lines effective
    lines="$(timeout "$window" ros2 topic echo --no-arr "$topic" 2>/dev/null \
        | grep -c '^---$')"
    effective="$(awk -v w="$window" -v c="$PF_ECHO_CONNECT_S" 'BEGIN{d=w-c; print (d>0)?d:0}')"
    awk -v n="$lines" -v d="$effective" 'BEGIN{ if (d<=0) print 0; else printf "%.1f", n/d }'
}

# True when the topic is advertised at all (cheap existence probe).
pf_topic_exists() {
    timeout 6 ros2 topic list 2>/dev/null | grep -qx "$1"
}

pf_service_exists() {
    timeout 6 ros2 service list 2>/dev/null | grep -q "$1"
}

# Which LIVE node advertises <service> as a service server? Echoes the first
# owning node name, or returns 1 when no live node offers it.
#
# `ros2 service list` is a NAME lookup across the whole DDS domain, so it is
# satisfied by any participant on the network - a foreign machine's stack, or a
# stale endpoint whose node is already gone. On 2026-08-18 the rig saw
# /all/emergency advertised while `ros2 node list` was EMPTY and the only topics
# were /parameter_events and /rosout, and the name-only e-stop gate PASSED
# against a kill path that did not exist. Resolving the owner turns a name match
# back into evidence.
pf_service_owner() {
    local svc="$1" node
    while IFS= read -r node; do
        [ -n "$node" ] || continue
        if timeout 6 ros2 node info "$node" 2>/dev/null \
            | awk '/^  Service Servers:/{f=1; next} /^  [A-Za-z]/{f=0} f {sub(/:.*/, "", $1); print $1}' \
            | grep -q -- "$svc"; then
            echo "$node"
            return 0
        fi
    done < <(timeout 6 ros2 node list 2>/dev/null)
    return 1
}

pf_node_exists() {
    timeout 6 ros2 node list 2>/dev/null | grep -q "$1"
}

# Exact variant: the whole `ros2 node list` line must equal <name> (e.g.
# /detector_node). The substring form above let a pose bridge node
# satisfy a check aimed at the detector while both detector components had
# failed to load (2026-08-12), so any check whose target has similarly-named
# neighbours must use this one.
pf_node_exists_exact() {
    timeout 6 ros2 node list 2>/dev/null | grep -qxF -- "$1"
}

# Numeric comparison without relying on bash arithmetic (values are floats).
#   pf_ge <a> <b>  -> true when a >= b
pf_ge() { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a>=b)}'; }

# --- process counting ---------------------------------------------------------
# Ourselves and everything we run inside of (the driver, the shell, the VS Code
# server). A process-counting check must never count these: the check script's
# own argv contains the very patterns it searches for, so a naive `pgrep -f`
# always finds at least itself and reports a phantom leftover.

# Parent PID of <pid>, read from /proc rather than by forking ps.
#
# Fewer forks matters here: every fork is itself a process that inherits our
# command line and can show up in the pgrep results below. /proc is also
# authoritative inside the container, where `ps -o ppid=` on the top-level shell
# reports 0 because its real parent lives outside the PID namespace.
#
# Returns non-zero when the process is gone.
pf_ppid() {
    local stat
    stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
    [ -z "$stat" ] && return 1
    # Fields: pid (comm) state ppid ... — comm may contain spaces and ')', so
    # strip everything through the LAST ') ' before splitting.
    stat="${stat##*) }"
    set -- $stat
    printf '%s' "${2:-}"
}

declare -A PF_ANCESTORS=()
pf_init_ancestors() {
    local probe=$$ guard=0
    while [ -n "$probe" ] && [ "$probe" -gt 1 ] 2>/dev/null && [ "$guard" -lt 64 ]; do
        PF_ANCESTORS["$probe"]=1
        probe="$(pf_ppid "$probe")" || break
        guard=$((guard + 1))
    done
}

# True when <pid> is us, one of our ancestors, or one of our descendants.
#
# Descendants matter as much as ancestors here: `n="$(pf_count_processes ...)"`
# forks a subshell that INHERITS this script's argv, so pgrep finds a second
# process whose command line contains the pattern. It is not an ancestor, so an
# ancestors-only filter still reports a phantom leftover.
#
# Siblings are deliberately NOT excluded: a stale node launched earlier from the
# same terminal is exactly what this is meant to catch.
pf_is_own_process() {
    local pid="$1" probe="$1" guard=0
    [ -n "${PF_ANCESTORS[$pid]+x}" ] && return 0
    while [ -n "$probe" ] && [ "$probe" -gt 1 ] 2>/dev/null && [ "$guard" -lt 64 ]; do
        [ "$probe" = "$$" ] && return 0
        probe="$(pf_ppid "$probe")" || return 1
        guard=$((guard + 1))
    done
    return 1
}

# Count processes whose command line contains <pattern>, excluding our own
# process tree. Always echoes an integer.
#
# The existence re-checks are not paranoia. pgrep reports a snapshot, and our
# own short-lived forks (command substitution, process substitution) inherit
# this script's command line, so they MATCH the pattern and can exit before we
# can walk their parent chain. Without the re-check a vanished fork is
# indistinguishable from a real leftover, which produced an intermittent false
# NO-GO - reproducible inside the dev container, invisible on the host.
# A process that no longer exists cannot be holding the camera or a UDP port.
#   pf_count_processes <pattern>
pf_count_processes() {
    local pattern="$1" pid count=0
    [ ${#PF_ANCESTORS[@]} -eq 0 ] && pf_init_ancestors
    while read -r pid; do
        [ -z "$pid" ] && continue
        [ -d "/proc/$pid" ] || continue          # already gone when we looked
        pf_is_own_process "$pid" && continue
        [ -d "/proc/$pid" ] || continue          # exited while we walked its chain
        count=$((count + 1))
    done < <(pgrep -f -- "$pattern" 2>/dev/null)
    printf '%d' "$count"
}
