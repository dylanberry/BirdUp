# Bird Up! — Architecture

Split out of `AGENTS.md` (2026-10-10). How audio reaches the Pi, how live listening works,
how node firmware is published, and how auth is layered.
See also `docs/deployment/README.md` and the sibling `~/code/tsim7080g-node` repo.

---

## Architecture in brief

- **Audio in (active path):** the mic node buffers ~10 min of 24 kHz IMA-ADPCM
  in PSRAM and TCP-dumps to the Pi on `:8557` every 5 min; `birdnet-dumpd`
  (systemd unit) decodes to 15 s StreamData WAVs named by **capture time**, so
  `birdnet_analysis` and detection timestamps need no changes. RTSP pull and
  UDP burst push remain installed but idle as fallbacks. Receivers + install
  notes: `avian/mic-node/`. Details: node repo.
- **Live listening:** drawer player → `avian/api/live-audio.php?action=start|end`
  → node's `/api/live` switches burst ↔ continuous; `scripts/livestream.sh`
  takes raw PCM from `UDP_LIVESTREAM_PORT` (default 8556) → icecast `/stream`.
  Node address: `avian/config/mic-node.json`.
- **Node firmware OTA:** admin drawer → `avian/api/ota-server.php`; artifacts in
  `~/BirdNET-Pi/avian/ota/` (caddy-writable, created by the deploy script).
  Scripted publishes use a Bearer deploy token (sha256 stored at
  `avian/ota/upload-token`; raw token shown once). See docs/deployment/README.md.
- **Auth:** collage is public; admin endpoints use the `birdup_admin` session
  cookie (30 days; verify against `AV_AUTH_HASH`), reverse-proxied tools
  (`/log*`, `/stats*`, `/terminal*`) fall back to Caddy `basic_auth`.
