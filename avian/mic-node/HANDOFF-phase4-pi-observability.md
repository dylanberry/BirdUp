# HANDOFF: Phase 4 — Pi-side observability (dump-hardening plan, phase 4)

Date: 2026-08-30. Author: field session (v1.60–1.63 install + verification).
References:
- Plan: `~/code/tsim7080g-node/docs/plan-2026-08-30-dump-hardening-logging.md` → **Phase 4**
- Analysis: `~/code/tsim7080g-node/docs/analysis-2026-08-30-dump-collapse.md` (F1–F6)
- Homelab stack: `~/code/k8s/docs/birdup-prometheus-telemetry-plan.md` (status: fully implemented),
  `~/code/k8s/docs/monitoring-alerting.md` (delivery conventions)
- Node firmware: `~/code/tsim7080g-node` (now v1.63, all Phase 1–3 code)

Phase 4 is **independent of node releases** — everything below is Pi/k8s side.
It had three bullet points in the plan: (1) dumpd trailer + `dumpd_v` marker,
(2) a JSONL→Prometheus exporter, (3) alerts. **Status: (1) done and deployed;
(2) already exists and is scraped, but needs a small extension for the new
tl_v=2 fields; (3) not started — alert rules live in the k8s repo.**

---

## 1. What's already done (2026-08-30)

1. **dumpd v2** (`avian/mic-node/birdnet-dumpd.py`, deployed to
   `/usr/local/bin/birdnet-dumpd.py`, service `birdnet-dumpd` active):
   - Failure paths now salvage the trailing partial WAV, reply `P<n>\n` with the
     exact persisted frame count, and mark telemetry records `dumpd_v:2`.
   - Field-verified live: mid-dump SIGKILL → node classified `stall` in 4.2 s,
     systemd auto-restart in 3 s, retry OK (see plan doc's verification block).
2. **Node header now carries tl_v=2 attempt telemetry** (every BUDP1 header →
   every JSONL staging record). New fields, all numeric unless noted:
   | Field | Meaning |
   |---|---|
   | `tl_v=2` | protocol version (dumpd parses generically) |
   | `prev_result` | **string** `ok\|stall\|eof\|connect\|hdr\|ack_timeout\|assoc` — outcome of the PREVIOUS attempt (rides the next header) |
   | `prev_attempt_ms` | wall-clock duration of the previous attempt |
   | `prev_bytes_sent` | bytes the previous attempt moved |
   | `retry_backoff_s` | current retry backoff (0 = none; grows 30→900 s ±10 % on streaks) |
   | `buf_consec_fails` | consecutive dump failures (same value as legacy `buf_dump_fails`) |
   | `dump_max_frames` | user cap (default 12000); effective cap is larger (production floor) |
   | `dumpd_v` | **envelope field** written by dumpd (=2), not from the header |
3. **Existing metrics exporter is live and scraped**: `node-telemetry-exporter.py`
   (`birdup-exporter` unit, user dylanberry, :9558) is deployed 1:1 with the
   repo (md5 `6376711…`, both). Cluster Prometheus already scrapes it — job
   `birdup`, targets `192.168.86.50:9558`, 60 s interval (job
   `birdnode-pi-node` is a DIFFERENT scrape of the Pi's host node_exporter
   :9100; both defined in
   `~/code/k8s/infrastructure/kube-prometheus-stack/configmap-values.yaml`).

Sample record (real, from the verification session) to target when extending:

```json
{"ts":1788121861.3,"src_ip":"192.168.86.51","dump_ok":true,"dump_bytes":537160,
 "segments":3,"duration_s":3.58,"dumpd_v":2,"error":null,
 "node":"birdnode-e79dd8","encoding":"ima-adpcm","rate":24000,"frame_samples":512,
 "frames":2066,"capture_start_epoch":1788121230,"dropped_frames":0,"tl_v":"2",
 "fw_version":"1.63","uptime_s":49,"restart_count":283,"boot_reason":"software",
 "batt_mv":4145,"batt_pct":100,"batt_chg":0,"batt_vbus":1,"axp_st1":40,"axp_st2":20,
 "buf_stalls":0,"pmu_temp_c":36.5,"batt_mode":3,"batt_eta_full_min":-1,"batt_life_min":-1,
 "esp_temp_c":43.1,"rssi_dbm":-54,"tx_dbm":15,"eco_mode":1,"eco_effective":0,
 "cpu_mhz":160,"buf_pending_frames":2068,"buf_used_pct":7.3,"buf_dump_fails":0,
 "capture_rate":24000,"dump_int_s":300,"buf_consec_fails":0,"retry_backoff_s":0,
 "dump_max_frames":12000,"prev_result":"none","prev_attempt_ms":0,"prev_bytes_sent":0}
```

---

## 2. Next: extend `node-telemetry-exporter.py` for tl_v=2

The exporter already maps most of the plan's metric list (battery, temps,
`rssi_dbm`, `tx_dbm`, `cpu_mhz`, eco, buffer, `dropped_frames_total` counter,
`last_dump_bytes/segments/duration_s`, `dump_records_total{result=ok|fail}`,
`telemetry_last_record_timestamp_seconds`, `birdnode_info` boot identity).
**Missing: the six new header fields.** Mapping to add to `_FIELD_METRICS` /
`_DUMP_STAT_METRICS` (plan §4.2 names in italics where they differ):

| JSONL field | Metric to add | Plan's name | Notes |
|---|---|---|---|
| `prev_result` | `birdnode_last_result` **info–style** `{result="ok"}` (or label on the last-dump family) | `birdnode_prev_result` | 7 distinct values — low cardinality, safe as a label on `birdnode_last_dump_info` if preferred |
| `retry_backoff_s` | `birdnode_dump_backoff_seconds` gauge | (unnamed in plan) | 0 normally; alert hook |
| `buf_consec_fails` | `birdnode_buf_dump_fails` already exists (same value) | `birdnode_dump_consec_fails` | pick one name — do **not** export both copies |
| `dump_max_frames` | `birdnode_buf_dump_max_frames` gauge | — | config visibility |
| `prev_attempt_ms` / `prev_bytes_sent` | `birdnode_last_attempt_milliseconds` / `birdnode_last_attempt_bytes` gauges | — | previous-attempt stats (dumpd's `dump_bytes`/`duration_s` are the current attempt) |
| `dumpd_v` | `birdnode_dumpd_version` gauge | `dumpd_v` marker | cheap protocol-level assert |

Already covered (do not duplicate): plan's `birdnode_dump_ok` per attempt →
`birdnode_dump_records_total{result="ok"|"fail"}`; `birdnode_dump_bytes` /
`birdnode_dump_duration_s` → `birdnode_last_dump_bytes` /
`birdnode_last_dump_duration_seconds`; `birdnode_buf_used_pct` →
`birdnode_buf_used_percent`; `birdnode_dropped_frames_total` exists as a counter.

**Decide before coding**: `prev_result` as a standalone `birdnode_last_result{result=...}` gauge is simplest for alerting (`birdnode_last_result{result="stall"} == 1`); labels on an info metric are the Prometheus-idiomatic alternative. Either is fine — just keep one.

---

## 3. Next: alerts (k8s repo — `~/code/k8s`, Flux-managed)

Conventions (see `~/code/k8s/docs/monitoring-alerting.md`): rules in
`infrastructure/kube-prometheus-stack/prometheusrule-birdup.yaml`, label
`birdup="true"`, routed via `alertmanager-bridge-birdup` to the ntfy topic
**`bird-up`** — the SAME topic the mic node itself posts its Thermal/ECO/boot
notifications to (the bridge's `NTFY_TOPIC`; the bridge account ACL grants
`bird-up:rw`). NOTE: monitoring-alerting.md's table row calls this topic
`spadaberry-birdup` — stale, the deployment.yaml is ground truth. Severity →
ntfy priority: critical=urgent, warning=high, info=default; repeat 1/day;
resolved=low. Scrape cadence 60 s.

The plan's four alerts, expressed against the post-extension metric names:

| Alert (name) | Expression (draft) | Severity |
|---|---|---|
| `BirdnodeDumpFails` | `birdnode_buf_dump_fails >= 5 for 10m` → fires ~10 min into a stall | warning |
| `BirdnodeDroppedFrames` | `increase(birdnode_dropped_frames_total[30m]) > 0` | warning |
| `BirdnodeWeakSignal` | `birdnode_rssi_dbm < -65 for 30m` | warning |
| `BirdnodeDumpStale` | `(time() - birdnode_telemetry_last_record_timestamp_seconds) > 2 * 300` (2× the dump interval; the metric exists already) | critical (audio is not landing) |

Field-caution: in the 2026-08-30 30 KB/s bench, a 13-min degraded episode cost
7,623 dropped frames (~2.7 min audio) — `DroppedFrames` will fire on such
episodes where pre-fix nothing did, which is the point. Expect alert noise on
the evening RF dip until thresholds are tuned.

---

## 4. Deploy + verify (Pi side, this repo is the source of truth)

```bash
# edit avian/mic-node/node-telemetry-exporter.py in ~/code/BirdUp, commit, then:
scp avian/mic-node/node-telemetry-exporter.py dylanberry@192.168.86.50:/tmp/
ssh dylanberry@192.168.86.50 'sudo cp /tmp/node-telemetry-exporter.py /usr/local/bin/ && \
  sudo chmod +x /usr/local/bin/node-telemetry-exporter.py && \
  sudo systemctl restart birdup-exporter && sleep 2 && systemctl is-active birdup-exporter'
# smoke the new metrics WITHOUT waiting for the next dump window:
curl -s http://192.168.86.50:9558/metrics | grep -E "birdnode_(last_result|dump_backoff|buf_dump_max|prev_attempt|dumpd_version)"
```
Gotchas (from the dumpd deploy, same rules): `sudo tee` for anything in
`/etc`; `chmod +x` after `cp` (else exit 203/EXEC); `pkill` any nohup'd copy
before `systemctl restart` (port bind conflict). The monitor repo copy must
stay byte-identical to `/usr/local/bin/` — redeploy with `deploy-monitoring-to-pi.sh`
or the scp pattern above; never edit the Pi copy ad-hoc.

No textfile collector in play: `prometheus-node-exporter` (:9100) runs plain
(no `--collector.textfile.directory`), and the cluster scrapes the exporter
HTTP endpoint directly. Keep it that way — don't introduce a textfile dir
unless the scrape target model changes.

---

## 5. Open questions / gotchas for the implementer

1. **`dropped_frames` counter across node reboots**: the exporter comment says
   Prometheus handles the per-boot reset via value-drop; the plan wanted a
   `restart_count` label. `birdnode_info{restart_count=N}` already exists — if
   `increase()` misbehaves across boots, add the label to the counter family
   (keeps `increase()` truthful per boot) and alert on the labeled series.
2. **`buf_dump_fails` vs `buf_consec_fails`**: identical values (both are the
   consecutive-fail counter). Export one; the plan's `birdnode_dump_consec_fails`
   name vs the existing `birdnode_buf_dump_fails` — pick and note in the rule.
3. **Stale doc to fix**: `~/code/k8s/docs/monitoring-alerting.md` lists the
   birdup ntfy topic as `spadaberry-birdup`; the bridge deployment
   (`alertmanager-bridge-birdup`) actually publishes to `bird-up` — correct the
   doc table when next touching the k8s repo.
4. **`assoc` prev_result class**: added in v1.60 beyond the plan's five —
   include it in enum documentation; it means association-timed-out (no send
   happened).
5. **Grafana**: dashboard "Bird Up! Telemetry" (uid `birdup-telemetry`,
   provisioned in `~/code/k8s/.../dashboards/`) can add a "dump health" row
   (backoff gauge + result info + dropped increase) after the exporter lands.
6. **Prior art to not reinvent**: `~/code/k8s/docs/birdup-prometheus-telemetry-plan.md`
   documents the whole Pi→cluster funnel and the retired health.php → metrics
   migration; keep the "never contact the node directly (ECO)" constraint.

---

## 6. Status — IMPLEMENTED (2026-08-30)

All of the above landed same-day:

- **Exporter extended** (`~/code/BirdUp` b088bd9, deployed to the Pi and
  verified in cluster Prometheus): `birdnode_dump_backoff_seconds`,
  `birdnode_buf_dump_max_frames`, `birdnode_last_attempt_milliseconds`,
  `birdnode_last_attempt_bytes`, `birdnode_dumpd_version` gauges and the
  string `prev_result` → `birdnode_last_result{result="..."} 1` special case
  (pre-tl_v2 records omit the new series). `buf_consec_fails` NOT exported
  (identical to `buf_dump_fails`, which the dashboard already panels).
  Exporter version bumped to 1.1.
- **Alert rules reconciled** (`~/code/k8s` 5deadca, live in-cluster per Flux):
  `BirdnodeDumpStale` >600 s critical, `BirdnodeDumpFails` `buf_dump_fails
  >= 5 for 10m` warning, `BirdnodeDroppedFrames` `increase[30m]` warning,
  `BirdnodeWeakSignal` `< -65 dBm for 30m` warning — renamed from the four
  looser pre-Phase4 rules.
- **Dashboard + docs** (`~/code/k8s` f89b0a2): "Dump health" row (6 stats)
  added in front of the powerbench row; `monitoring-alerting.md` topic row
  corrected to `bird-up` (bridge deployment is ground truth).

Follow-ups: tune `WeakSignal`/`DroppedFrames` thresholds after a week of
evening RF-dip noise data; the loose `dashboards/birdup-dashboard.json`
copy is a stale mirror (ConfigMap is the wired source of truth).