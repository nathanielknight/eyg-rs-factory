# Set up Incus

```
sudo apt install incus
sudo usermod -aG incus-admin $USER   # log out and back in
incus admin init --minimal           # or drop --minimal for the interactive setup`
```

Test with `incus launch images:debian/13 test --vm`

# Golden image

The `eyg-golden` profile (`configfiles/incus-eyg-golden-profile.yaml`) builds a
Debian 13 VM (1 CPU, 4 GiB RAM, 20 GiB disk) via cloud-init with git, Node LTS +
npm, `eyg-run`, Rust (rustup), uv + latest Python, and Claude Code. Tools are
installed for the passwordless-sudo `agent` user; work goes in `/home/agent/work`.

Build (or rebuild) the image and publish it as the local alias `eyg-golden`:

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

Run workloads in VMs on the `eygbr0` bridge (10.196.0.0/24), never on
`incusbr0`. Host-side Incus ACLs on each VM's NIC enforce the limits, so root
inside the guest can't lift them:

| Mode    | Can reach                                                      |
|---------|----------------------------------------------------------------|
| `ci`    | host `10.196.0.1:8080` only                                    |
| `agent` | host `10.196.0.1:8080` + Anthropic API (`160.79.104.0/23:443`) |

Nothing else is reachable: no other host ports, LAN, Tailscale, internet, or
other VMs. DNS resolves only `anthropic.com` names. Spoofed source addresses are
dropped. **Package registries are not reachable either**, so dependencies must
be in the image or served via the coordinator.

Create or update the bridge, ACLs and profiles (safe to re-run after editing
`configfiles/incus-eygbr0-network.yaml`, `incus-acl-eyg-*.yaml` or
`incus-eyg-{agent,ci}-profile.yaml`):

```
./scripts/setup-network.sh
```

The coordinator API must listen on `10.196.0.1:8080`. To change the port, edit
both ACL files and re-run the setup script.

Launch with the script. It checks the VM really got the restricted NIC and
deletes it if not:

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
