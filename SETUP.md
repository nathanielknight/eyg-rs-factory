# Set up Incus

```
sudo apt install incus
sudo usermod -aG incus-admin $USER   # log out and back in
incus admin init --minimal           # or drop --minimal for the interactive setup`
```

Test with `incus launch images:debian/13 test --vm`
