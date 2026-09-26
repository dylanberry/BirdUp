# powerbench — automated mic-node power experiments

Walks an experiment schedule (`experiments.json`), pushing each phase's node
config via the node's `/api/set`, pausing the phase clock while the node is
charging / below the battery floor / unreachable, and auto-reverting to
baseline + halting when a guard trips.

**How a phase is measured (2026-09-26 rewrite).** The first schedule's numbers
were an artifact: a plain least-squares fit over ~60 s samples of the phase's
*wall* window filtered to `vbus==0` — sparse, non-contiguous and
charging-interleaved — reported −0.73 mV/h and two *positive* slopes, where the
same nights measure ≈ −25 mV/h through contiguous spans. The estimator is now:

1. contiguous discharge **spans** (same boot, no VBUS, no gap > 900 s),
2. each span smoothed to 15-min medians, fitted with **Theil–Sen** (median
   pairwise slope — robust to the fuel gauge's self-calibration steps),
3. aggregated per **cycle** (one cycle = one local calendar day of discharge,
   i.e. one battery session), reported as a median with IQR as the noise band.

A report also carries a **coverage gate**: if the measured spans cover less
than `min_coverage_ratio` (0.6) of the phase's own accrued battery hours, the
phase is reported invalid and counted in `powerbench_phase_discarded_total` — a
phase can no longer "complete" on thin data. Per-cycle numbers are in `/data`
state and `powerbench_phase_report_*` metrics; the offline equivalent, which
buckets by dump-header ground truth instead of phase names, is `reanalyze.py`.

**Night sleep is an invariant, not a phase variable.** It is the shipping
default and the only setting that removes whole hours of radio time, so a phase
whose `set` touches `nightSleep` / `night_sleep(_*)` is refused with an abort,
and if the node reports `night_sleep=0` the clock pauses and the operator is
notified once.

**One cycle, not one hour — and the power comes from the sun.** The node's USB
is fed by a solar panel, so VBUS is present only while the sun is on it
(observed ~10:00–13:00, i.e. it self-charges to ~100% midday and runs on
battery from mid-afternoon through the morning). Charging pauses the phase
clock, so accrual happens in the off-sun windows; each off-sun session is one
cycle and a phase wants ≥3 of them (schedule uses 36 battery-hours ≈ 3–4 solar
days). Two consequences to keep in mind: a panel that is over-provisioned for
this test pins the battery at ~100%, where mV/h is compressed and little
accrues (`PowerbenchNodePlugged` escalates that), and the analyser compares
cycles rather than assuming a fixed daily window.

**Notifications are actionable by contract.** Phase reports and every alert
lead with what happened, state whether the result beat the reference beyond the
cycle-noise band, and end with one `DO:` line; the bridge maps
`annotations.ntfy_title` to the notification title and `ntfy_click`/
`runbook_url` to the ntfy Click deep-link. `digest.py` (CronJob) is silent
unless something changed: a phase completed or was discarded in the last 24 h,
or night sleep is off on the node.

**Primary deployment: in-cluster** (k3s, namespace `birdup-go`, manifests in
the k8s repo `apps/base/powerbench/`). Telemetry source = Prometheus instant
queries against the birdup exporter metrics; metrics served on :9559 and
scraped via ServiceMonitor; tokens from the SOPS `powerbench-auth` secret;
state on a pinned local PV. `powerbench.py` is mounted from a ConfigMap
**generated** by `apps/base/powerbench/gen-configmap.sh` — the file in THIS
repo is the single source of truth; after editing it, run the generator and
commit both repos.

**Fallback deployment: systemd on sb-birdnet-pi4** (`powerbench.service`,
`deploy-powerbench-to-pi.sh`, telemetry source = dumpd JSONL). Kept for
cluster-outage scenarios; do not run both at once (both would push config).

Stdlib-only Python 3, like `node-telemetry-exporter.py`.

## Why push-with-retry

The node is unreachable except during ECO dump windows (~15 s every 5 min).
Config applies therefore poll `/api/set` until every key lands or
`apply_timeout_s` elapses. Node *state* is read-only observation
(Prometheus in-cluster, JSONL on the Pi) — the analysis never depends on the
node answering. The two exceptions, both tolerant of radio-off windows, read
`/api/status`: baseline capture on phase entry, and the night-sleep invariant
check (hourly) — the one thing that must come from the node itself.

## Files (this repo)

| File | Purpose |
|---|---|
| `powerbench.py` | Controller; source for the cluster ConfigMap |
| `experiments.json` | Experiment schedule; source for the cluster ConfigMap |
| `powerbench.json` | Pi-fallback config (jsonl mode, LAN URLs) |
| `powerbench.service` + `deploy-powerbench-to-pi.sh` | Pi fallback only |

Cluster config is `apps/base/powerbench/powerbench.cluster.json` in the k8s repo.
State lives in `state_dir` (`/data` PVC in-cluster, `~/.powerbench` on the Pi).

## Operate

In-cluster:

```sh
kubectl -n birdup-go exec deploy/powerbench -- python3 /app/powerbench.py status
kubectl -n birdup-go exec deploy/powerbench -- python3 /app/powerbench.py pause   # resume|skip|abort
kubectl -n birdup-go logs -f deploy/powerbench
```

On the Pi (fallback mode): `powerbench.py status|pause|resume|skip|abort`.

`powerbench.py status|pause|resume|skip|abort|reset`.

One control command per tick (~60 s); a second command while one is pending
is refused. `reset` restarts the current schedule at phase 0 (archiving the
previous state on the PVC) — deleting `/data/state.json` by hand does not work,
because the running daemon rewrites it on its next tick.

## Secrets

In-cluster: SOPS `powerbench-auth` (namespace birdup-go) — `NTFY_TOKEN`
(dedicated `powerbench` ntfy user, ACL `bird-up:rw`, provisioned in
`apps/base/ntfy/secret.sops.yaml`), `GRAFANA_USER`/`GRAFANA_PASSWORD`
(copied from `grafana-admin-credentials`; swap for a service-account token
later via `GRAFANA_TOKEN`). Env vars override the token files, so the
`GRAFANA_TOKEN`/`NTFY_TOKEN` file paths are only used in Pi-fallback mode.

## Metrics

`powerbench_phase_info{phase,index,config_hash}`, `..._phase_start_timestamp_seconds`,
`..._phase_duration_seconds`, `..._phase_run_seconds` (battery-time accrued),
`..._phase_elapsed_wall_seconds`, `powerbench_paused{reason}` (charging |
low_batt | manual | no_telemetry | night_sleep_off), `powerbench_night_sleep_off`,
`powerbench_halted`, `powerbench_guard_tripped_total{guard}`,
`powerbench_apply_failures_total`, `powerbench_last_transition_timestamp_seconds`,
`powerbench_phase_completed_total{phase}`, `..._valid_total{phase}`,
`..._discarded_total{phase}`, and per-phase reports
`powerbench_phase_report_{slope_mv_per_hour,slope_pct_per_hour,duty_cycle,
coverage_ratio,span_hours,cycles,valid,dropped_frames,restarts,rssi_dbm_avg,
battery_hours}{phase}`.

Scraped via ServiceMonitor (`apps/base/powerbench/deployment.yaml`); rules in
`infrastructure/kube-prometheus-stack/prometheusrule-birdup-powerbench.yaml`
(every rule sets `ntfy_title` + a `DO:` line — see the actionability contract
there).

## Deploy / change flow

```sh
# 1. edit powerbench.py / experiments.json HERE, commit
# 2. regenerate + commit the cluster manifests:
~/code/k8s/apps/base/powerbench/gen-configmap.sh
# 3. push k8s repo -> Flux applies (ConfigMap change rolls the pod via restart:
kubectl -n birdup-go rollout restart deploy/powerbench   # if not automatic
```

First `run` enters phase 0 (the reference phase, no config changes). The
schedule only advances on battery-discharge time, so wall time stretches with
charge cycles — on solar expect ~3–4 days per phase.
