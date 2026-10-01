#!/usr/bin/env bash
# Create or update the restricted network used by EYG factory VMs: the
# `eygbr0` bridge, the `eyg-ci` / `eyg-agent` ACLs, and the matching profiles.
# Safe to re-run after editing any of the config files.
set -euo pipefail

cd "$(dirname "$0")/.."

# `incus <thing> create` reads YAML from stdin when it isn't a terminal.
apply() {
    local kind=$1 name=$2 file=$3
    if incus $kind show "$name" >/dev/null 2>&1; then
        incus $kind edit "$name" < "$file"
    else
        incus $kind create "$name" < "$file"
    fi
}

# ACLs first: the network references eyg-agent.
apply "network acl" eyg-ci configfiles/incus-acl-eyg-ci.yaml
apply "network acl" eyg-agent configfiles/incus-acl-eyg-agent.yaml
apply network eygbr0 configfiles/incus-eygbr0-network.yaml
apply profile eyg-ci configfiles/incus-eyg-ci-profile.yaml
apply profile eyg-agent configfiles/incus-eyg-agent-profile.yaml

echo "Network eygbr0, ACLs and profiles eyg-ci / eyg-agent are up to date."
