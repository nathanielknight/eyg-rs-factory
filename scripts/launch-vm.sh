#!/usr/bin/env bash
# Launch a network-restricted VM from the golden image.
#
#   scripts/launch-vm.sh agent NAME   # host coordinator port + Anthropic API
#   scripts/launch-vm.sh ci NAME      # host coordinator port only
set -euo pipefail

usage() { echo "usage: $0 {agent|ci} NAME" >&2; exit 2; }
[ $# -eq 2 ] || usage
MODE=$1
NAME=$2
case "$MODE" in agent|ci) ;; *) usage ;; esac

# Fail closed: until the NIC is verified, any error deletes the VM rather than
# leaving it running with unknown network access.
trap 'echo "error: deleting $NAME" >&2; incus delete --force "$NAME" 2>/dev/null' EXIT

# The mode profile goes last so its eth0 replaces the default profile's.
incus launch eyg-golden "$NAME" --vm -p default -p eyg-golden -p "eyg-$MODE" < /dev/null

# `incus config device get` doesn't see profile devices, so check the expanded
# ones.
incus query "/1.0/instances/$NAME" | python3 -c '
import json, sys
mode = sys.argv[1]
eth0 = json.load(sys.stdin)["expanded_devices"].get("eth0", {})
ok = eth0.get("network") == "eygbr0" and eth0.get("security.acls") == "eyg-" + mode
if not ok:
    sys.exit(f"eth0 is not the restricted NIC: {eth0}")
' "$MODE"
trap - EXIT

echo "Waiting for the VM agent..."
until incus exec "$NAME" -- true 2>/dev/null; do sleep 2; done
echo "$NAME is up ($MODE)."
