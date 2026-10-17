# Set up Incus

```
sudo apt install incus
sudo usermod -aG incus-admin $USER   # log out and back in
incus admin init --minimal           # or drop --minimal for the interactive setup`
```

Test with `incus launch images:debian/13 test --vm`

# Project

The factory's images, profiles and instances live in their own Incus project,
`factory` by default. Set `EYG_INCUS_PROJECT` to use a different name; every
script reads it. The scripts create the project if it's missing, with its own
images and profiles but the `default` project's networks and ACLs. Per-project
networks need OVN, so the `eygbr0` / `eygcibr0` bridges and `eyg-*` ACLs stay
in `default`.
The project's `default` profile is copied from the `default` project's (root
disk and `incusbr0` NIC) the first time.

To skip `--project factory` on manual commands, switch to it:

```
incus project switch factory
```

The examples below assume you have, or pass `--project factory` yourself.

# Golden image

The `eyg-golden` profile (`configfiles/incus-eyg-golden-profile.yaml`) builds a
Debian 13 VM (1 CPU, 4 GiB RAM, 20 GiB disk) via cloud-init with git, the `eyg`
CLI (via `curl -fsSL https://eyg.run/install | bash`), Rust (rustup), uv +
latest Python, and Claude Code. Tools are installed for the passwordless-sudo
`agent` user; work goes in `/home/agent/work`.

Build (or rebuild) the image and publish it as the alias `eyg-golden` in the
factory project:

```
./scripts/build-golden-image.sh
```

For real workloads, launch through `scripts/launch-vm.sh` (see below). A plain
launch like this one has unrestricted network access, so use it only for
poking at the image:

```
incus launch eyg-golden run-1 --vm -p default -p eyg-golden
incus exec run-1 -- su - agent
```

Get a clean slate by deleting and relaunching (`incus delete --force run-1`), or
snapshot a fresh instance and roll back to it:

```
incus snapshot create run-1 clean
incus snapshot restore run-1 clean
```

# Restricted VMs (agent / CI)

Run workloads in VMs on the restricted bridges, never on `incusbr0`. Each mode
has its own bridge, and a host-side Incus ACL on the bridge enforces the limits,
so root inside the guest can't lift them:

| Mode    | Bridge (subnet)              | Can reach                                                      |
|---------|------------------------------|----------------------------------------------------------------|
| `ci`    | `eygcibr0` (10.197.0.0/24)   | host `10.197.0.1:8080` only                                    |
| `agent` | `eygbr0` (10.196.0.0/24)     | host `10.196.0.1:8080` + Anthropic API (`160.79.104.0/23:443`) |

The ACLs are on the bridges rather than the NICs because Incus 6.0 looks NIC
ACLs up in the instance's project, where they don't exist. So whatever is
plugged into a bridge gets that bridge's limits.

Nothing else is reachable: no other host ports, LAN, Tailscale, internet, or
other VMs. DNS resolves only `anthropic.com` names on `eygbr0` and nothing on
`eygcibr0`. Spoofed source addresses are
dropped. **Package registries are not reachable either**, so dependencies must
be in the image or served via the coordinator.

Create or update the bridges and ACLs (in `default`) and the profiles (in the
factory project) (safe to re-run after editing
`configfiles/incus-eyg{,ci}br0-network.yaml`, `incus-acl-eyg-*.yaml` or
`incus-eyg-{agent,ci}-profile.yaml`):

```
./scripts/setup-network.sh
```

The coordinator API must listen on both `10.196.0.1:8080` and
`10.197.0.1:8080`. To change the port, edit both ACL files and re-run the setup
script.

Launch with the script. It checks the VM is on its mode's bridge, and that the
bridge carries the mode's ACL, and deletes the VM if not:

```
./scripts/launch-vm.sh agent run-1
./scripts/launch-vm.sh ci ci-1
```

Run commands as the agent user. Pass Claude credentials per run; never bake
them into the image. For a subscription account, make a one-year token on a
machine with a browser using `claude setup-token`:

```
incus exec run-1 --user 1000 --group 1000 --cwd /home/agent/work \
    --env HOME=/home/agent \
    --env PATH=/home/agent/.local/bin:/home/agent/.cargo/bin:/usr/local/bin:/usr/bin:/bin \
    --env CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
    -- claude -p "..."
```
