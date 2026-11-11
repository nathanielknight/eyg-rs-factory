#!/usr/bin/env bash
# Build the EYG factory golden image and publish it as the local image alias
# `eyg-golden`. Instances are then launched from that image:
#
#   incus launch eyg-golden run-1 --project factory --vm -p default -p eyg-golden
#
# The image and profile live in the Incus project EYG_INCUS_PROJECT (default
# `factory`), which this script creates if needed.
#
# Re-running this script rebuilds from the latest Debian cloud image and
# replaces the `eyg-golden` alias.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib.sh

PROFILE=eyg-golden
IMAGE_ALIAS=eyg-golden
BASE_IMAGE=images:debian/13/cloud
BUILDER=eyg-golden-build

ensure_project

# `incus profile create` reads YAML from stdin when it isn't a terminal.
if pincus profile show "$PROFILE" >/dev/null 2>&1; then
    pincus profile edit "$PROFILE" < configfiles/incus-eyg-golden-profile.yaml
else
    pincus profile create "$PROFILE" < configfiles/incus-eyg-golden-profile.yaml
fi

pincus delete --force "$BUILDER" 2>/dev/null || true
pincus launch "$BASE_IMAGE" "$BUILDER" --vm -p default -p "$PROFILE" < /dev/null

echo "Waiting for the VM agent..."
until pincus exec "$BUILDER" -- true 2>/dev/null; do sleep 5; done

echo "Waiting for cloud-init (this takes a few minutes)..."
pincus exec "$BUILDER" -- cloud-init status --wait --long
pincus exec "$BUILDER" -- test -f /var/lib/eyg-golden-ready

echo "Checking tools..."
pincus exec "$BUILDER" -- su - agent -c '
    set -e
    git --version
    command -v eyg
    cargo --version
    uv --version
    python --version
    claude --version
'

# Instances launched from the image must not re-run the cloud-init build, and
# need their own machine-id (systemd-networkd derives the DHCP client ID from
# it, so clones would otherwise fight over one IP).
pincus exec "$BUILDER" -- sh -c '
    touch /etc/cloud/cloud-init.disabled
    apt-get clean
    truncate -s 0 /etc/machine-id
    rm -f /var/lib/dbus/machine-id
'

pincus stop "$BUILDER"
pincus publish "$BUILDER" --alias "$IMAGE_ALIAS" --reuse \
    description="EYG factory golden image ($(date -u +%Y-%m-%d))"
pincus delete --force "$BUILDER"

echo "Published image '$IMAGE_ALIAS' in project '$PROJECT'."
