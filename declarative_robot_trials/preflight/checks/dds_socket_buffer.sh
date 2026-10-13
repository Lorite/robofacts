#!/usr/bin/env bash
# The kernel can actually give CycloneDDS the receive buffer its config DEMANDS.
#
# Why this exists (2026-08-19, the last lab day before Spain). The rig's
# `cyclonedds_rig_lan.xml` carries `<SocketReceiveBufferSize min="16MB"/>`.
# Cyclone treats that as a HARD requirement: if the kernel cannot satisfy it,
# `rmw_create_node` fails and EVERY node dies at construction:
#
#   failed to increase socket receive buffer size to at least 16777216 bytes,
#   current is 425984 bytes
#   [rmw_cyclonedds_cpp]: rmw_create_node: failed to create domain, error Error
#   RCLError: error creating node: rcl node's rmw handle is invalid
#
# `net.core.rmem_max` is NOT persistent across reboots unless someone wrote it
# into /etc/sysctl.d/. The rig computer rebooted that morning, the value reverted to the
# 212992 default, and the whole stack died. The T-0 gate then reported SEVEN
# failures - absent nodes, dead topics, a 0-frame mocap stream - none of which
# was the cause, and the session spent an hour of a non-repeatable lab day
# chasing symptoms.
#
# That is exactly the failure shape this framework exists to prevent: a gate
# that reports consequences instead of causes. One line here turns it into a
# 5-second NO-GO carrying the fix.
#
#   dds_socket_buffer.sh [config=<path>] [min_bytes=<n>]
#
# With no arguments it reads the ACTIVE config from $CYCLONEDDS_URI and the
# requirement from that file, so it always speaks for the DDS the stack will
# actually use rather than a value hardcoded here.
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="dds_socket_buffer.sh [config=<path>] [min_bytes=<n>]  # kernel rmem_max vs the active CycloneDDS SocketReceiveBufferSize"
pf_parse_args "$@"

RMEM_PATH=/proc/sys/net/core/rmem_max
CONFIG="$(pf_arg config "")"
MIN_BYTES="$(pf_arg min_bytes "")"

# --- resolve the active config ------------------------------------------------
# Unreadable is a FAIL, never a skip: on this rig an unset CYCLONEDDS_URI is
# itself the documented FALSE-DEAD TRAP (bash does not source /etc/zsh/zshenv),
# so a check that cannot see the config cannot speak for the stack either.
if [ -z "$CONFIG" ]; then
    [ -n "${CYCLONEDDS_URI:-}" ] \
        || pf_fail "CYCLONEDDS_URI is unset in this shell, so the active DDS config is unknown - run the gate under zsh -lc, or pass config=<path>"
    CONFIG="${CYCLONEDDS_URI#file://}"
fi
[ -f "$CONFIG" ] \
    || pf_fail "CycloneDDS config '$CONFIG' does not exist (from \$CYCLONEDDS_URI='${CYCLONEDDS_URI:-}')"

# --- what does the config demand? ---------------------------------------------
# Cyclone RENAMED this element: MinimumSocketReceiveBufferSize is still parsed
# but IGNORED, the live one is SocketReceiveBufferSize[@min]. A config carrying
# only the old name silently requests nothing, which is its own defect worth
# naming rather than passing over in silence.
pf_to_bytes() {
    local raw="${1//[[:space:]]/}" num unit
    num="$(printf '%s' "$raw" | tr -dc '0-9')"
    unit="$(printf '%s' "$raw" | tr -dc 'A-Za-z' | tr '[:upper:]' '[:lower:]')"
    [ -n "$num" ] || return 1
    case "$unit" in
        ""|b)   printf '%s' "$num" ;;
        kb|k)   printf '%s' "$((num * 1024))" ;;
        mb|m)   printf '%s' "$((num * 1024 * 1024))" ;;
        gb|g)   printf '%s' "$((num * 1024 * 1024 * 1024))" ;;
        *) return 1 ;;
    esac
}

if [ -z "$MIN_BYTES" ]; then
    RAW="$(grep -oE '<SocketReceiveBufferSize[^>]*min="[^"]+"' "$CONFIG" 2>/dev/null \
           | grep -oE 'min="[^"]+"' | head -1 | sed 's/min="//; s/"//')"
    if [ -z "$RAW" ]; then
        if grep -qE '<MinimumSocketReceiveBufferSize' "$CONFIG" 2>/dev/null; then
            pf_fail "'$(basename "$CONFIG")' uses <MinimumSocketReceiveBufferSize>, which Cyclone parses but IGNORES - rename it to <SocketReceiveBufferSize min=\"...\"/> or the request silently does nothing"
        fi
        pf_pass "$(basename "$CONFIG") requests no SocketReceiveBufferSize minimum - Cyclone will not hard-fail on the kernel buffer"
    fi
    MIN_BYTES="$(pf_to_bytes "$RAW")" \
        || pf_fail "could not parse SocketReceiveBufferSize min='$RAW' in '$CONFIG'"
fi

# --- what can the kernel give? ------------------------------------------------
[ -r "$RMEM_PATH" ] || pf_fail "cannot read $RMEM_PATH - kernel receive-buffer ceiling unknown"
RMEM="$(tr -dc '0-9' < "$RMEM_PATH")"
[ -n "${RMEM:-}" ] || pf_fail "read an empty value from $RMEM_PATH"

# Linux doubles SO_RCVBUF for bookkeeping (sock_setsockopt: sk_rcvbuf =
# 2 * min(requested, rmem_max)), and Cyclone compares the DOUBLED value it
# reads back against its minimum. So the true threshold is rmem_max >=
# min_bytes/2, and comparing the doubled figure is what the runtime actually
# does - being stricter here would NO-GO a rig that works.
EFFECTIVE="$((RMEM * 2))"
MIN_MB="$((MIN_BYTES / 1024 / 1024))"

if pf_ge "$EFFECTIVE" "$MIN_BYTES"; then
    pf_pass "net.core.rmem_max=${RMEM} gives DDS ${EFFECTIVE} B, meets the ${MIN_MB} MB minimum in $(basename "$CONFIG")"
fi

pf_fail "net.core.rmem_max=${RMEM} gives DDS only ${EFFECTIVE} B but $(basename "$CONFIG") demands ${MIN_BYTES} B (${MIN_MB} MB) - EVERY node will die at rmw_create_node. Fix on the HOST (not the container), and persist it or the next reboot repeats this: sudo sysctl -w net.core.rmem_max=${MIN_BYTES} && echo 'net.core.rmem_max=${MIN_BYTES}' | sudo tee /etc/sysctl.d/60-cyclonedds.conf"
