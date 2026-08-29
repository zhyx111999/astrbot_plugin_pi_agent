# Bundled Pi runtime

This directory holds the plugin-owned Node `22.19.0` / Pi `0.84.2` payload.

- `vendor/pi-runtime-linux-x64.tar.xz` is the packaged linux-x64 runtime.
- `node/` and `pi/` are the extracted layout used at process launch.
- If the extracted files are missing, `pi_agent_bridge.runtime` unpacks the vendor archive on first resolve.

Current payload is linux-x64 only. Other hosts still use the PATH / nvm fallback.
