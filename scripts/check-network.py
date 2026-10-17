#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Check what restricted VMs can actually reach.

Launches a VM per mode with scripts/launch-vm.sh, probes the network from root
inside it, and compares the results with what SETUP.md promises.

DNS is out of scope: Incus lets the VMs query any resolver on the host (e.g.
incusbr0's dnsmasq), so DNS tunnelling is possible. That's accepted; this
checks ordinary reachability. Exits non-zero
on any mismatch. Each VM is deleted afterwards.

    scripts/check-network.py            # both modes
    scripts/check-network.py ci         # one mode

Run it after changing the bridges, ACLs, profiles or host firewall. While it
runs it listens on ports 8080 and 8081 of each bridge IP (skipping any already
in use, e.g. by the coordinator), so an allowed port shows up as open rather
than refused.
"""

import argparse
import errno
import os
import secrets
import socket
import subprocess
import sys
import threading
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PROJECT = os.environ.get("EYG_INCUS_PROJECT", "factory")

BRIDGE_IP = {"agent": "10.196.0.1", "ci": "10.197.0.1"}
COORDINATOR_PORT = 8080
BLOCKED_PORT = 8081  # on the bridge IP, but not allowed by either ACL
ANTHROPIC_IP = "160.79.104.10"  # inside 160.79.104.0/23

# Runs as root in the guest. Arguments: own bridge IP, then every other host
# address. Prints one "check: result" line per probe.
PROBE = r"""
own=$1; shift
tcp() {
    timeout 5 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null
    case $? in 0) echo open ;; 124) echo timeout ;; *) echo refused ;; esac
}
# Raw UDP A query for example.com; prints the rcode and answer count. Only
# used to check the VM can't talk to outside resolvers directly.
dnsq() {
    timeout 4 bash -c "exec 3<>/dev/udp/$1/53
        printf '\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01' >&3
        head -c 12 <&3 | od -An -tu1" 2>/dev/null | {
        read -r _ _ _ f2 _ _ a1 a2 _ || { echo "no reply"; return; }
        case $((f2 & 15)) in 0) r=NOERROR ;; 3) r=NXDOMAIN ;; 5) r=REFUSED ;; *) r="rcode $((f2 & 15))" ;; esac
        echo "$r $((a1 * 256 + a2))"
    }
}
resolve() { getent hosts "$1" >/dev/null && echo yes || echo no; }
echo "resolve api.anthropic.com: $(resolve api.anthropic.com)"
echo "anthropic api: $(curl -sS -m 8 -o /dev/null -w '%{http_code}' \
    --resolve api.anthropic.com:443:ANTHROPIC_IP https://api.anthropic.com/ 2>/dev/null)"
echo "internet tcp 1.1.1.1:443: $(tcp 1.1.1.1 443)"
echo "internet ping 1.1.1.1: $(ping -c1 -W3 1.1.1.1 >/dev/null 2>&1 && echo yes || echo no)"
echo "internet dns 8.8.8.8: $(dnsq 8.8.8.8)"
echo "own tcp $own:COORDINATOR_PORT: $(tcp $own COORDINATOR_PORT)"
for port in BLOCKED_PORT 22; do echo "own tcp $own:$port: $(tcp $own $port)"; done
for ip in "$@"; do
    for port in 22 COORDINATOR_PORT BLOCKED_PORT; do
        echo "host tcp $ip:$port: $(tcp $ip $port)"
    done
done
""".replace("ANTHROPIC_IP", ANTHROPIC_IP).replace(
    "COORDINATOR_PORT", str(COORDINATOR_PORT)
).replace("BLOCKED_PORT", str(BLOCKED_PORT))


def incus(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["incus", "--project", PROJECT, *args], **kwargs)


def host_addresses() -> list[str]:
    "Every IPv4 address on the host, loopback excluded."
    out = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "scope", "global"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [line.split()[3].split("/")[0] for line in out.splitlines()]


def listen(addresses: list[str], ports: list[int]) -> list[str]:
    "Accept-and-close listeners in daemon threads; returns what was skipped."
    skipped = []
    for ip in addresses:
        for port in ports:
            try:
                sock = socket.create_server((ip, port))
            except OSError as e:
                if e.errno != errno.EADDRINUSE:
                    raise
                skipped.append(f"{ip}:{port}")
                continue

            def serve(sock=sock):
                while True:
                    conn, _ = sock.accept()
                    conn.close()

            threading.Thread(target=serve, daemon=True).start()
    return skipped


def expected(mode: str, others: list[str]) -> dict[str, tuple[str, callable]]:
    "check name -> (description, predicate on the probe's result)."
    own = BRIDGE_IP[mode]
    is_ = lambda want: (want, lambda got: got == want)  # noqa: E731
    not_open = ("not open", lambda got: got in ("refused", "timeout"))
    no_answer = ("no answer", lambda got: not got.startswith("NOERROR"))
    checks = {
        "internet tcp 1.1.1.1:443": not_open,
        "internet ping 1.1.1.1": is_("no"),
        "internet dns 8.8.8.8": no_answer,
        f"own tcp {own}:{COORDINATOR_PORT}": is_("open"),
        f"own tcp {own}:{BLOCKED_PORT}": not_open,
        f"own tcp {own}:22": not_open,
    }
    if mode == "agent":
        checks["resolve api.anthropic.com"] = is_("yes")
        checks["anthropic api"] = ("an HTTP status", lambda got: got not in ("", "000"))
    else:
        checks["resolve api.anthropic.com"] = is_("no")
        checks["anthropic api"] = ("no response", lambda got: got in ("", "000"))
    for ip in others:
        for port in (22, COORDINATOR_PORT, BLOCKED_PORT):
            checks[f"host tcp {ip}:{port}"] = not_open
    return checks


def check(mode: str, addresses: list[str]) -> bool:
    name = f"netcheck-{mode}-{secrets.token_hex(3)}"
    own = BRIDGE_IP[mode]
    others = [a for a in addresses if a != own]
    print(f"== {mode}: launching {name}", flush=True)
    try:
        subprocess.run([SCRIPTS / "launch-vm.sh", mode, name], check=True)
        out = incus(
            "exec", name, "--", "bash", "-s", "--", own, *others,
            input=PROBE, capture_output=True, text=True, check=True,
        ).stdout
    finally:
        incus("delete", "--force", name, capture_output=True)

    results = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
    ok = True
    for key, (want, pred) in expected(mode, others).items():
        got = results.get(key, "<missing>")
        good = pred(got)
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'} {key}: {got}" + ("" if good else f" (want {want})"))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    # argparse rejects a list default alongside choices, so default afterwards.
    parser.add_argument("modes", nargs="*", choices=["agent", "ci"])
    args = parser.parse_args()
    modes = args.modes or ["agent", "ci"]

    addresses = host_addresses()
    skipped = listen(list(BRIDGE_IP.values()), [COORDINATOR_PORT, BLOCKED_PORT])
    if skipped:
        print(f"already listening (left alone): {', '.join(skipped)}")

    results = {mode: check(mode, addresses) for mode in modes}
    print()
    for mode, ok in results.items():
        print(f"{mode}: {'PASS' if ok else 'FAIL'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
