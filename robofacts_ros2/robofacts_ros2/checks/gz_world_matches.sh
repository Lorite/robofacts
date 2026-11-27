#!/usr/bin/env bash
# Exactly one Gazebo world is running, and it is the world that the trial declares.
#
# Why this exists (2026-10-01). The PX4 simulation script
# px4-rc.gzsim looks for /world/*/clock and, when a world is already running,
# prints "gazebo already running world: <name>" and ATTACHES to it, whatever
# PX4_GZ_WORLD asked for. A Gazebo server left over from a previous trial
# (PX4 starts it with `&`, so it survives PX4) therefore makes the next trial
# fly in the wrong world while its metadata records the requested one. The
# ros_clean_slate check cannot see this: one gz server is exactly what it
# allows. This check asks the Gazebo transport which worlds advertise a clock.
#
#   gz_world_matches.sh world=${world} [gz_ip=127.0.0.1] [timeout_s=5]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="gz_world_matches.sh world=<name> [gz_ip=127.0.0.1] [timeout_s=5]"
pf_parse_args "$@"
pf_require_args world

WORLD="$(pf_arg world)"
GZ_IP_ARG="$(pf_arg gz_ip 127.0.0.1)"
TIMEOUT="$(pf_arg timeout_s 5)"

command -v gz >/dev/null 2>&1 || pf_fail "the gz CLI is not on PATH, so the running worlds cannot be read"

TOPICS="$(GZ_IP="$GZ_IP_ARG" timeout "$TIMEOUT" gz topic -l 2>/dev/null)"
WORLDS="$(printf '%s\n' "$TOPICS" | sed -n 's#^/world/\([^/]*\)/clock$#\1#p' | sort -u | tr '\n' ' ' | sed 's/ $//')"

[ -n "$WORLDS" ] || pf_fail "no Gazebo world advertises a clock (gz transport on $GZ_IP_ARG)"
[ "$WORLDS" = "$WORLD" ] && pf_pass "exactly one Gazebo world is running, '$WORLD'"
pf_fail "Gazebo worlds running: $WORLDS (the trial claims only '$WORLD') - a simulation start script may attach to an already running world, so a leftover server makes the trial run in the wrong one"
