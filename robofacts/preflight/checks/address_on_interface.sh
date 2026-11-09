#!/usr/bin/env bash
# The rig's IP address is carried by the interface we think it is.
#
# Why this is a HARD gate and not a nicety
# ----------------------------------------
# The rig computer can reach the mocap network over either of two radios, and they are
# NOT equivalent. The internal Realtek chip stalls its downlink ~59 ms every
# 8.000 s (buffered, not lost - the client dozes and the AP holds its frames),
# which lands in the bags as mocap "teleports" of 50-160 mm. The USB dongle,
# with power save genuinely disabled at module load, measures clean: 0 holes in
# 190 s where ~24 were due.
#
# Both radios carry the same static address, so a fallback is INVISIBLE. If the
# dongle is knocked out of its port - it protrudes from a robot that walks - and
# someone brings the internal profile up to recover, the rig keeps working, the
# feed keeps flowing, every other check keeps passing, and the trials silently
# go back to teleporting. That failure mode cost four days to find once.
#
# So the gate asserts WHICH interface holds the address, turning a silent
# degradation into a refusal to fly.
#
#   address_on_interface.sh addr=192.168.0.20 iface=wlx58044ff1255d [why=...]
source "${PF_LIB:-$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh}"
PF_HELP="address_on_interface.sh addr=<ip> iface=<name> [why=<one-line reason>]"
pf_parse_args "$@"
pf_require_args addr iface

ADDR="$(pf_arg addr)"
IFACE="$(pf_arg iface)"
WHY="$(pf_arg why "")"

# Prefer iproute2, fall back to python3. The rig container is network_mode:host
# so it sees the host's interfaces either way, but `ip` is not guaranteed to be
# installed in every image and a missing binary must not silently skip a HARD
# check.
pf_addr_owner() {
    if command -v ip >/dev/null 2>&1; then
        ip -o -4 addr show 2>/dev/null | awk '{split($4,a,"/"); print $2, a[1]}'
    elif command -v python3 >/dev/null 2>&1; then
        python3 - <<'PY'
import socket
try:
    import psutil  # not assumed present
except ImportError:
    psutil = None
if psutil:
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET:
                print(name, a.address)
else:
    # stdlib only: walk the interface list via SIOCGIFADDR
    import array
    import fcntl
    import struct
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    names = array.array("B", b"\0" * 4096)
    outbytes = struct.unpack("iL", fcntl.ioctl(
        s.fileno(), 0x8912,  # SIOCGIFCONF
        struct.pack("iL", 4096, names.buffer_info()[0])))[0]
    data = names.tobytes()[:outbytes]
    for i in range(0, len(data), 40):
        name = data[i:i + 16].split(b"\0", 1)[0].decode()
        addr = socket.inet_ntoa(data[i + 20:i + 24])
        if name:
            print(name, addr)
PY
    else
        return 1
    fi
}

OWNERS="$(pf_addr_owner)" \
    || pf_fail "cannot enumerate interfaces (no 'ip' and no python3) - the interface guard could not run, so it does not pass"

ACTUAL="$(echo "$OWNERS" | awk -v a="$ADDR" '$2 == a {print $1}' | head -1)"

if [ -z "${ACTUAL:-}" ]; then
    pf_fail "$ADDR is not configured on ANY interface - the rig is off its network${WHY:+ ($WHY)}"
fi

if [ "$ACTUAL" != "$IFACE" ]; then
    pf_fail "$ADDR is on '$ACTUAL', expected '$IFACE' - the rig fell back to the other interface and the feed is NOT the one that was characterised${WHY:+. $WHY}"
fi

pf_pass "$ADDR is on $IFACE as expected"
