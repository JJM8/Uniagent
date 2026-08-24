# Uniagent

(No active temporary rules.)

## Multi-device setup

This repo is synced across multiple machines via Syncthing (see `.stfolder`, `.stignore`, `sync-conflict-*` files). The main PC runs the live Uniagent server; other devices (e.g. Josh's laptop) hold a local copy of the code that syncs to/from it but do not run the "real" instance users interact with day to day. When working from a non-primary device, keep in mind:
- Code edits here sync to the main PC over Syncthing, not via direct deploy/restart — the running server on the main PC may need a restart to pick up changes.
- Sync conflicts can produce stray `*.sync-conflict-*` files; treat these as Syncthing artifacts, not intentional project files.
- If asked to debug "weird" runtime behavior reported from a non-primary device, consider that the device may be a client of the main PC's server (e.g. over the network/tailscale) rather than running its own server — check which server the client is actually pointed at before assuming code-level bugs.
