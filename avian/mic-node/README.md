# Mic node — Pi-side receivers

Pi-side deployment for the T-SIM7080G mic node's audio paths. **Buffered
store-and-forward (dumpd, :8557) is the active path** since 2026-08-16
(node fw v1.38+). The UDP push path below (burst/live) is a fallback, and
RTSP pull is retired (`RTSP_STREAM=` empty, `birdnet_recording` inactive).

> Moved here from the node repo (`tsim7080g-node/pi/`) on 2026-08-20 — this
> repo is the source of truth for the Pi deployment. The node firmware lives
> in the sibling repo `~/code/tsim7080g-node` (dump protocol spec:
> `docs/buffered-recording-spec.md` there).

| Port | Consumer | Purpose |
|---|---|---|
| 8557/tcp | `birdnet-dumpd.py` (this dir, §3) | **active**: buffered ADPCM dumps → StreamData WAVs |
| 8555/udp | `birdnet-udp2-recording.sh` (this dir) | fallback: analysis segments → `~/BirdSongs/StreamData` |
| 8556/udp | `livestream.sh` UDP branch (`../..`/scripts/livestream.sh) | icecast live audio (only while node is in *live* mode) |

UDP push details (fallback): the node streams 48 kHz s16le mono PCM.
It only sends to 8556 in **live** mode; in **burst** mode only 8555 gets
audio (duty-cycled 20 s on / 100 s off by default).

## 1. UDP2 recording service (analysis)

```bash
sudo cp avian/mic-node/birdnet-udp2-recording.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/birdnet-udp2-recording.sh
sudo cp avian/mic-node/birdnet-udp2.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now birdnet-udp2
```

Optional `birdnet.conf` overrides:
```ini
UDP2_STREAM_PORT=8555
```

The service writes `%F-birdnet-UDP2-*.wav` into `$RECS_DIR/StreamData`
(`RECORDING_LENGTH`-second segments, same as the RTSP path), and
`birdnet_analysis` consumes them unchanged. Burst gaps are handled with a UDP
read timeout (`UDP_READ_TIMEOUT_US`, default 10 min): on a dead stream ffmpeg
exits (finalizing the open segment's WAV header so analysis consumes it
normally) and the loop respawns a fresh listener. Without the timeout a node
dying mid-segment leaves the WAV open and header-unfinalized forever -
invisible to `birdnet_analysis` (no IN_CLOSE_WRITE; open files are excluded
from its backlog) and later bursts would append to the stale segment.

## 2. Live audio (icecast) — Bird Up! hook

Already wired in this repo:

- `avian/api/live-audio.php` — `?action=start|end|status`; starts `livestream`
  on demand, toggles the node `/api/live?on=1|0`, and restores both on end.
- `avian/frontend/apt.js` — the drawer LIVE AUDIO button calls start/end.
- `scripts/livestream.sh` — added a UDP source branch driven by
  `UDP_LIVESTREAM_PORT` (default 8556) in `birdnet.conf`:
  ```ini
  UDP_LIVESTREAM_PORT=8556
  ```
  The node switches to continuous push while live, so the icecast feed is
  gap-free during a listening session. When the session ends the node returns
  to burst mode ("ends if the mic node was initially in burst mode").

### Node config on the Pi

`avian/config/mic-node.json`:
```json
{ "url": "http://192.168.86.51", "service": "livestream" }
```
(`url` = the node's LAN address; the node's `/api/live` is open on the LAN.)

### Sudoers note

`birdnet-udp2` runs as `pi`; `livestream` start/stop from PHP (caddy user)
is covered by the existing `/etc/sudoers.d/010_caddy-nopasswd` rule. If you
tighten that, add to `/etc/sudoers.d/020_avian-admin`:
```
/bin/systemctl start livestream, \
/bin/systemctl stop livestream, \
/bin/systemctl is-active livestream, \
/bin/systemctl is-active birdnet-udp2
```

## 3. Buffered dump receiver (dumpd) — PRIMARY analysis path since 2026-08-16

With node fw v1.38+ in buffered store-and-forward mode, the node does NOT
push UDP; it TCP-dumps ADPCM frames to the Pi on **:8557** every `udp_buf_int_s`
(default 300 s). `birdnet-dumpd.py` decodes them to 15 s 24 kHz WAVs named by
capture time (`%F-birdnet-UDP2-*.wav` → StreamData; `birdnet_analysis`
resamples and consumes them unchanged).

Installed 2026-08-17 as a systemd unit:

```bash
sudo cp avian/mic-node/birdnet-dumpd.py /usr/local/bin/
sudo chmod +x /usr/local/bin/birdnet-dumpd.py            # else exit 203/EXEC
sudo sed 's/<USER>/dylanberry/g' avian/mic-node/birdnet-dumpd.service | sudo tee /etc/systemd/system/birdnet-dumpd.service
                                                       # tee, not `>` (redirect runs unprivileged)
sudo pkill -f 'nohup.*birdnet-dumpd' 2>/dev/null; pkill -f birdnet-dumpd.py  # old copies hold :8557
sudo systemctl daemon-reload
sudo systemctl enable --now birdnet-dumpd
```

Watch dumps: `journalctl -u birdnet-dumpd -f` (expect a connection from
192.168.86.51 every 5 min, then `wrote ...wav` lines + `dump complete`).
3 consecutive dump failures reboot the node (wedge detector), so this service
must stay up — `Restart=always` is in the unit. When dumpd is the active path,
`RTSP_STREAM` in birdnet.conf is empty and `birdnet_recording` is inactive;
`birdnet-udp2` can stay enabled but receives nothing.

A successful dump also refreshes `StreamData/.last-dump` (heartbeat marker).
The cluster's birdup-exporter reads it (and the service states directly) for
the `birdup_last_dump_timestamp_seconds` / `birdup_service_active` metrics —
WAVs are consumed by analysis ~1-2 min after landing, so the dir is routinely
empty and WAV mtime is NOT a reliable liveness signal. (`avian/api/health.php`
served this role for the retired k8s birdup-health probe; it is gone.)

### Node telemetry (JSONL staging)

Every dump attempt with a complete header (success OR failure) appends one
JSON line to `~/BirdNET-Pi/data/node-telemetry/node-telemetry-YYYY-MM-DD.jsonl`
(UTC day files). The record is every header field (numeric-coerced) plus
`ts`, `src_ip`, `dump_ok`, `dump_bytes`, `segments`, `duration_s`, and
`error` on failure. Node fw v1.58+ sends battery/power (`batt_mv`, `batt_pct`,
`batt_chg`, `batt_vbus`, `batt_mode`, `batt_eta_full_min`, `batt_life_min`),
thermal (`esp_temp_c`, `pmu_temp_c`), radio (`rssi_dbm`, `tx_dbm`, `eco_mode`,
`eco_effective`), buffer (`dropped_frames`, `buf_pending_frames`,
`buf_used_pct`, `buf_dump_fails`), and boot state (`uptime_s`,
`restart_count`, `boot_reason`, `prev_reboot`, `fw_version`); older nodes
send only the original 7 fields. Telemetry rides the existing dump
connection — no extra node radio activity (ECO-safe).

Overrides: `NODE_TELEMETRY_DIR`, `NODE_TELEMETRY_RETENTION_DAYS` (default 45;
swept once per UTC day). This is the staging point for a later Prometheus
exporter — note `dropped_frames` is a per-boot lifetime counter, and
`restart_count`/`boot_reason`/`prev_reboot` identify boot boundaries.

```bash
tail -f ~/BirdNET-Pi/data/node-telemetry/node-telemetry-$(date -u +%F).jsonl | jq .
```

Redeploy after editing `birdnet-dumpd.py`:

```bash
sudo cp avian/mic-node/birdnet-dumpd.py /usr/local/bin/
sudo chmod +x /usr/local/bin/birdnet-dumpd.py
sudo systemctl restart birdnet-dumpd
```

## 4. Smoke test

```bash
# node side (burst mode): watch segments appear
ls -lt ~/BirdSongs/StreamData/ | head
# live toggle (Bird Up! drawer → LIVE AUDIO) — then both ports should flow:
sudo tcpdump -i any -n udp port 8555 or port 8556   # (needs sudo)
```
## 5. Prometheus exporter stack (node telemetry → cluster)

The cluster scrapes sb-birdnet-pi4 for the full Bird Up! telemetry picture
(see the homelab repo, `docs/birdup-prometheus-telemetry-plan.md`):

| Port | Service (unit) | Exposes |
|---|---|---|
| 9558/tcp | `node-telemetry-exporter.py` (`birdup-exporter`) | `birdnode_*` node telemetry (tailed JSONL), `birdup_service_active`, dump heartbeat `.last-dump`, `birdup_detections_*` (incremental birds.db counters). v1.73+ adds the night-sleep bracket: `birdnode_night_sleep` (0/1), `birdnode_night_wake_timestamp_seconds` (current cycle's planned wake, persisted until the next dusk bracket), `birdnode_night_bracket_timestamp_seconds`, `birdnode_night_slept_seconds` — see `plan-2026-09-23-night-sleep-observability.md`. The nightly 8-15 h sleep flatline is expected; `BirdnodeDumpStale`/`BirdUpDumpStale` are exempt until planned wake + 30/60 min, and `BirdnodeNightSleepMissed` fires when telemetry keeps flowing past expected dusk with no bracket (alert rules in the homelab repo, `prometheusrule-birdup.yaml`). |
| 9100/tcp | `prometheus-node-exporter` (Debian pkg) | Pi host metrics (CPU/mem/disk/temp) |
| 2020/tcp | Caddy site (`:2020` in Caddyfile) | Caddy built-in `/metrics` (proxied from the loopback admin endpoint) |
| 9253/tcp | `php-fpm_exporter` (`birdup-phpfpm-exporter`, runs as caddy) | pool `www` status via the FastCGI unix socket (`pm.status_path=/status`) |
| 3903/tcp | `mtail` (`birdup-mtail`) | Caddy access-log counts (`requests_total`/`status_total`/latency from `/var/log/caddy/access.log`) |

Deploy the whole stack (idempotent; requires dylanberry passwordless sudo on
the Pi):

```bash
./scripts/deploy-monitoring-to-pi.sh
```

This copies the exporter + units, validates+installs the Caddyfile, enables
`pm.status_path` in `www.conf`, installs mtail + php-fpm_exporter binaries
(arm64 GitHub releases), and smoke-tests every port. **The Pi is a deploy
target, not a source of truth** — edit files in this repo, never ad-hoc on
the Pi; back-port any emergency Pi hotfix the same day.

Exporter details: the JSONL tail seeds from the newest line at startup (no
history re-count), accumulates `birdnode_dump_records_total` from then on,
and persists cumulative detection counters (rowid-based increments) in
`~/.local/state/birdup-exporter/state.json` so restarts don't lose them.
`dropped_frames` is exported as observed (per-boot counter); Prometheus
`increase()`/`rate()` handle the boot reset. Never alert on WAV count —
StreamData is routinely empty between dumps (analysis consumes them in
1-2 min); the `.last-dump` marker is the liveness signal.
