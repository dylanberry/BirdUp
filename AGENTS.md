# AGENTS.md

Guidance for coding agents working in this repo. Update this file when you
change architecture, deployment, or workflow conventions — keep it short;
detail belongs in the linked docs, not here.

## What this is

- **Bird Up!** — a BirdNET-Pi fork that adds a live species-collage UI
  (upstream readme: [README.upstream.md](README.upstream.md)).
- This repo is the source of truth for **one live deployment**: a Raspberry Pi 4
  running 24/7 bird detection in a Toronto backyard (solar power planned).
- `avian/` and `frame/` are our code. **Everything else is upstream BirdNET-Pi** —
  don't edit upstream files unless the change is meant to be a patch
  (see `docs/deployment/analysis.py.patch` for the pattern).

## Read first (follow the links; don't duplicate them here)

| Doc | Covers |
|---|---|
| [README.md](README.md) | Product overview, BOM, install, repo layout |
| [docs/deployment/README.md](docs/deployment/README.md) | Caddy/BirdNET-Pi config, PHP-FPM permissions, deploy script, OTA server requirements |
| [avian/scripts/README.md](avian/scripts/README.md) | Illustration pipeline (pregen → cutout → masks), sex variants, per-species tuning |
| [frame/README.md](frame/README.md) | Optional e-ink wall display |
| `../tsim7080g-node/CONTEXT.md` | The ESP32 mic-node firmware (the audio source) — sibling repo `~/code/tsim7080g-node` |
| `CONTEXT.local.md` | Gitignored local-only ops notes (Pi sudo state, workarounds). **Never commit.** |

## Live environment

| Item | Value |
|---|---|
| Pi (LAN) | `192.168.86.50`, SSH `dylanberry@192.168.86.50` (Tailscale/hostname in `CONTEXT.local.md`) |
| Mic node | `192.168.86.51` (T-SIM7080G; WebUI reachable only in ~15 s dump windows on battery) |
| App root on Pi | `~/BirdNET-Pi/` (overlay at `~/BirdNET-Pi/avian/`) |
| URLs | collage `/`, stock BirdNET-Pi UI `/index.php` |
| Deploy | `./scripts/deploy-to-pi.sh` — needs a temporary sudoers entry on the Pi; the script prints the exact command |

**Secrets live only on the Pi** (`BIRDWEATHER_ID` in `birdnet.conf`, Gemini API
key, OTA upload token). Never commit tokens, passwords, or hashes beyond the
`Caddyfile.sample` placeholder.

## Conventions (do not break)

- **Cache busting:** any `avian/frontend/apt.js` edit → bump `apt.js?v=rNN` in
  `avian/frontend/index.html`. Illustration/mask changes → also bump
  `SKETCH_VERSION` and `IMG_VERSION` at the top of `apt.js`.
- **Caddyfile:** `docs/deployment/Caddyfile` (real bcrypt hash) is gitignored.
  Mirror any structural change into `Caddyfile.sample`.
- **Illustration deploys:** rsync with `--size-only`, never `--delete`
  (already wired in `scripts/deploy-to-pi.sh`).
- **Static-first images (load-bearing):** the frontend resolves bundled
  illustrations straight to `/avian/assets/illustrations/<slug>.png` via
  the `DIMS` table in `apt.js`, bypassing `cutout.php` (a per-species PHP
  burst saturated the fpm pool). Re-run `build_masks.py` after any
  illustration add/remove/rename so DIMS stays 1:1 with the files.
- **Classifier models:** the settings UI `MODEL` enum only lists classifiers
  whose files ship with BirdNET-Pi (V2.4 FP16 is live, MD5-verified). Do not
  select `Perch_v2` or `BirdNET-Go_classifier_*` without downloading the model
  files to the Pi first.
- **Detection config:** live values in `docs/deployment/birdnet.conf.example`
  (`CONFIDENCE=0.5`, `SENSITIVITY=1.35`, `AUDIO_GAIN=3.0`, `DATA_MODEL_VERSION=2`).
  `AUDIO_GAIN` only works because of the `analysis.py` patch.

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

## Hard-won lessons

- The birdnet venv must include **resampy**: dumpd writes 24 kHz WAVs and
  `analysis.py` resamples with `res_type='kaiser_fast'` (resampy-only in
  librosa). Without it every analysis fails and the StreamData backlog
  crash-loops the service, flooding the journal (2026-08-17: 4k-file backlog,
  journal vacuumed). Also: **rembg is pinned ≤2.0.68** — 2.0.70+ requires
  pillow≥12.1, which violates streamlit's `pillow<12` constraint.
- A Gemini API **429 can mean billing/credits exhausted**, not rate limiting.
- Cutouts: **BiRefNet locally, u2netp only on the Pi** — u2netp over-removes
  pale plumage (belly/vent holes).
- Style-reference prints can reinforce wrong-genus drift (a swift collapsed to a
  swallow because the style print had swallows); `ANTI_REF_TRIGGERS` in
  `pregen.py` is the fix mechanism.
- "Missed" detections are usually **threshold-filtered, not pipeline failures** —
  chunk scores just under `CONFIDENCE` are silently dropped.
- Pi sudo for `dylanberry` is **not** passwordless; the service-restart
  workaround is in `CONTEXT.local.md`.

## Open work

1. Generate the remaining ~120 Ontario species illustrations (priority list:
   `avian/scripts/ontario_toronto_labels.txt`; pipeline: avian/scripts/README.md).
2. Rotate credentials previously exposed in chat: Caddy `basic_auth` password,
   BirdWeather token.
3. Solar install (AGM chosen over LiFePO4 — LiFePO4 can't charge below 0 °C).
4. Optional: Perch v2 classifier trial (download model + labels to the Pi first;
   plan a fallback before switching `MODEL`).
5. Field-validate detection levels on the mic-node audio path (A/B vs old USB lav).
