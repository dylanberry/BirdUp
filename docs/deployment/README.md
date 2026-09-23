# Deployment notes

This folder collects the non-Bird Up! configuration changes made for the
Toronto Raspberry Pi 4 deployment.

## Files

- `Caddyfile.sample` — full Caddy config template. Keeps the collage public and
  password-protects only the admin menu/tools endpoints. Copy to `Caddyfile`,
  replace the placeholder hash, and edit the paths for your system.
- `birdnet.conf.example` — the BirdNET-Pi settings that differ from defaults
  (confidence, sensitivity, audio gain, coordinates placeholder).
- `analysis.py.patch` — patch for `BirdNET-Pi/scripts/utils/analysis.py` to
  apply the configurable `AUDIO_GAIN` before analysis.
- `birdnet_analysis.py.patch` — patch for `BirdNET-Pi/scripts/birdnet_analysis.py`:
  a WAV that raises during analysis (corrupt/truncated, e.g. a writer that
  died mid-segment) is deleted instead of being left in StreamData forever
  as an orphan that is retried (and fails) on every service restart.
- `avian/mic-node/` — the Pi-side mic-node receivers (buffered dump daemon + UDP2
  fallback + systemd units + install notes). See [`avian/mic-node/README.md`](../../avian/mic-node/README.md).
- `crontab-alsa.txt` — ALSA commands to set the USB mic to 100% capture volume
  and disable AGC on every boot.

## Applying

0. Install the mic-node receivers per [`avian/mic-node/README.md`](../../avian/mic-node/README.md)
   (`birdnet-dumpd` is the active audio path; `birdnet-udp2` is the fallback).

1. Copy `Caddyfile.sample` to `/etc/caddy/Caddyfile`, replace the bcrypt hash
   placeholder with your own password, adjust the root path and any hostnames,
   then `sudo caddy reload`.
2. Apply `analysis.py.patch` and `birdnet_analysis.py.patch` in your
   `BirdNET-Pi` directory.
3. Merge the values from `birdnet.conf.example` into your `birdnet.conf`.
4. Add the two `@reboot` lines from `crontab-alsa.txt` to the Pi user's crontab.

## PHP-FPM permissions for corrections

The `correction.php` admin endpoint can hide detections, reidentify them (moving audio/spectrogram files), and append species to `exclude_species_list.txt`. On the live Pi, PHP-FPM runs as the `caddy` user, so that user must have write access to:

- `~/BirdNET-Pi/scripts/birds.db` — `hide` and `reidentify` update the `detections` table.
- `~/BirdSongs/Extracted/By_Date/` — `reidentify` moves `.mp3` and `.png` files into the new species directory.
- `~/BirdNET-Pi/exclude_species_list.txt` — `exclude` appends the requested species.

Verify the permissions on the Pi:

```bash
ls -l ~/BirdNET-Pi/scripts/birds.db
ls -ld ~/BirdSongs/Extracted/By_Date/
ls -l ~/BirdNET-Pi/exclude_species_list.txt
```

If the `caddy` user cannot write them, adjust group ownership and group-write:

```bash
sudo chown :caddy ~/BirdNET-Pi/scripts/birds.db
sudo chmod g+w ~/BirdNET-Pi/scripts/birds.db
sudo chown -R :caddy ~/BirdSongs/Extracted/By_Date/
sudo chmod -R g+w ~/BirdSongs/Extracted/By_Date/
sudo chown :caddy ~/BirdNET-Pi/exclude_species_list.txt
sudo chmod g+w ~/BirdNET-Pi/exclude_species_list.txt
```

## Deploying updates from this workstation

This repo is the source-of-truth copy for the Toronto Pi. After front-end or
auth changes, push the updated files to the live Pi:

- `avian/frontend/index.html`
- `avian/frontend/apt.js`
- `avian/api/correction.php`
- `avian/api/birdnet-api.php`
- `docs/deployment/Caddyfile` (gitignored, contains the real bcrypt hash)
- `avian/forwarding/caddy-auth.caddy` (if the admin path list changes)

Fast path: run `./scripts/deploy-to-pi.sh` from the repo root. It copies the
files, reloads Caddy, and verifies public endpoints return `200` and admin
endpoints return `401`.

The script requires a temporary passwordless sudoers entry on the Pi because the
workstation agent cannot prompt for the Pi sudo password. If the entry is
missing, the script prints the command to create it. On the Pi:

```bash
echo 'dylanberry ALL=(root) NOPASSWD: /usr/bin/cp /tmp/Caddyfile /etc/caddy/Caddyfile, /usr/bin/systemctl reload caddy, /usr/bin/chown -R caddy\:caddy /home/dylanberry/BirdNET-Pi/avian/ota, /usr/bin/rm -f /etc/sudoers.d/avian-deploy' \
  | sudo tee /etc/sudoers.d/avian-deploy \
  && sudo chmod 440 /etc/sudoers.d/avian-deploy \
  && sudo visudo -c
```

The `chown` command makes `~/BirdNET-Pi/avian/ota/` writable by the php-fpm
(`caddy`) user so the OTA admin endpoint can store firmware builds. The script
removes `/etc/sudoers.d/avian-deploy` after the Caddy reload.

## Node firmware OTA server

The Pi hosts the T-SIM7080G node's firmware OTA server; the node fetches from
`http://192.168.86.50/avian/ota/` (compile-time in the node firmware, see
`../tsim7080g-node/`). Admin surface: Bird Up! drawer → **OTA**
(`avian/api/ota-server.php`). Requirements on the Pi:

- `~/BirdNET-Pi/avian/ota/` exists and is owned by `caddy:caddy` (deploy script does this).
- `avian/api/.user.ini` in place — raises `upload_max_filesize` to 8M for the
  firmware upload (php-fpm honors `.user.ini`; the Debian default 2M is too tight
  as the app image grows).
- Caddy serves `/avian/ota/*` statically (no auth — the node fetches it
  unauthenticated over the LAN). Handled by the `handle /avian/ota/*` block in
  `docs/deployment/Caddyfile`.

To publish a build: `pio run` in the node repo, upload
`.pio/build/esp32-s3-devkitc-1/firmware.bin` (app-only image) with a version in
X.Y form from the OTA admin page. The node's own `/ota` page then offers
"Download and install vX".

When editing `apt.js`, bump the cache-busting query string in `index.html`
(`apt.js?v=r...`) so browsers load the new version. The live Caddyfile in
`docs/deployment/Caddyfile` must stay in sync with `Caddyfile.sample` except for
the real bcrypt hash and system-specific paths.

## Monitoring exporter stack (branch: `scripts/deploy-monitoring-to-pi.sh`)

See `avian/mic-node/README.md` §5 for the full table. Deployment notes that
differ from the front-end deploy:

- Runs as **dylanberry** except `birdup-phpfpm-exporter` (must read the
  `caddy:caddy` FastCGI socket — runs as user caddy).
- The Caddyfile now carries a `{ metrics }` global option (served on the
  loopback admin endpoint :2019) plus a `:2020` site proxying only
  `/metrics` for the cluster scrape; the admin API stays loopback-bound.
- Access log: main site writes JSON to `/var/log/caddy/access.log`
  (created `caddy:caddy` 0644; the packaged unit's ProtectSystem is fine —
  /var/log is writable). mtail tails it as dylanberry.
- php-fpm: `pm.status_path = /status` is enabled in the `www` pool by the
  deploy script (single `sed` on `/etc/php/8.4/fpm/pool.d/www.conf` + php-fpm
  restart). No status route is exposed over HTTP — the exporter reads the
  pool status over the FastCGI socket directly, so nothing leaks on the LAN
  or via the public birds.* path.
- php-fpm pool sizing: `deploy-to-pi.sh` sets `pm.max_children = 12`
  (start 4, spare 2–8) in `www.conf`. The stock default of 5 was saturated
  by collage page loads (one image request per species). The frontend now
  serves bundled illustrations statically via DIMS (< Pi 2026-09 change),
  but the larger pool stays as headroom for API/admin bursts; ~400 MB
  worst case, fine on the 4 GB Pi.
