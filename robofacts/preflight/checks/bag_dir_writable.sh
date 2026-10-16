#!/usr/bin/env bash
# The directory the bag will be written to exists and is actually writable.
#
# Exists because of the failure this misses everything else about: on the Lab PC
# the host user is uid 1001 while the container user is uid 1000, so the mounted
# workspace was read-only to the container. `ros2 bag record` started, printed
# "Failed to create database directory", exited, and the trial finished looking
# entirely normal -- a stack that came up, an action that ran, a recorder that
# stopped. The only evidence was an empty directory.
#
# Cheap to check, and it fails in the one second before the robot moves rather
# than after a flight that cannot be recovered.
#
#   bag_dir_writable.sh path=/workspaces/.../experiments/<exp>/bags [label=bags]
#
# A path that does not exist yet passes when its nearest existing parent is
# writable (the recorder creates it), and fails when that parent is not.
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="bag_dir_writable.sh path=<dir> [label=<human name>] [min_free_mb=0]"
pf_parse_args "$@"
pf_require_args path

TARGET="$(pf_arg path)"
LABEL="$(pf_arg label "$TARGET")"
MIN_FREE_MB="$(pf_arg min_free_mb 0)"

# A MISSING directory is not a NO-GO when it can be created (2026-10-13, the
# adoption test of the SAC 2027 framework paper): the recorder creates the bag
# directory and its parents (rosbag2 does, checked in the dev container), and
# the T-0 gate runs before it, so a bare "does not exist" refused the first
# trial of every new experiment. What matters is whether the recorder will be
# able to write there, so a missing directory is judged by its nearest
# existing parent, with the same write probe. An existing directory that is
# not writable still FAILS (the uid incident above, and E1's bag_dir_unwritable
# fault, which makes an existing bags/ read-only).
PROBE_DIR="$TARGET"
CREATABLE=""
if [ ! -d "$TARGET" ]; then
    [ -e "$TARGET" ] && pf_fail "$LABEL at $TARGET exists but is not a directory"
    PROBE_DIR="$(dirname -- "$TARGET")"
    while [ ! -d "$PROBE_DIR" ] && [ "$PROBE_DIR" != "/" ] && [ "$PROBE_DIR" != "." ]; do
        [ -e "$PROBE_DIR" ] && pf_fail "$LABEL cannot be created at $TARGET: $PROBE_DIR is not a directory"
        PROBE_DIR="$(dirname -- "$PROBE_DIR")"
    done
    [ -d "$PROBE_DIR" ] || pf_fail "$LABEL does not exist at $TARGET and no parent of it exists"
    CREATABLE=" (does not exist yet, the recorder creates it in $PROBE_DIR)"
fi

# -w lies under some mount and ACL combinations, so write something instead.
PROBE="$PROBE_DIR/.preflight_write_probe.$$"
if ! (touch "$PROBE" 2>/dev/null); then
    OWNER="$(stat -c '%U:%G (%u:%g)' "$PROBE_DIR" 2>/dev/null || echo unknown)"
    rm -f "$PROBE" 2>/dev/null
    if [ -n "$CREATABLE" ]; then
        pf_fail "$LABEL does not exist at $TARGET and cannot be created: its nearest \
existing parent $PROBE_DIR is NOT writable by $(id -un) ($(id -u):$(id -g)) - owned by $OWNER."
    fi
    pf_fail "$LABEL is NOT writable by $(id -un) ($(id -u):$(id -g)) - owned by $OWNER. \
Recording would start and silently produce an empty bag. If this is a dev container, \
its user id does not match the host's (VS Code's updateRemoteUserUID normally fixes \
this; a container started straight from docker compose does not get that)."
fi
rm -f "$PROBE" 2>/dev/null

if [ "$MIN_FREE_MB" != "0" ]; then
    FREE_MB="$(df -Pm "$PROBE_DIR" 2>/dev/null | awk 'NR==2{print $4}')"
    pf_ge "$FREE_MB" "$MIN_FREE_MB" || \
        pf_fail "$LABEL writable but only ${FREE_MB} MB free (need >= ${MIN_FREE_MB} MB)${CREATABLE}"
    pf_pass "$LABEL writable, ${FREE_MB} MB free${CREATABLE}"
fi

pf_pass "$LABEL writable by $(id -un)${CREATABLE}"
