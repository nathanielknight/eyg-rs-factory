#!/usr/bin/env bash
# Launch a network-restricted VM from the golden image.
#
#   scripts/launch-vm.sh agent NAME   # host coordinator port + Anthropic API
#   scripts/launch-vm.sh ci NAME      # host coordinator port only
#
# The VM goes in the Incus project EYG_INCUS_PROJECT (default `factory`).
set -euo pipefail

. "$(dirname "$0")/lib.sh"

usage() { echo "usage: $0 {agent|ci} NAME" >&2; exit 2; }
[ $# -eq 2 ] || usage
MODE=$1
NAME=$2
case "$MODE" in agent|ci) ;; *) usage ;; esac

# Fail closed: until the NIC is verified, any error deletes the VM rather than
# leaving it running with unknown network access.
trap 'echo "error: deleting $NAME" >&2; pincus delete --force "$NAME" 2>/dev/null' EXIT

# The mode profile goes last so its eth0 replaces the default profile's.
pincus launch eyg-golden "$NAME" --vm -p default -p eyg-golden -p "eyg-$MODE" < /dev/null

# The bridge's ACL is what restricts the VM, so check both that eth0 is on the
# mode's bridge and that the bridge carries the mode's ACL. `incus config device
# get` doesn't see profile devices, so check the expanded ones.
case "$MODE" in agent) BRIDGE=eygbr0 ;; ci) BRIDGE=eygcibr0 ;; esac
incus query "/1.0/instances/$NAME?project=$PROJECT" | python3 -c '
import json, sys
bridge = sys.argv[1]
eth0 = json.load(sys.stdin)["expanded_devices"].get("eth0", {})
if eth0.get("network") != bridge or eth0.get("nictype"):
    sys.exit(f"eth0 is not on {bridge}: {eth0}")
' "$BRIDGE"
incus query "/1.0/networks/$BRIDGE?project=default" | python3 -c '
import json, sys
mode = sys.argv[1]
config = json.load(sys.stdin)["config"]
want = {
    "security.acls": "eyg-" + mode,
    "security.acls.default.egress.action": "reject",
    "security.acls.default.ingress.action": "reject",
}
bad = {k: config.get(k) for k, v in want.items() if config.get(k) != v}
if bad:
    sys.exit(f"bridge is not restricted for {mode}: {bad}")
' "$MODE"
trap - EXIT

echo "Waiting for the VM agent..."
until pincus exec "$NAME" -- true 2>/dev/null; do sleep 2; done
echo "$NAME is up ($MODE, project $PROJECT)."
