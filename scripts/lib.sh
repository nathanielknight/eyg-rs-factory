# Shared helpers for the incus scripts. Source it; don't run it.
#
# Everything the factory owns (images, profiles, instances) lives in its own
# Incus project, named by EYG_INCUS_PROJECT (default `factory`). Networks and
# ACLs stay in the `default` project: per-project networks need OVN, and a
# project without `features.networks` uses the default project's.

PROJECT=${EYG_INCUS_PROJECT:-factory}

# Run incus against the factory project.
pincus() { incus --project "$PROJECT" "$@"; }

# Create the project if needed and give it a default profile. With
# `features.profiles` the project starts with an empty default profile, so copy
# the default project's (root disk + incusbr0 NIC) to keep this host's storage
# pool and bridge names.
ensure_project() {
    if ! incus project show "$PROJECT" >/dev/null 2>&1; then
        incus project create "$PROJECT" \
            -c features.images=true \
            -c features.profiles=true \
            -c features.networks=false
    fi
    if [ -z "$(pincus profile device list default)" ]; then
        incus profile show default --project default | pincus profile edit default
    fi
}
