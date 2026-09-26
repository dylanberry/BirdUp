#!/usr/bin/env python3
"""powerbench — automated power-efficiency experiment controller for the
T-SIM7080G mic node.

Runs either in-cluster (k3s, namespace birdup-go; telemetry source =
Prometheus instant queries against the birdup exporter) or on sb-birdnet-pi4
(telemetry source = dumpd JSONL) — see telemetry_source in the config.
Walks an experiment schedule (experiments.json), applying each phase's node
config via the node's /api/set endpoint, pausing the phase clock while the
node is charging or below the battery floor, and tripping guards (crash /
dropped-frames) that revert the node to baseline and halt the schedule.

Night sleep is an INVARIANT, not a phase variable: it is the shipping default
and the only setting that removes whole hours of radio time, so the controller
refuses any phase whose `set` touches it and pauses if the node reports it off.

Analysis: the per-phase discharge slope is computed with the validated
estimator (contiguous discharge spans -> per-span Theil-Sen over 15-min
medians -> median over CYCLES, one cycle = one local day of discharge), plus a
coverage gate, because the first schedule's plain least-squares fit over the
phase wall window reported -0.73 mV/h and two positive slopes where the same
nights measure -25 mV/h. See the power-bench re-analysis note in the node
repo's AGENTS.md and the offline tool reanalyze.py.

Node state is read-only observation (Prometheus or JSONL — nothing ever polls
the node for state, it is unreachable except during ECO dump windows).
Config changes are pushed by polling /api/set until a dump window answers.

Exposes Prometheus metrics on :9559/metrics:
  powerbench_phase_info{phase,index,config_hash} 1
  powerbench_phase_start_timestamp_seconds
  powerbench_phase_duration_seconds
  powerbench_phase_run_seconds            (battery-time accumulated this phase)
  powerbench_phase_coverage_ratio         (measured spans / accrued battery time)
  powerbench_phase_cycles                 (cycles seen this phase)
  powerbench_paused{reason} 0/1           (charging|low_batt|manual|no_telemetry|
                                           night_sleep_off|awaiting_unplug)
  powerbench_halted 0/1
  powerbench_guard_tripped_total{guard}
  powerbench_apply_failures_total
  powerbench_phase_completed_total{phase}
  powerbench_phase_valid_total{phase}
  powerbench_phase_discarded_total{phase} (failed the coverage gate)
  powerbench_last_transition_timestamp_seconds
  powerbench_phase_report_{slope_mv_per_hour,slope_pct_per_hour,duty_cycle,
                           dropped_frames,restarts,rssi_dbm_avg,battery_hours,
                           coverage_ratio,cycles}{phase}

Modes:
  powerbench.py run      — daemon (systemd)
  powerbench.py status   — print state
  powerbench.py pause|resume|abort|skip|reset — control the running daemon
                           (reset restarts the current schedule at phase 0,
                            archiving the previous state on the PVC)
Stdlib only, mirroring node-telemetry-exporter.py conventions.
"""

import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [powerbench] %(levelname)s %(message)s")
log = logging.getLogger("powerbench")

DEFAULT_STATE_DIR = os.path.expanduser("~/.powerbench")

# Estimator tuning (see the module docstring for why this is not a plain fit).
BIN_S = 900.0          # slope smoothing bin (15 min)
SPAN_GAP_S = 900.0     # max gap between samples inside one span (3x dump interval)

# Known-good baseline values for keys we experiment on, used when /api/status
# does not expose the key. Values are strings: /api/set takes form fields.
DEFAULT_BASELINE = {
    "pwr_cpu_low": "160",
    "udp_buf_int_s": "300",
    "pwr_tx_dbm": "-1",
}

# Config keys a phase may never touch: night sleep is the invariant under test.
FORBIDDEN_SET_KEYS = {"nightSleep", "night_sleep", "night_tz_rule",
                      "night_sleep_lat", "night_sleep_lon"}


_MISSING = object()


def _load_json(path, default=_MISSING):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        if default is not _MISSING:
            return default
        raise


def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])


def _quantile(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    i = q * (len(vals) - 1)
    lo = int(i)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (i - lo)


def _f(v, spec="%.1f"):
    return "n/a" if v is None else spec % v


def _overlap(a, b):
    """True when two [q1,q3] ranges overlap (i.e. no separation). Missing or
    degenerate ranges count as overlapping = 'not proven different'."""
    if not a or not b or len(a) != 2 or len(b) != 2:
        return True
    if a[0] is None or a[1] is None or b[0] is None or b[1] is None:
        return True
    return not (a[1] < b[0] or a[0] > b[1])


def _theil_sen(points):
    """Median pairwise slope of [(x_hours, y)] — robust to the fuel gauge's
    self-calibration steps, which wreck a least-squares fit."""
    """Median pairwise slope of [(x_hours, y)] — robust to the fuel gauge's
    self-calibration steps, which wreck a least-squares fit."""
    n = len(points)
    if n < 4:
        return None
    slopes = []
    for i in range(n - 1):
        xi, yi = points[i]
        for j in range(i + 1, n):
            xj, yj = points[j]
            if xj != xi:
                slopes.append((yj - yi) / (xj - xi))
    return _median(slopes)


def _smoothed(points, bin_h=BIN_S / 3600.0):
    """Median of y within bin_h-hour bins (cancels dump IR-drop and load)."""
    bins = {}
    for x, y in points:
        bins.setdefault(int(x / bin_h + 0.5), []).append(y)
    return [(k * bin_h, _median(v)) for k, v in sorted(bins.items())]


def _spans(samples, max_gap_s=SPAN_GAP_S):
    """Contiguous discharge spans: same boot, no vbus, not charging, and no
    gap larger than max_gap_s between consecutive samples."""
    out, cur, prev = [], [], None
    for s in samples:
        if prev is not None:
            restart_changed = (s.get("restart") is not None
                               and prev.get("restart") is not None
                               and s["restart"] != prev["restart"])
            if s["ts"] - prev["ts"] > max_gap_s or restart_changed:
                if cur:
                    out.append(cur)
                cur = []
        cur.append(s)
        prev = s
    if cur:
        out.append(cur)
    return out


def discharge_samples(samples):
    """Keep only samples taken while discharging on battery."""
    return [s for s in samples
            if s.get("vbus") == 0 and s.get("chg") == 0 and s.get("mv") is not None]


def analyze_samples(spans, min_cycle_h=0.5):
    """Discharge analysis of contiguous spans. Returns (fields, cycles) where a
    cycle is one local calendar day of discharge (one battery session)."""
    days = {}
    for span in spans:
        hours = (span[-1]["ts"] - span[0]["ts"]) / 3600.0
        if hours <= 0:
            continue
        t0 = span[0]["ts"]
        mv = _theil_sen(_smoothed([((s["ts"] - t0) / 3600.0, float(s["mv"]))
                                   for s in span]))
        pct_pts = [((s["ts"] - t0) / 3600.0, float(s["pct"]))
                   for s in span if s.get("pct") is not None]
        pct = _theil_sen(_smoothed(pct_pts)) if len(pct_pts) >= 4 else None
        day = time.strftime("%Y-%m-%d", time.localtime(t0 + (span[-1]["ts"] - t0) / 2.0))
        c = days.setdefault(day, {"day": day, "hours": 0.0, "spans": 0, "mv": 0.0,
                                  "mv_h": 0.0, "pct": 0.0, "pct_h": 0.0,
                                  "mv_first": span[0]["mv"], "mv_last": span[-1]["mv"]})
        c["hours"] += hours
        c["spans"] += 1
        if mv is not None:
            c["mv"] += mv * hours
            c["mv_h"] += hours
        if pct is not None:
            c["pct"] += pct * hours
            c["pct_h"] += hours
        c["mv_last"] = span[-1]["mv"]
    cycles = []
    for day in sorted(days):
        c = days[day]
        cycles.append({
            "day": day,
            "hours": round(c["hours"], 2),
            "spans": c["spans"],
            "mv_per_hour": round(c["mv"] / c["mv_h"], 2) if c["mv_h"] else None,
            "pct_per_hour": round(c["pct"] / c["pct_h"], 2) if c["pct_h"] else None,
            "mv_range": "%s-%s" % (c["mv_first"], c["mv_last"]),
        })
    usable = [c for c in cycles if c["hours"] >= min_cycle_h and c["mv_per_hour"] is not None]
    mv_vals = [c["mv_per_hour"] for c in usable]
    pct_vals = [c["pct_per_hour"] for c in usable]
    fields = {
        "cycles": cycles,
        "cycle_count": len(cycles),
        "valid_cycles": len(usable),
        "span_hours": round(sum(c["hours"] for c in cycles), 2),
        "slope_mv_per_hour": _median(mv_vals),
        "slope_pct_per_hour": _median(pct_vals),
        "slope_mv_iqr": ([_quantile(mv_vals, 0.25), _quantile(mv_vals, 0.75)]
                         if mv_vals else None),
        "slope_pct_iqr": ([_quantile(pct_vals, 0.25), _quantile(pct_vals, 0.75)]
                          if pct_vals else None),
    }
    return fields, cycles


class Controller:
    def __init__(self, cfg, experiments, state_dir):
        self.cfg = cfg
        self.phases = experiments["phases"]
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.state_path = os.path.join(state_dir, "state.json")
        self.control_path = os.path.join(state_dir, "control.json")
        self.lock = threading.Lock()
        self.state = _load_json(self.state_path, default=None) or self._fresh_state()
        self.guard_trips = {}            # guard -> count (process lifetime)
        self.apply_failures = 0

    # ------------------------------------------------------------------ state

    def _fresh_state(self):
        return {
            "phase_index": 0,
            "phase_start_epoch": None,     # wall clock when phase began
            "phase_run_s": 0.0,            # battery-time accumulated this phase
            "paused_reason": None,         # None | "charging" | "low_batt" | "manual"
            "halted": False,
            "applied": {},                 # keys currently pushed to the node
            "baseline": {},                # captured before first mutation
            "phase_start_restart_count": None,
            "phase_start_dropped_frames": None,
            "guard_tripped": None,
            "last_transition_epoch": None,
            "last_tick_epoch": None,
            "reports": {},                 # phase name -> analysis report dict
            "last_valid_phase": None,
            "night_sleep_off": False,      # invariant violation (clock paused)
            "night_sleep_checked_epoch": None,
        }

    def save(self):
        _save_json(self.state_path, self.state)

    @property
    def phase(self):
        if self.state["phase_index"] >= len(self.phases):
            return None
        return self.phases[self.state["phase_index"]]

    # --------------------------------------------------------------- node I/O

    def _node_request(self, path, data=None, timeout=10):
        url = self.cfg["node_url"].rstrip("/") + path
        body = None
        headers = {"X-ESP32MIC-CSRF": "1"}
        if data is not None:
            body = "&".join("%s=%s" % (k, v) for k, v in data.items()).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def node_reachable_status(self):
        try:
            return json.loads(self._node_request("/api/status", timeout=8))
        except Exception:
            return None

    def apply_config(self, kv, deadline_s):
        """Push keys via /api/set, retrying across ECO dump windows until the
        deadline. Returns True on full success."""
        deadline = time.time() + deadline_s
        pending = dict(kv)
        while pending and time.time() < deadline:
            for key in list(pending):
                try:
                    self._node_request("/api/set", data={"key": key, "value": pending[key]})
                    log.info("applied %s=%s", key, pending[key])
                    del pending[key]
                except Exception as exc:
                    log.info("apply %s pending (node in radio-off window?): %s", key, exc)
                    time.sleep(2)
            if pending:
                time.sleep(30)
        if pending:
            self.apply_failures += 1
            log.error("apply timed out, unapplied: %s", sorted(pending))
            return False
        return True

    # -------------------------------------------------------------- telemetry

    def telemetry_snapshot(self):
        """Newest node telemetry as a dict, from the configured source:
        'prometheus' (in-cluster; instant queries against the birdup exporter
        metrics) or 'jsonl' (on-Pi; tails the dumpd day files). Returns None
        when nothing fresh is available."""
        if self.cfg.get("telemetry_source", "jsonl") == "prometheus":
            try:
                return self._snapshot_prometheus()
            except Exception as exc:
                log.warning("prometheus snapshot failed: %s", exc)
                return None
        return self.latest_record()

    def _prom_query(self, query):
        url = (self.cfg["prometheus_url"].rstrip("/") +
               "/api/v1/query?query=" + urllib.parse.quote(query))
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        result = data.get("data", {}).get("result", [])
        return result[0] if result else None

    def _prom_value(self, query):
        res = self._prom_query(query)
        try:
            return float(res["value"][1])
        except (TypeError, KeyError, IndexError, ValueError):
            return None

    def _snapshot_prometheus(self):
        ts = self._prom_value("birdnode_telemetry_last_record_timestamp_seconds")
        if ts is None:
            return None
        snap = {
            "ts": ts,
            "batt_vbus": self._prom_value("birdnode_battery_vbus"),
            "batt_chg": self._prom_value("birdnode_battery_charging"),
            "batt_pct": self._prom_value("birdnode_battery_soc_percent"),
            "restart_count": self._prom_value("birdnode_restart_count"),
            "dropped_frames": self._prom_value("birdnode_dropped_frames_total"),
        }
        info = self._prom_query("birdnode_info")
        labels = (info or {}).get("metric", {})
        snap["boot_reason"] = labels.get("boot_reason", "")
        snap["prev_reboot"] = labels.get("prev_reboot", "")
        return snap

    def node_night_sleep(self):
        """The node's own night-sleep TOGGLE (not the slept bracket) straight
        from /api/status, or None when the node is in a radio-off window. Only
        used to police the invariant, so a failure to answer is not an error."""
        st = self.node_reachable_status()
        if st is None:
            return None, None
        return st.get("night_sleep"), st

    # ------------------------------------------------------------- analysis

    def _prom_range(self, query, start, end, step=300):
        """query_range -> [(ts, value), ...] for the first result series."""
        url = (self.cfg["prometheus_url"].rstrip("/") +
               "/api/v1/query_range?query=" + urllib.parse.quote(query) +
               "&start=%.0f&end=%.0f&step=%d" % (start, end, step))
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        result = data.get("data", {}).get("result", [])
        if not result:
            return []
        pts = []
        for ts, val in result[0].get("values", []):
            try:
                pts.append((float(ts), float(val)))
            except (TypeError, ValueError):
                pass
        return pts

    def _samples_prom(self, start, end, step=300):
        """Battery + dump-health samples over [start, end], one dict per
        timestamp. `ok` is unknown per sample in this source, so delivered
        bytes include failed attempts (documented approximation)."""
        fields = {
            "mv": "birdnode_battery_millivolts",
            "pct": "birdnode_battery_soc_percent",
            "vbus": "birdnode_battery_vbus",
            "chg": "birdnode_battery_charging",
            "restart": "birdnode_restart_count",
            "dropped": "birdnode_dropped_frames_total",
            "duration": "birdnode_last_dump_duration_seconds",
            "interval": "birdnode_dump_interval_seconds",
            "bytes": "birdnode_last_dump_bytes",
            "rssi": "birdnode_rssi_dbm",
        }
        series = {k: dict(self._prom_range(q, start, end, step))
                  for k, q in fields.items()}
        out = []
        for ts, mv in series["mv"].items():
            s = {"ts": ts, "mv": mv}
            for k in fields:
                if k != "mv":
                    s[k] = series[k].get(ts)
            out.append(s)
        out.sort(key=lambda s: s["ts"])
        return out

    def _jsonl_range(self, start, end):
        """Dump records with start <= ts <= end (day files are UTC named)."""
        import datetime
        recs = []
        day = datetime.datetime.fromtimestamp(start, datetime.timezone.utc).date()
        last = datetime.datetime.fromtimestamp(end, datetime.timezone.utc).date()
        while day <= last:
            path = os.path.join(self.cfg["jsonl_dir"],
                                "node-telemetry-%s.jsonl" % day.isoformat())
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        ts = rec.get("ts")
                        if (isinstance(ts, (int, float)) and start <= ts <= end
                                and rec.get("fw_version")):
                            recs.append(rec)
            except OSError:
                pass
            day += datetime.timedelta(days=1)
        recs.sort(key=lambda r: r["ts"])
        return recs

    def _samples_jsonl(self, start, end):
        return [{
            "ts": r["ts"], "mv": r.get("batt_mv"), "pct": r.get("batt_pct"),
            "vbus": r.get("batt_vbus"), "chg": r.get("batt_chg"),
            "restart": r.get("restart_count"), "dropped": r.get("dropped_frames"),
            "duration": r.get("duration_s"), "interval": r.get("dump_int_s"),
            "bytes": r.get("dump_bytes"), "rssi": r.get("rssi_dbm"),
            "ok": r.get("dump_ok"),
        } for r in self._jsonl_range(start, end)]

    @staticmethod
    def _window_health(spans):
        """Dump health over the measured discharge spans only, so every rate
        shares the slope's denominator."""
        in_span = [s for sp in spans for s in sp]
        hours = sum((sp[-1]["ts"] - sp[0]["ts"]) / 3600.0 for sp in spans)
        durs = [s["duration"] for s in in_span if isinstance(s.get("duration"), (int, float))]
        itvs = [s["interval"] for s in in_span if isinstance(s.get("interval"), (int, float))]
        out = {
            "duty_cycle": (round(_median(durs) / _median(itvs), 4)
                           if durs and itvs and _median(itvs) else None),
            "radio_hours": round(sum(durs) / 3600.0, 2) if durs else None,
            "rssi_dbm_avg": _median([s["rssi"] for s in in_span
                                     if s.get("rssi") is not None]),
        }
        drops, prev = 0, None
        for s in in_span:
            if (prev is not None and s.get("dropped") is not None
                    and prev.get("dropped") is not None
                    and s["dropped"] >= prev["dropped"]):
                drops += s["dropped"] - prev["dropped"]
            prev = s
        out["dropped_frames"] = drops
        out["drops_per_hour"] = round(drops / hours, 1) if hours else None
        boots = [s.get("restart") for s in in_span if s.get("restart") is not None]
        out["restarts"] = len(set(boots)) - 1 if boots else 0
        if hours:
            delivered = sum(s["bytes"] for s in in_span
                            if s.get("bytes") and s.get("ok") in (True, None))
            out["delivered_mb"] = round(delivered / 1e6, 1)
            out["mb_per_hour"] = round(delivered / 1e6 / hours, 2)
        return out

    def analyze_phase(self, idx, start, end, run_s, completed_by):
        """Per-phase report over [start, end] with the validated estimator,
        plus the coverage gate: a phase whose measured spans cover less than
        min_coverage_ratio of its own accrued battery time is reported invalid
        and counted as discarded, so a phase can never 'complete' on thin
        data the way the first schedule's phases did."""
        phase = self.phases[idx]
        name = phase["name"]
        battery_hours = run_s / 3600.0
        min_span_h = float(self.cfg.get("min_span_h", 2.0))
        report = {
            "phase": name,
            "config": phase.get("set", {}),
            "start": start, "end": end, "completed_by": completed_by,
            "battery_hours": round(battery_hours, 2),
            "source": self.cfg.get("telemetry_source", "jsonl"),
        }
        try:
            samples = (self._samples_prom(start, end)
                       if report["source"] == "prometheus"
                       else self._samples_jsonl(start, end))
            spans = _spans(discharge_samples(samples))
            fields, _ = analyze_samples(spans)
            report.update(fields)
            report.update(self._window_health(spans))
            report["coverage_ratio"] = (round(fields["span_hours"] / battery_hours, 3)
                                        if battery_hours > 0 else 0.0)
            report["valid"] = bool(
                battery_hours >= min_span_h
                and report["coverage_ratio"] >= float(self.cfg.get("min_coverage_ratio", 0.6))
                and fields["span_hours"] >= min_span_h)
        except Exception:
            log.exception("phase %s analysis failed", name)
            report["valid"] = False
        if report.get("valid"):
            self._bump("phases_valid", name)
        elif battery_hours >= min_span_h:
            self._bump("phases_discarded", name)
        with self.lock:
            self.state.setdefault("reports", {})[name] = report
            if report.get("valid"):
                self.state["last_valid_phase"] = name
            self.save()
        log.info("phase %s report: %s", name, report)
        self._report_notify(report)

    def _bump(self, key, label):
        with self.lock:
            d = self.state.setdefault(key, {})
            d[label] = d.get(label, 0) + 1

    def _prom_value_at(self, query, at):
        url = (self.cfg["prometheus_url"].rstrip("/") +
               "/api/v1/query?query=" + urllib.parse.quote(query) +
               "&time=%.0f" % at)
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        result = data.get("data", {}).get("result", [])
        if not result:
            return None
        try:
            return float(result[0]["value"][1])
        except (TypeError, KeyError, IndexError, ValueError):
            return None

    def _report_notify(self, report):
        """Actionable phase report: what it measured, whether it counts, how it
        compares to the reference, and the single next action."""
        name = report["phase"]
        mv = report.get("slope_mv_per_hour")
        ref_name, ref = None, None
        for candidate in (self.state.get("last_valid_phase"), "reference", "baseline"):
            if candidate and candidate != name and candidate in self.state.get("reports", {}):
                ref_name, ref = candidate, self.state["reports"][candidate]
                break
        lines = []
        if not report.get("valid"):
            lines.append("NO USABLE DATA: %s of %.1f accrued battery-hours were "
                         "measurable (gate %.0f%%)." % (
                             (_f(100.0 * report.get("coverage_ratio", 0.0), "%.0f%%")
                              if report.get("coverage_ratio") is not None else "n/a"),
                             report.get("battery_hours", 0.0),
                             100.0 * float(self.cfg.get("min_coverage_ratio", 0.6))))
            lines.append("cycles seen: %s | span-hours: %s" % (
                report.get("cycle_count", "n/a"), report.get("span_hours", "n/a")))
            lines.append("DO: leave the node unplugged for the whole block — only "
                         "discharge time on battery counts, and USB/charging pauses it.")
            verdict = "invalid"
            title = "powerbench: %s measured NOTHING (node on USB?)" % name
        else:
            lines.append("discharge %s mV/h over %s cycle(s) | %s battery-hours "
                         "(%s covered)%s" % (
                             _f(mv), report.get("valid_cycles", 0),
                             _f(report.get("span_hours"), "%.1f"),
                             _f(100.0 * report.get("coverage_ratio", 0.0), "%.0f%%"),
                             "" if report.get("completed_by") == "complete"
                             else " [%s]" % report.get("completed_by")))
            disc = [c for c in (report.get("cycles") or []) if c.get("mv_per_hour") is not None]
            if disc:
                lines.append("per cycle: %s" % ", ".join(
                    "%s %s" % (c["day"][5:], _f(c["mv_per_hour"])) for c in disc))
            lines.append("duty %s | drops %s/h | restarts %s | rssi %s dBm" % (
                _f(100.0 * report["duty_cycle"], "%.1f%%")
                if report.get("duty_cycle") is not None else "n/a",
                _f(report.get("drops_per_hour"), "%.0f"),
                _f(report.get("restarts"), "%.0f"),
                _f(report.get("rssi_dbm_avg"), "%.0f")))
            verdict = None
            if (mv is not None and mv < 0 and ref
                    and ref.get("slope_mv_per_hour") and ref["slope_mv_per_hour"] < 0):
                delta = (abs(mv) / abs(ref["slope_mv_per_hour"]) - 1.0) * 100.0
                noise = _overlap(report.get("slope_mv_iqr"), ref.get("slope_mv_iqr"))
                verdict = "%+.0f%% vs %s%s" % (
                    delta, ref_name, " (within cycle noise)" if noise else "")
                lines.append("vs %s: %s" % (ref_name, verdict))
                if noise:
                    lines.append("DO: not separated from noise yet — run one more block "
                                 "before deciding.")
                elif delta < 0:
                    lines.append("DO: %s looks like a real win and it is FLATTER — adopt "
                                 "it as the default, then advance to the next knob." % name)
                else:
                    lines.append("DO: %s is worse — revert it (it is already in the "
                                 "baseline) and pick a different knob or stop." % name)
            title = "powerbench: %s done%s" % (name, " — %s" % verdict if verdict else "")
        lines.append("NEXT: python3 /app/powerbench.py skip | pause | status")
        self._notify(title, "\n".join(lines),
                     "3" if report.get("valid") else "4",
                     click=self.cfg.get("grafana_url"))
        self._annotate("phase %s %s: %s" % (name, report.get("completed_by"),
                                            verdict or "report ready"),
                       ["powerbench", name, "report"])

    def latest_record(self):
        """Newest JSONL record across today+yesterday (UTC rollover)."""
        import datetime
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        paths = []
        for delta in (1, 0):
            day = (now_utc - datetime.timedelta(days=delta)).strftime("%Y-%m-%d")
            paths.append(os.path.join(self.cfg["jsonl_dir"],
                                      "node-telemetry-%s.jsonl" % day))
        for path in reversed(paths):
            try:
                with open(path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - 65536))
                    tail = fh.read().decode("utf-8", "replace")
                for line in reversed(tail.strip().splitlines()):
                    try:
                        return json.loads(line)
                    except ValueError:
                        continue
            except OSError:
                continue
        return None

    # ------------------------------------------------------------ transitions

    def _capture_baseline(self, keys):
        st = self.node_reachable_status()
        for key in keys:
            if key in self.state["baseline"]:
                continue
            val = None
            if st is not None:
                val = st.get(key)
            if val is None:
                val = DEFAULT_BASELINE.get(key)
                log.warning("baseline for %s not readable from node; using default %s", key, val)
            self.state["baseline"][key] = str(val)

    def _notify(self, title, body, priority="3", click=None):
        token = os.environ.get("NTFY_TOKEN")
        if not token:
            token_path = os.path.join(self.state_dir, "ntfy-token")
            try:
                with open(token_path, "r", encoding="utf-8") as fh:
                    token = fh.read().strip()
            except OSError:
                log.info("no ntfy token (env or %s); skipping notification", token_path)
                return
        url = "%s/%s" % (self.cfg["ntfy_url"].rstrip("/"), self.cfg["ntfy_topic"])
        headers = {"Authorization": "Bearer " + token,
                   "Title": title, "Priority": priority, "Tags": "bird,microchip"}
        if click:
            headers["Click"] = click
        req = urllib.request.Request(url, data=body.encode(), headers=headers)
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:
            log.warning("ntfy post failed: %s", exc)

    def _annotate(self, text, tags):
        token = os.environ.get("GRAFANA_TOKEN")
        if not token:
            token_path = os.path.join(self.state_dir, "grafana-token")
            try:
                with open(token_path, "r", encoding="utf-8") as fh:
                    token = fh.read().strip()
            except OSError:
                token = None
        user = os.environ.get("GRAFANA_USER") or self.cfg.get("grafana_user")
        password = os.environ.get("GRAFANA_PASSWORD") or self.cfg.get("grafana_password")
        if not token and not (user and password):
            return
        payload = json.dumps({"text": text, "tags": tags}).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        else:
            import base64
            headers["Authorization"] = "Basic " + base64.b64encode(
                ("%s:%s" % (user, password)).encode()).decode()
        req = urllib.request.Request(self.cfg["grafana_url"].rstrip("/") + "/api/annotations",
                                     data=payload, headers=headers)
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as exc:
            log.warning("grafana annotation failed: %s", exc)

    def _enter_phase(self, idx):
        """Apply phase idx's config and reset per-phase accounting. Analyzes
        the outgoing phase first (report -> state/metrics + ntfy + Grafana)."""
        st = self.state
        prev_idx = st["phase_index"]
        if idx != prev_idx and prev_idx < len(self.phases) and st.get("phase_start_epoch"):
            self.analyze_phase(prev_idx, st["phase_start_epoch"], time.time(),
                               st.get("phase_run_s", 0.0), "complete")
        if idx >= len(self.phases):
            # Schedule complete: mark done; tick() applies baseline + notifies.
            st["phase_index"] = idx
            st["last_transition_epoch"] = time.time()
            self.save()
            return
        phase = self.phases[idx]
        name = phase["name"]
        bad = sorted(FORBIDDEN_SET_KEYS & set(phase.get("set", {}).keys()))
        if bad:
            self._abort("phase %s sets invariant key(s) %s -- night sleep must stay "
                        "ON (shipping default, and the only knob that removes whole "
                        "hours of radio time)" % (name, bad))
            return
        log.info("entering phase %d (%s)", idx, name)
        if phase.get("set"):
            self._capture_baseline(phase["set"].keys())
        ok = self.apply_config(phase.get("set", {}), self.cfg["apply_timeout_s"])
        st = self.state
        rec = self.telemetry_snapshot() or {}
        st.update({
            "phase_index": idx,
            "phase_start_epoch": time.time(),
            "phase_run_s": 0.0,
            "paused_reason": None,
            "applied": dict(phase.get("set", {})),
            "phase_start_restart_count": rec.get("restart_count"),
            "phase_start_dropped_frames": rec.get("dropped_frames"),
            "guard_tripped": None,
            "last_transition_epoch": time.time(),
        })
        self.save()
        if not ok:
            self._abort("apply timeout entering phase %s" % name)
            return
        self._notify("powerbench: phase %s" % name,
                     "Entered phase %d (%s), set=%s" % (idx, name, phase.get("set", {})))
        self._annotate("powerbench phase: %s" % name, ["powerbench", name])

    def _abort(self, reason):
        """Revert to baseline and halt the schedule. Analyzes the interrupted
        phase first (partial data still informs the guard decision)."""
        log.error("ABORT: %s", reason)
        st = self.state
        idx = st["phase_index"]
        if idx < len(self.phases) and st.get("phase_start_epoch"):
            self.analyze_phase(idx, st["phase_start_epoch"], time.time(),
                               st.get("phase_run_s", 0.0), "aborted")
        self.apply_config(self.state["baseline"], self.cfg["apply_timeout_s"])
        st = self.state
        st["halted"] = True
        st["applied"] = {}
        self.save()
        self._notify("powerbench ABORT", reason + " — node reverted to baseline, schedule halted.", "5")
        self._annotate("powerbench ABORT: " + reason, ["powerbench", "abort"])

    def _trip_guard(self, guard, detail):
        self.guard_trips[guard] = self.guard_trips.get(guard, 0) + 1
        self.state["guard_tripped"] = guard
        self.save()
        self._abort("guard '%s' tripped: %s" % (guard, detail))

    # ------------------------------------------------------------------- tick

    def tick(self):
        st = self.state
        now = time.time()
        last = st.get("last_tick_epoch") or now
        st["last_tick_epoch"] = now

        self._consume_command()
        if st["halted"]:
            self.save()
            return
        if st["phase_index"] >= len(self.phases):
            # Schedule complete: ensure baseline applied once, then idle.
            if st["applied"]:
                self._enter_baseline_idle()
            self.save()
            return

        phase = self.phase
        rec = self.telemetry_snapshot()

        # Pause accounting: only battery-discharge time counts toward a phase.
        # Night sleep is an invariant: if the node reports it off, the night is
        # spent awake and the phase is measuring the wrong thing entirely, so
        # the clock stops and the operator is told once (checked hourly -- the
        # node only answers /api/status inside a radio-up window).
        if now - (st.get("night_sleep_checked_epoch") or 0) > 3600:
            toggle, status = self.node_night_sleep()
            if toggle is not None:
                st["night_sleep_checked_epoch"] = now
                off = not bool(toggle)
                if off and not st.get("night_sleep_off"):
                    st["night_sleep_off"] = True
                    log.error("night sleep is OFF on the node; pausing")
                    code = (status or {}).get("deep_sleep_status_code") \
                        if isinstance(status, dict) else None
                    self._notify(
                        "powerbench PAUSED: node night sleep is OFF",
                        "The node reports night_sleep=0%s, so it stays awake all "
                        "night and this block measures the wrong thing.\n"
                        "DO: turn night sleep back on (WebUI -> Night Sleep card, or "
                        "POST /api/set key=night_sleep value=1), then let the clock "
                        "resume (python3 /app/powerbench.py resume)."
                        % (" (status: %s)" % code if code else ""),
                        "4", click=self.cfg.get("grafana_url"))
                elif not off and st.get("night_sleep_off"):
                    st["night_sleep_off"] = False
                    if st.get("paused_reason") == "night_sleep_off":
                        st["paused_reason"] = None
                    log.info("night sleep back ON; resuming accrual")

        pause = None
        if st["paused_reason"] == "manual":
            pause = "manual"
        elif st.get("night_sleep_off"):
            pause = "night_sleep_off"
        elif rec is None or (time.time() - float(rec.get("ts", 0))) > 900:
            # No fresh telemetry: node state unknown, do not accrue battery time.
            pause = "no_telemetry"
        elif rec.get("batt_vbus") == 1 or rec.get("batt_chg") == 1:
            pause = "charging"
        elif rec.get("batt_pct") is not None and 0 <= rec.get("batt_pct", 0) < self.cfg["battery_floor_pct"]:
            pause = "low_batt"
        if pause != st["paused_reason"]:
            log.info("pause state: %s -> %s", st["paused_reason"], pause)
            st["paused_reason"] = pause
        if pause is None:
            st["phase_run_s"] += max(0.0, now - last)

        # Guards.
        if rec is not None and not st["halted"]:
            guards = phase.get("guards", [])
            if "crash" in guards:
                rc0 = st.get("phase_start_restart_count")
                rc = rec.get("restart_count")
                boot = str(rec.get("boot_reason", ""))
                prev = str(rec.get("prev_reboot", ""))
                if rc0 is not None and rc is not None and rc > rc0 and (
                        "interrupt_wdt" in boot or "interrupt_wdt" in prev):
                    self._trip_guard("crash", "restart_count %s->%s boot_reason=%s prev_reboot=%s"
                                     % (rc0, rc, boot, prev))
                    return
            if "drops" in guards:
                d0 = st.get("phase_start_dropped_frames")
                d = rec.get("dropped_frames")
                if d0 is not None and d is not None and d - d0 > self.cfg["drop_guard_frames"]:
                    self._trip_guard("drops", "dropped_frames +%d (limit %d)"
                                     % (d - d0, self.cfg["drop_guard_frames"]))
                    return

        # Phase expiry.
        duration_s = phase["duration_h"] * 3600.0
        if st["phase_run_s"] >= duration_s:
            log.info("phase %s complete (%.1f battery-hours)", phase["name"], st["phase_run_s"] / 3600.0)
            self._bump("phases_completed", phase["name"])
            self._enter_phase(st["phase_index"] + 1)
            return

        self.save()

    def _enter_baseline_idle(self):
        log.info("schedule complete; reverting to baseline and idling")
        self.apply_config(self.state["baseline"], self.cfg["apply_timeout_s"])
        st = self.state
        st["applied"] = {}
        st["last_transition_epoch"] = time.time()
        self.save()
        self._notify("powerbench: schedule complete",
                     "All phases done; node reverted to baseline.")
        self._annotate("powerbench: schedule complete", ["powerbench", "done"])

    # --------------------------------------------------------------- control

    def _consume_command(self):
        cmd = _load_json(self.control_path, default=None)
        if not cmd:
            return
        try:
            os.remove(self.control_path)
        except OSError:
            pass
        name = cmd.get("command")
        log.info("control command: %s", name)
        if name == "pause":
            self.state["paused_reason"] = "manual"
        elif name == "resume":
            self.state["paused_reason"] = None
        elif name == "abort":
            self._abort("manual abort")
        elif name == "skip":
            if self.phase is not None:
                self._enter_phase(self.state["phase_index"] + 1)
        elif name == "reset":
            # Start the current schedule from phase 0 with fresh accounting. The
            # running process owns state.json, so this is the only safe way to
            # clear it (deleting the file races the next tick's save).
            log.warning("RESET: state cleared, restarting the schedule at phase 0")
            archived = os.path.join(self.state_dir,
                                    "state.pre-reset-%d.json" % int(time.time()))
            try:
                _save_json(archived, self.state)
            except OSError as exc:
                log.warning("could not archive state before reset: %s", exc)
            old = dict(self.state)
            self.state = self._fresh_state()
            self.state["baseline"] = old.get("baseline", {})
            self._notify("powerbench RESET",
                         "Schedule restarted at phase 0; previous state archived on "
                         "the PVC. Baseline kept: %s" % (old.get("baseline") or {}), "3",
                         click=self.cfg.get("grafana_url"))
            self._enter_phase(0)

    # --------------------------------------------------------------- metrics

    def render_metrics(self):
        with self.lock:
            st = dict(self.state)
            trips = dict(self.guard_trips)
            fails = self.apply_failures
        phase = self.phase
        name = phase["name"] if phase else "done"
        idx = st["phase_index"]
        import hashlib
        cfg_hash = hashlib.sha1(json.dumps(
            phase.get("set", {}) if phase else {}, sort_keys=True).encode()).hexdigest()[:8]
        out = []
        out.append("# HELP powerbench_phase_info Current experiment phase.")
        out.append("# TYPE powerbench_phase_info gauge")
        out.append('powerbench_phase_info{phase="%s",index="%d",config_hash="%s"} 1'
                   % (name, idx, cfg_hash))
        def g(n, h, v):
            if v is None:
                return
            out.append("# HELP %s %s" % (n, h))
            out.append("# TYPE %s gauge" % n)
            out.append("%s %s" % (n, v))
        g("powerbench_phase_start_timestamp_seconds", "Wall clock when the current phase began.",
          st.get("phase_start_epoch"))
        g("powerbench_phase_duration_seconds", "Battery-time budget of the current phase.",
          (phase["duration_h"] * 3600) if phase else 0)
        g("powerbench_phase_run_seconds", "Battery-discharge time accumulated in the current phase.",
          round(st.get("phase_run_s", 0), 1))
        paused = st.get("paused_reason")
        out.append("# HELP powerbench_paused Phase clock paused (1) with reason label.")
        out.append("# TYPE powerbench_paused gauge")
        for reason in ("charging", "low_batt", "manual", "no_telemetry",
                       "night_sleep_off"):
            out.append('powerbench_paused{reason="%s"} %d' % (reason, 1 if paused == reason else 0))
        g("powerbench_night_sleep_off",
          "Node night sleep reported OFF (invariant violated, clock paused).",
          1 if st.get("night_sleep_off") else 0)
        g("powerbench_phase_elapsed_wall_seconds",
          "Wall-clock time since the current phase began (battery time is run_seconds).",
          round(time.time() - st["phase_start_epoch"], 1)
          if st.get("phase_start_epoch") else None)
        out.append("# HELP powerbench_phase_completed_total Phases that reached their battery-hour budget.")
        out.append("# TYPE powerbench_phase_completed_total counter")
        out.append("# HELP powerbench_phase_valid_total Phase reports that passed the coverage gate.")
        out.append("# TYPE powerbench_phase_valid_total counter")
        out.append("# HELP powerbench_phase_discarded_total Phase reports discarded by the coverage gate (too little measurable discharge time).")
        out.append("# TYPE powerbench_phase_discarded_total counter")
        for key, metric in (("phases_completed", "powerbench_phase_completed_total"),
                            ("phases_valid", "powerbench_phase_valid_total"),
                            ("phases_discarded", "powerbench_phase_discarded_total")):
            for phase_name, count in sorted((st.get(key) or {}).items()):
                out.append('%s{phase="%s"} %d' % (metric, phase_name, count))
        g("powerbench_halted", "Schedule halted (abort or complete).",
          1 if st.get("halted") or phase is None else 0)
        g("powerbench_last_transition_timestamp_seconds", "Wall clock of the last phase transition.",
          st.get("last_transition_epoch"))
        out.append("# HELP powerbench_guard_tripped_total Guard trips by guard name.")
        out.append("# TYPE powerbench_guard_tripped_total counter")
        for guard, count in trips.items():
            out.append('powerbench_guard_tripped_total{guard="%s"} %d' % (guard, count))
        out.append("# HELP powerbench_apply_failures_total Config apply timeouts.")
        out.append("# TYPE powerbench_apply_failures_total counter")
        out.append("powerbench_apply_failures_total %d" % fails)
        # Per-phase analysis reports (computed at phase exit).
        report_metrics = [
            ("slope_mv_per_hour", "powerbench_phase_report_slope_mv_per_hour",
             "Battery discharge slope during the phase (mV/h, discharge-only samples, negative = draining)."),
            ("slope_pct_per_hour", "powerbench_phase_report_slope_pct_per_hour",
             "Battery discharge slope during the phase (SOC %/h)."),
            ("duty_cycle", "powerbench_phase_report_duty_cycle",
             "Radio duty cycle (dump window / interval) during the phase."),
            ("dropped_frames", "powerbench_phase_report_dropped_frames",
             "Dropped audio frames during the phase."),
            ("restarts", "powerbench_phase_report_restarts",
             "Node reboots during the phase."),
            ("rssi_dbm_avg", "powerbench_phase_report_rssi_dbm_avg",
             "Mean WiFi RSSI during the phase (confound check)."),
            ("battery_hours", "powerbench_phase_report_battery_hours",
             "Battery-discharge hours the phase accumulated."),
            ("coverage_ratio", "powerbench_phase_report_coverage_ratio",
             "Measured discharge-span hours / accrued battery hours (coverage gate input)."),
            ("span_hours", "powerbench_phase_report_span_hours",
             "Discharge hours actually measurable inside contiguous spans."),
            ("cycle_count", "powerbench_phase_report_cycles",
             "Battery cycles (local discharge days) the phase saw."),
            ("valid", "powerbench_phase_report_valid",
             "1 when the phase passed the coverage gate (0 = discarded)."),
        ]
        reports = st.get("reports", {})
        for key, metric, help_ in report_metrics:
            out.append("# HELP %s %s" % (metric, help_))
            out.append("# TYPE %s gauge" % metric)
            for phase_name, rep in sorted(reports.items()):
                val = rep.get(key)
                if val is None:
                    continue
                out.append('%s{phase="%s"} %s' % (metric, phase_name, val))
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------- run

    def run(self):
        port = self.cfg["metrics_port"]
        ctrl = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.rstrip("/") in ("", "/metrics"):
                    body = ctrl.render_metrics().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; version=0.0.4")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("metrics on :%d/metrics", port)

        # Boot: if never transitioned, enter phase 0.
        if self.state["phase_start_epoch"] is None and not self.state["halted"]:
            self._enter_phase(0)

        tick_s = self.cfg["tick_s"]
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            time.sleep(tick_s)


def _write_control(state_dir, command):
    path = os.path.join(state_dir, "control.json")
    if os.path.exists(path):
        pending = _load_json(path, default={})
        print("refusing: command '%s' is still pending (daemon consumes one command per tick)"
              % pending.get("command", "?"))
        return 1
    _save_json(path, {"command": command, "ts": time.time()})
    print("queued command: %s (daemon applies within one tick)" % command)
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] not in ("run", "status", "pause", "resume", "abort",
                                       "skip", "reset"):
        print(__doc__)
        return 2
    mode = argv[1]
    cfg_dir = os.environ.get("POWERBENCH_CONF_DIR",
                             os.path.join(os.path.dirname(os.path.abspath(__file__))))
    cfg = _load_json(os.path.join(cfg_dir, "powerbench.json"))
    experiments = _load_json(os.path.join(cfg_dir, "experiments.json"))
    state_dir = cfg.get("state_dir", DEFAULT_STATE_DIR)
    os.makedirs(state_dir, exist_ok=True)

    if mode in ("pause", "resume", "abort", "skip", "reset"):
        return _write_control(state_dir, mode)
    if mode == "status":
        st = _load_json(os.path.join(state_dir, "state.json"), default={})
        phase_idx = st.get("phase_index", 0)
        name = (experiments["phases"][phase_idx]["name"]
                if phase_idx < len(experiments["phases"]) else "done")
        run_h = st.get("phase_run_s", 0) / 3600.0
        dur_h = experiments["phases"][phase_idx]["duration_h"] if phase_idx < len(experiments["phases"]) else 0
        print("phase:      %d (%s)" % (phase_idx, name))
        print("progress:   %.1f / %.1f battery-hours" % (run_h, dur_h))
        print("paused:     %s" % (st.get("paused_reason") or "no"))
        print("halted:     %s" % bool(st.get("halted")))
        print("applied:    %s" % (st.get("applied") or "{}"))
        print("baseline:   %s" % (st.get("baseline") or "{}"))
        print("guard trip: %s" % (st.get("guard_tripped") or "none"))
        print("night sleep off: %s" % bool(st.get("night_sleep_off")))
        for pname, rep in sorted((st.get("reports") or {}).items()):
            print("report %-10s %s valid=%s coverage=%s cycles=%s span_h=%s" % (
                pname,
                ("%s mV/h" % _f(rep.get("slope_mv_per_hour"))) if rep.get("slope_mv_per_hour") is not None else "-",
                rep.get("valid"), rep.get("coverage_ratio"),
                rep.get("cycle_count"), rep.get("span_hours")))
        return 0

    Controller(cfg, experiments, state_dir).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
