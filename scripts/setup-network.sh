#!/usr/bin/env bash
# Create or update the restricted networks used by EYG factory VMs: the
# `eyg-agent` / `eyg-ci` ACLs, the `eygbr0` (agent) and `eygcibr0` (ci) bridges
# that carry them, and the matching profiles.
# Safe to re-run after editing any of the config files.
#
# The bridges and ACLs go in the `default` project; the profiles go in the
# factory project EYG_INCUS_PROJECT (default `factory`), created if needed.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib.sh

# `incus <thing> create` reads YAML from stdin when it isn't a terminal.
apply() {
    local project=$1 kind=$2 name=$3 file=$4
    if incus --project "$project" $kind show "$name" >/dev/null 2>&1; then
        incus --project "$project" $kind edit "$name" < "$file"
    else
        incus --project "$project" $kind create "$name" < "$file"
    fi
}

ensure_project

# ACLs first: the bridges reference them.
apply default "network acl" eyg-ci configfiles/incus-acl-eyg-ci.yaml
apply default "network acl" eyg-agent configfiles/incus-acl-eyg-agent.yaml
apply default network eygbr0 configfiles/incus-eygbr0-network.yaml
apply default network eygcibr0 configfiles/incus-eygcibr0-network.yaml
apply "$PROJECT" profile eyg-ci configfiles/incus-eyg-ci-profile.yaml
apply "$PROJECT" profile eyg-agent configfiles/incus-eyg-agent-profile.yaml

echo "Networks eygbr0 / eygcibr0, ACLs and profiles eyg-ci / eyg-agent ($PROJECT) are up to date."
