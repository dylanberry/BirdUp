# Plan: night-sleep observability — Bird Up! side (exporter metrics + alert tolerance)

Date: 2026-09-23. Status: **implemented + deployed live** (see Implementation notes below; original plan text retained). Related firmware:
`~/code/tsim7080g-node` v1.73 (night sleep, civil-twilight duty cycle) —
published + field-installed 2026-09-23; the node's first dusk-handoff bracket is
expected tonight ~19:43 EDT.

Sources of truth: `docs/night-sleep-spec.md` + `docs/plan-2026-09-23-night-sleep.md`
(sibling repo, phase E.5 "Ops prep"); `avian/mic-node/HANDOFF-phase4-pi-observability.md`
(exporter + alert conventions that this plan extends).

## Why this exists

Night sleep deep-sleeps the node overnight (single chunk, no radio, no dumps):
Prometheus flatlines 8–15 h every battery night. **The existing
`BirdnodeDumpStale` (10 min) and `BirdUpDumpStale` (30 min) alerts will fire
all night, every night, starting tonight.** The dump-header bracket (node v1.73)
is the hook that turns the flatline from an alarm into a schedule:

- the **dusk-handoff dump** (window close) carries `night_sleep=1` + `night_wake_s`
  (planned wake epoch) — a clean "we are about to sleep until X" boundary;
- the **first post-wake dump** carries `night_sleep=0` + `night_slept_s` — "we woke".

dumpd already writes every header k=v generically into the JSONL staging
(`write_telemetry` → `field.items()`, numeric-coerced via `_coerce`) — **no dumpd
change is needed**; the fields are in the staging records right now. This plan is
three deltas: exporter surface, alert-rules tolerance + the two spec alert
classes, and a dashboard courtesy panel.

## Facts checked 2026-09-23

| Surface | Location | Current state |
|---|---|---|
| JSONL staging | `~/BirdNET-Pi/data/node-telemetry/node-telemetry-*.jsonl` (Pi, written by `birdnet-dumpd.py`) | Records already contain `night_sleep` (int), `night_wake_s` (int epoch), `night_slept_s` (int) — generic v1.58 header copy. No dumpd change. |
| Exporter | `avian/mic-node/node-telemetry-exporter.py` → live `/usr/local/bin/node-telemetry-exporter.py` (`birdup-exporter` unit, :9558) | `_FIELD_METRICS` generic loop exports numeric fields from the **latest record only** — a naive `night_wake_s` entry would appear at dusk and *vanish* once the morning record (which omits it) replaces it. The exporter needs persisted "last planned wake" state, not a bare metric row. |
| Alert rules | `~/code/k8s/infrastructure/kube-prometheus-stack/prometheusrule-birdup.yaml` (Flux-managed, `birdup: "true"` → Alertmanager bridge → ntfy `bird-up`) | `BirdnodeDumpStale` = `time() - ..._timestamp_seconds > 600` (0m, critical) — fires all night. `BirdUpDumpStale` = `.last-dump` mtime > 1800 (10m, warning) — fires all night. |
| health.php | `avian/api/health.php` | **Retired** (README.md:115-116 documents its removal). The firmware plan's "make health.php's stale check night-tolerant" is satisfied vacuously — no op; the equivalent surface is the alert exemption below. |

## Implementation notes (2026-09-23 — built, deployed, verified live)

Deltas vs the contract above, recorded so the plan stays truthful:

1. **Metric names** carry Prometheus unit suffixes (existing house convention,
   cf. `birdnode_telemetry_last_record_timestamp_seconds`): the plan's
   `birdnode_night_wake_timestamp`→**`birdnode_night_wake_timestamp_seconds`**,
   `birdnode_night_bracket_timestamp`→**`birdnode_night_bracket_timestamp_seconds`**.
   Alert expressions in this file use the deployed names.
2. **Always-emit with 0 defaults**: all four night metrics are rendered even
   before the first dusk bracket (0 = unknown/none). This keeps the alert rules'
   `== 0` guard branches meaningful — an absent series would make the `or
   wake == 0` exemption in B1/B2 evaluate to empty (alert never fires, even
   pre-night-sleep). Verified in-cluster: gauges present at 0 before tonight's
   first bracket.
3. **B2's real rule name** is `BirdUpDumpStale` (not "BirdupDumpHeartbeat");
   the exemption margin is 1 h past planned wake (vs 30 min on the node-side
   rule), matching its 30 min heartbeat threshold ×2.
4. **Exporter restart during the day** drops the held planned-wake until the
   next dusk bracket re-seeds it from a fresh handoff (the seed reads only the
   newest record). Harmless for B1/B2: during the day telemetry flows (stale
   rules inert) and the exemption is restored before the next flatline. One
   narrow class-1 residual: if sleep also fails to trigger *that same evening*,
   `BirdnodeNightSleepMissed` can't fire (no bracket baseline) — accepted; a
   day-restart followed by a same-evening sleep failure is a double fault.
5. **`health.php`**: confirmed retired (README.md:115-116) — no op, as noted.
6. State machine + render are host-tested: `night-observability-smoke.py`
   (28 assertions, two full night cycles incl. the PWRON mid-night edge) —
   run it from this dir before any future exporter change.
7. **Phase C landed**: "Night sleep bracket" timeseries panel added to
   `birdup-telemetry` via `build-dashboard.py` (id 59, mic-node row):
   `birdnode_night_sleep` + the bracket/wake epoch gauges as step-function
   `dateTimeAsEpoch` lines (`>0`-guarded per house convention), regenerated
   through `gen-dashboard-configmap.sh` — never hand-edited.

Deployed:
- `node-telemetry-exporter.py` → Pi `/usr/local/bin/` (md5 `aeebe0a7…`, unit
  restarted, `:9558` serving the four night gauges; cluster Prometheus job
  `birdup` scrapes them).
- `prometheusrule-birdup.yaml` → homelab repo commit `cf12ba5` (merged to
  `main`, Flux applied `main@sha1:cf12ba5f…`); `birdup.node` and
  `birdup.backend` rule groups evaluate with no errors.

## Locked contract (alert semantics)

- **During the night** (`time() < planned wake`), stale/heartbeat alerts must NOT
  fire — the flatline is the feature.
- **After `night_wake_s` + N min with no telemetry** = wake failed / radio dead →
  fires (this IS the spec's alert class (2), implemented as the exempted stale rule).
- **Records still flowing past expected dusk with no new bracket** = sleep failed
  to trigger (class (1)) → fires, but only when it's *supposed* to be night:
  battery + ECO effective (the charging/eco-off gates mean "awake by design").
- Expected dusk is **self-calibrating**: previous dusk bracket + 24 h (the solar
  schedule drifts <2 min/day; no hardcoded hours).

## Phase A — exporter surface (`avian/mic-node/node-telemetry-exporter.py`)

### A1 — persisted night-cycle state in `NodeTelemetry`

Extend the class with a per-cycle state dict that survives record replacement
(update under the existing `self.lock` inside `_apply`):

```
night_state = { sleep: int|None,    # latest night_sleep (0/1)
                wake_epoch: int|None,      # LAST-seen night_wake_s — kept across
                                           # records that omit it; cleared only
                                           # when a NEW night_sleep=1 bracket arrives
                bracket_ts: float|None,    # ts of the record carrying night_sleep=1
                                           # (the dusk handoff; sleep-entry boundary)
                slept_s: int|None }        # latest night_slept_s (planned duration)
```

Rules:
- `night_sleep`/`slept_s` = latest record values (they're always present when set).
- `wake_epoch`: on a record with `night_wake_s` → store it; on a record with
  `night_sleep == 1` → clear it (new cycle: the new `night_wake_s` in the same or
  next record wins). Never cleared by a `night_sleep == 0` record.
- `bracket_ts`: record `ts` when `night_sleep == 1` appears.

### A2 — metrics (`render()`)

Four new output lines, emitted from the state above (not the generic
`_FIELD_METRICS` loop), in the node-telemetry block:

| Metric | Type | Semantics |
|---|---|---|
| `birdnode_night_sleep` | gauge | 1 = latest record is the dusk bracket (node sleeping/just asleep); 0 = awake. Emitted 0 until the first bracket. |
| `birdnode_night_wake_timestamp_seconds` | gauge | **Current sleep cycle's planned wake epoch** (last-seen `night_wake_s`; see A1). The alert-reference primitive. 0 until the first bracket. |
| `birdnode_night_bracket_timestamp_seconds` | gauge | Epoch the dusk handoff record landed (`bracket_ts`) — the "expected dusk" baseline for class (1). |
| `birdnode_night_slept_seconds` | gauge | Planned sleep duration from the current cycle's `night_slept_s` (dashboard aid). |

Keep the generic `_FIELD_METRICS` untouched (the three raw keys must stay out of
it so the persisted semantics win and there is no double-emission).

## Phase B — alert rules (`~/code/k8s/.../prometheusrule-birdup.yaml`)

All in group `birdup.node`, all carrying `birdup: "true"` (existing routing).

### B1 — `BirdnodeDumpStale` (modify; becomes spec class (2))

```
expr: time() - birdnode_telemetry_last_record_timestamp_seconds > 600
      and (birdnode_night_wake_timestamp_seconds == 0
           or time() > birdnode_night_wake_timestamp_seconds + 1800)
```

- Night: `time() < wake` → exempt (flatline is expected). **No change before the
  first-ever bracket** (`== 0` keeps today's behavior).
- Planned wake + 30 min with no record → fires: "wake failed / radio dead" —
  exactly the spec's class (2). *A wake with no post-wake dump within 30 min is a
  real outage whether it booted or not — the first dump is the proof of life.*
- Day: telemetry flows → first clause false.

### B2 — `BirdUpDumpStale` (modify, same exemption, looser margin)

```
expr: time() - birdup_last_dump_timestamp_seconds > 1800
      and (birdnode_night_wake_timestamp_seconds == 0
           or time() > birdnode_night_wake_timestamp_seconds + 3600)
```

### B3 — `BirdnodeNightSleepMissed` (new; spec class (1))

```
expr: birdnode_night_sleep == 0
      and birdnode_eco_effective == 1
      and birdnode_battery_vbus == 0
      and time() - birdnode_telemetry_last_record_timestamp_seconds < 600
      and (
        (birdnode_night_bracket_timestamp_seconds > 0
         and time() - (birdnode_night_bracket_timestamp_seconds + 86400) > 3600)
        or
        (birdnode_night_bracket_timestamp_seconds == 0
         and birdnode_night_wake_timestamp_seconds > 0
         and time() - birdnode_night_wake_timestamp_seconds > 18 * 3600)
      )
for: 30m   severity: warning
```

Semantics: node is awake-by-choice-unblocked (ECO effective, on battery),
telemetry still flowing, and we're >1 h past *today's expected dusk* (previous
bracket + 24 h) with no new `night_sleep=1` → sleep didn't trigger. The 30 m
`for:` absorbs dump-interval jitter. The `bracket == 0 and wake > 0` fallback
(wake+18 h ≈ Toronto dusk proxy) is defensive only — `bracket_ts` and
`wake_epoch` are set by the same bracket record, so no reachable state has one
at 0 and the other >0; retained in the deployed rule as belt-and-braces. Fires
at worst ~1-2 h after normal bed time, only while records are actually still
flowing.

### B4 — untouched rules

`BirdnodeBatteryLow` (battery doesn't drain while deep-sleeping; unchanged),
`BirdnodeDroppedFrames`, `BirdnodeDumpFails`, `BirdnodeWeakSignal`, backend/
frontend rules (heartbeat-exempted ones covered in B2). Re-verify the `for:`
durations after the first two real nights.

## Phase C — dashboard (optional, small)

`infrastructure/kube-prometheus-stack/dashboards/birdup-dashboard-configmap.yaml`
(uid `birdup-telemetry`, generated via `build-dashboard.py` + `gen-dashboard-configmap.sh`):
add one time-series panel, "Night sleep bracket": `birdnode_night_sleep` + a
step function of `birdnode_night_bracket_timestamp_seconds`/`birdnode_night_wake_timestamp_seconds`
so the flatline reads as a scheduled gap, not a dead node. Regenerate via the
generator scripts — never hand-edit the configmap. **Implemented 2026-09-23**
(see Implementation notes item 7).

## Phase D — deploy + validation

### D1 — exporter (Pi)

1. Edit `avian/mic-node/node-telemetry-exporter.py`; commit.
2. Unit smoke on this laptop:
   `python3 -m py_compile ...` + drive `NodeTelemetry` against a fixture JSONL
   with a handoff record (`night_sleep=1`, `night_wake_s`) followed by a wake
   record (`night_sleep=0`, no `night_wake_s`) → assert `birdnode_night_sleep=0`
   while `birdnode_night_wake_timestamp_seconds` still holds the cycle's wake; then a new
   bracket record → wake timestamp replaced. Add the fixture under
   `avian/mic-node/powerbench/`-adjacent tests (see conventions there).
3. Deploy (HANDOFF-phase4 pattern):
   `scp avian/mic-node/node-telemetry-exporter.py dylanberry@192.168.86.50:/usr/local/bin/`
   → `install -m 755` → `systemctl restart birdup-exporter` → check
   `curl localhost:9558/metrics | grep night` on the Pi → commit + update
   README.md exporter row.

### D2 — alert rules (k8s repo)

1. Edit `prometheusrule-birdup.yaml`; commit + push (Flux-managed; HANDOFF-phase4
   reconciled via `flux reconcile` per `~/code/k8s` conventions).
2. Verify in-cluster: rule present + no parse error (`kubectl -n monitoring get
   prometheusrule birdup-alerts -o yaml`; Prometheus rule UI), and the new metric
   name set matches the exporter output.
3. Confirmed-deploy gate: rules land on the **same day as the exporter** so the
   night exemption and the new field set go live together; otherwise tonight's
   flatline false-alarms under the old rules.

### D3 — live validation (first real night)

Expected from the firmware plan's field soak:
- ~19:43 EDT dusk handoff → JSONL record `night_sleep=1` + `night_wake_s`;
  exporter shows `birdnode_night_sleep=1`, `birdnode_night_wake_timestamp_seconds ≈
  06:31 +delta`; no `BirdnodeDumpStale`/`BirdUpDumpStale` all night.
- ~06:31 wake → first dump → record `night_sleep=0` + `night_slept_s`; exporter
  flips `night_sleep=0`, keeps `night_wake_timestamp` (this morning's) until the
  next evening's bracket replaces it.
- Class (2) exercised for real only when the node fails to wake — until then,
  validate the rule by temporarily setting `night_wake_timestamp` artificially
  past (or reviewing the expression against the live scrape with `promtool`).
- Class (1): A/B by disabling night sleep in the node WebUI for one evening →
  `BirdnodeNightSleepMissed` should fire ~dust+1 h; re-enable → clears.
- ≥2 consecutive battery-only nights for a clean flatline measurement
  (USB/charging nights skip sleep by design — no flatline, no exemption exercised).

### D4 — docs

Update `avian/mic-node/README.md` observability section (new metrics + the two
alert rules + "night flatline is scheduled" note) and the dashboard generator
comment if Phase C landed.

## Not in scope

- dumpd changes (none needed — generic header copy already stages the fields).
- Firmware changes (node v1.73 is the contract; the exporter ignores absent
  fields, so older node firmware stays compatible — all four metrics emit 0 until
  the first bracket).
- `health.php` (retired; the exemption above is its successor surface).
- Grafana alerting (Alertmanager-only per `~/code/k8s/docs/monitoring-alerting.md`).
- Battery-floor stop / solar behaviors (firmware repo, separate plans).