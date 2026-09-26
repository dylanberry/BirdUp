#!/usr/bin/env python3
"""powerbench digest — experiment summary to ntfy, only when it is actionable.

Runs as a CronJob (in-cluster). Reads the controller's per-phase report metrics
(computed at each phase exit with the validated estimator) plus current-phase
progress from Prometheus, formats a table with a verdict, and posts it to ntfy.

Actionability rule (2026-09-26): the digest is SILENT unless one of these is
true, because a daily table that says "nothing changed" is noise:

  * a phase completed in the last 24 h (there is a verdict to read), or
  * the clock is paused for a reason only a human can clear (low_batt /
    night_sleep_off / charging for >12 h), or
  * a phase was discarded by the coverage gate, or
  * an explicit --force (used when testing this script).

Every posted digest ends with the reference comparison and ONE next action.
Stdlib only.

Env:
  PROMETHEUS_URL  (default http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090)
  NTFY_URL        (default http://ntfy.ntfy.svc.cluster.local)
  NTFY_TOPIC      (default bird-up)
  NTFY_TOKEN      (required to post; absent = print only)
  DASHBOARD_URL   (default the Tailscale Grafana ingress)
"""

import json
import os
import sys
import urllib.parse
import urllib.request

PROM = os.environ.get(
    "PROMETHEUS_URL",
    "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090")
NTFY_URL = os.environ.get("NTFY_URL", "http://ntfy.ntfy.svc.cluster.local")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "bird-up")
TOKEN = os.environ.get("NTFY_TOKEN", "")
DASHBOARD = os.environ.get("DASHBOARD_URL", "https://grafana.tail404e6.ts.net")

REPORTS = [
    ("battery_hours", "powerbench_phase_report_battery_hours", "%.1f"),
    ("slope_mv", "powerbench_phase_report_slope_mv_per_hour", "%.1f"),
    ("slope_pct", "powerbench_phase_report_slope_pct_per_hour", "%.2f"),
    ("duty", "powerbench_phase_report_duty_cycle", None),      # -> %
    ("coverage", "powerbench_phase_report_coverage_ratio", None),  # -> %
    ("cycles", "powerbench_phase_report_cycles", "%.0f"),
    ("valid", "powerbench_phase_report_valid", "%.0f"),
    ("drops", "powerbench_phase_report_dropped_frames", "%.0f"),
    ("restarts", "powerbench_phase_report_restarts", "%.0f"),
    ("rssi", "powerbench_phase_report_rssi_dbm_avg", "%.0f"),
]


def query(q):
    url = PROM.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote(q)
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return data.get("data", {}).get("result", [])


def value(q):
    res = query(q)
    try:
        return float(res[0]["value"][1])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def f(v, spec="%.1f", suffix=""):
    return "n/a" if v is None else (spec % v) + suffix


def rows_by_phase():
    rows = {}
    for key, metric, _fmt in REPORTS:
        for series in query(metric):
            ph = series["metric"].get("phase", "?")
            try:
                rows.setdefault(ph, {})[key] = float(series["value"][1])
            except (KeyError, TypeError, ValueError):
                pass
    return rows


def fmt_table(rows):
    lines = ["%-11s %6s %8s %8s %6s %6s %5s %7s %6s" % (
        "phase", "batt-h", "mV/h", "%/h", "duty", "covg", "cyc", "drops", "ok")]
    for ph in sorted(rows):
        r = rows[ph]
        lines.append("%-11s %6s %8s %8s %6s %6s %5s %7s %6s" % (
            ph, f(r.get("battery_hours")),
            f(r.get("slope_mv"), "%+.1f"),
            f(r.get("slope_pct"), "%+.2f"),
            f(100.0 * r["duty"], "%.0f", "%") if r.get("duty") is not None else "n/a",
            f(100.0 * r["coverage"], "%.0f", "%") if r.get("coverage") is not None else "n/a",
            f(r.get("cycles"), "%.0f"),
            f(r.get("drops"), "%.0f"),
            f(r.get("valid"), "%.0f")))
    return lines


def verdict(rows):
    """Compare the newest valid non-reference phase against the reference."""
    ref = rows.get("reference") or rows.get("baseline")
    cand = [ph for ph in rows
            if ph not in ("reference", "baseline") and rows[ph].get("valid")]
    if not ref or not cand:
        return "reference block only so far — no verdict yet", \
            "DO: let the reference block finish (~3 cycles), then the next phase runs itself."
    ph = sorted(cand)[-1]
    r = rows[ph]
    a, b = r.get("slope_mv"), ref.get("slope_mv")
    if a is None or b is None or b >= 0 or a >= 0:
        return "%s: no usable slope comparison" % ph, "DO: check coverage in the table above."
    delta = (abs(a) / abs(b) - 1.0) * 100.0
    if abs(delta) < 8.0:
        return ("%s: %.1f mV/h vs reference %.1f mV/h — %+.0f%% (within the noise band)"
                % (ph, a, b, delta),
                "DO: no decision yet — this knob does not change drain enough to act on.")
    if delta < 0:
        return ("%s: %.1f mV/h vs reference %.1f mV/h — %.0f%% BETTER"
                % (ph, a, b, abs(delta)),
                "DO: adopt %s as the default, then append the next knob to the schedule." % ph)
    return ("%s: %.1f mV/h vs reference %.1f mV/h — %.0f%% WORSE"
            % (ph, a, b, delta),
            "DO: do not adopt %s; stop tuning this slice or pick a different knob." % ph)


def main():
    force = "--force" in sys.argv
    phase = "?"
    info = query("powerbench_phase_info")
    if info:
        phase = info[0]["metric"].get("phase", "?")
    run = value("powerbench_phase_run_seconds") or 0.0
    dur = value("powerbench_phase_duration_seconds") or 0.0
    paused = query("powerbench_paused > 0")
    paused_reason = paused[0]["metric"].get("reason", "?") if paused else None
    coverage = value("powerbench_phase_report_coverage_ratio")
    night_off = value("powerbench_night_sleep_off")

    rows = rows_by_phase()
    header = "phase %s (%.1f / %.1f battery-h, paused: %s)" % (
        phase, run / 3600.0, dur / 3600.0, paused_reason or "no")
    body = [header, ""]
    body += fmt_table(rows) if rows else ["no completed-phase reports yet"]
    body.append("")
    line, action = verdict(rows)
    body.append(line)
    body.append(action)
    body.append("charts: %s" % DASHBOARD)

    # Actionability gate. Deliberately NOT gated on charging/low_batt: those
    # are steady states already covered by their own actionable rules
    # (PowerbenchNodePlugged / PowerbenchFloorReached), and repeating them in a
    # daily digest would just be noise.
    completed_24h = value("increase(powerbench_phase_completed_total[24h])") or 0.0
    discarded_24h = value("increase(powerbench_phase_discarded_total[24h])") or 0.0
    why = None
    if completed_24h > 0:
        why = "a phase completed in the last 24 h"
    elif discarded_24h > 0:
        why = "a phase was discarded by the coverage gate"
    elif night_off:
        why = "night sleep is OFF on the node (clock paused)"

    print("\n".join(body))
    if not (force or why):
        print("\n(no actionable change; not posting)")
        return 0
    if not TOKEN:
        print("\nNTFY_TOKEN not set; not posting")
        return 0
    print("\nposting: %s" % (why or "forced"))
    req = urllib.request.Request(
        "%s/%s" % (NTFY_URL.rstrip("/"), NTFY_TOPIC),
        data=("\n".join(body)).encode(),
        headers={"Authorization": "Bearer " + TOKEN,
                 "Title": "powerbench digest: %s" % (line.split(" — ")[0][:80]),
                 "Priority": "3",
                 "Click": DASHBOARD,
                 "Tags": "bird,chart_with_downwards_trend"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        print("ntfy post: HTTP %d" % resp.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
