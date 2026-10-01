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

Launch a disposable instance from it, and get a shell as the agent user:

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
