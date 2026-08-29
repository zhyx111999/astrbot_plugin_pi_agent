# Bundled Pi runtime

The install payload is a compressed linux-x64 archive:

```text
runtime/vendor/pi-runtime-linux-x64.tar.xz
```

When the plugin loads, it unpacks Node `22.19.0` and Pi `0.84.2` into `node/` and `pi/`, then deletes the archive so disk does not keep both copies. Other hosts still use the PATH / nvm fallback.

If the archive is missing from a checkout, download `pi-runtime-linux-x64.tar.xz` from the GitHub Release assets into `runtime/vendor/` and reload the plugin.
