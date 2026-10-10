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

## Documentation map

Follow the links; don't duplicate them here.

| Doc | Covers |
|---|---|
| [README.md](README.md) | Product overview, BOM, install, repo layout |
| [docs/deployment/README.md](docs/deployment/README.md) | Caddy/BirdNET-Pi config, PHP-FPM permissions, deploy script, OTA server requirements |
| [avian/scripts/README.md](avian/scripts/README.md) | Illustration pipeline (pregen → cutout → masks), sex variants, per-species tuning |
| [frame/README.md](frame/README.md) | Optional e-ink wall display |
| `../tsim7080g-node/AGENTS.md` | The ESP32 mic-node firmware (the audio source) — sibling repo `~/code/tsim7080g-node` |
| `CONTEXT.local.md` | Gitignored local-only ops notes (Pi sudo state, workarounds). **Never commit.** |
| [docs/architecture.md](docs/architecture.md) | Audio path, live listening, node OTA, auth layering |
| [docs/landmines.md](docs/landmines.md) | Hard-won lessons: venv/deps, cutout toolchain, thresholds |
| [docs/status.md](docs/status.md) | Open work on the live deployment |

## Essentials

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

## Imperatives

### Conventions (do not break)


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

### Dependency and pipeline constraints

- **`resampy` must be in the birdnet venv.** `dumpd` writes 24 kHz WAVs and `analysis.py`
  resamples with `res_type='kaiser_fast'` (resampy-only in librosa). Without it every analysis
  fails and the StreamData backlog crash-loops the service.
- **`rembg` is pinned ≤2.0.68.** 2.0.70+ requires `pillow≥12.1`, which violates streamlit's
  `pillow<12` constraint.
- **Cutouts: BiRefNet locally, `u2netp` only on the Pi.** u2netp over-removes pale plumage
  (belly/vent holes).
- **A Gemini API 429 can mean billing/credits exhausted**, not rate limiting.
- **"Missed" detections are usually threshold-filtered, not pipeline failures** — chunk scores
  just under `CONFIDENCE` are silently dropped.
- **Pi `sudo` for `dylanberry` is not passwordless** — the service-restart workaround lives in
  `CONTEXT.local.md`.
