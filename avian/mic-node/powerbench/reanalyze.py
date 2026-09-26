#!/usr/bin/env python3
"""powerbench offline re-analysis (option A).

The live controller's per-phase report fit a discharge slope over a sparse,
non-contiguous subset of each phase's *wall* window and bucketed by phase NAME.
Phases were cumulative (each `set` applied on top of the previous), spanned
firmware updates, and mixed USB (eco_effective=0) with battery+ECO records, so
the reported slopes are not usable as measurements.

This script ignores phase names and buckets by the CONFIG GROUND TRUTH carried
in each dump header (dump_int_s / tx_dbm / cpu_mhz / fw_version / eco_effective),
which the node reports itself. For each bucket it computes:

  - battery discharge slope, mV/h and %SoC/h, from hourly medians (Theil-Sen)
  - radio duty cycle (dump window / interval)
  - delivered throughput (MB/h) and energy cost per GB delivered
  - dump health: ok %, failed %, throughput-crawl %

Every sample is taken at the same point of the duty cycle (end of a dump), so
the systematic IR-drop offset cancels in the slope.

Usage:
  reanalyze.py [--dir DIR] [--json] [--quiet]
Stdlib only.
"""

import argparse
import collections
import datetime
import glob
import json
import os
import statistics
import sys

# Phase windows (wall clock) from the controller state.json reports, for the
# one thing header labels cannot show: the cpu80 phase (cpu_mhz is always
# reported as 160 at dump time -- v1.54 moves the up-switch to post-association).
PHASE_WINDOWS = [
    ("baseline",  "2026-08-24 09:28", "2026-09-04 20:01"),
    ("cpu80",     "2026-09-04 20:05", "2026-09-06 07:27"),
    ("dump420",   "2026-09-06 07:40", "2026-09-08 06:17"),
    ("txpower85", "2026-09-08 06:24", "2026-09-19 05:37"),
]

CRAWL_KBPS = 40.0  # v1.70 default crawl threshold


def epoch(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


def load(path):
    recs = []
    for f in sorted(glob.glob(os.path.join(path, "*.jsonl"))):
        with open(f, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("ts") and d.get("fw_version"):
                    recs.append(d)
    recs.sort(key=lambda d: d["ts"])
    # boot id: uptime_s resets at each boot
    boot = 0
    prev_u = None
    for d in recs:
        u = d.get("uptime_s")
        if u is not None and prev_u is not None and u < prev_u:
            boot += 1
        d["_boot"] = boot
        if u is not None:
            prev_u = u
    return recs


def bucket_key(d):
    return (
        int(d.get("dump_int_s") or 0),
        d.get("tx_dbm"),
        d.get("fw_version"),
        int(d.get("eco_effective") or 0),
    )


def theil_sen(points):
    """points: [(x_hours, y)]. Returns median pairwise slope."""
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
    return statistics.median(slopes) if slopes else None


def hourly_medians(recs, field):
    """Median of `field` per UTC hour; only hours with >=3 samples.
    Returns [(hours_since_epoch, value)]."""
    byh = collections.defaultdict(list)
    for d in recs:
        v = d.get(field)
        if v is None:
            continue
        byh[d["ts"] // 3600].append(float(v))
    return [(h, statistics.median(v)) for h, v in sorted(byh.items()) if len(v) >= 3]


def contiguous_spans(recs, max_gap_s):
    """Split a record list into spans where consecutive samples are within
    max_gap_s and share the same boot."""
    spans = []
    cur = []
    for d in recs:
        if cur:
            p = cur[-1]
            if d["_boot"] != p["_boot"] or d["ts"] - p["ts"] > max_gap_s:
                spans.append(cur)
                cur = []
        cur.append(d)
    if cur:
        spans.append(cur)
    return spans


def quantile(vals, q):
    vals = sorted(vals)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    i = q * (len(vals) - 1)
    lo = int(i)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (i - lo)


def summarize(name, recs, span_hours_required=3.0):
    """All rates are normalised to DISCHARGE hours (the union of contiguous
    battery spans), never to wall time -- wall time includes plugged-in hours
    and would dilute every rate by 5-10x."""
    out = {"bucket": name, "records": len(recs)}
    if not recs:
        return out
    t0, t1 = recs[0]["ts"], recs[-1]["ts"]
    interval = int(recs[0].get("dump_int_s") or 300)
    out["first"] = datetime.datetime.fromtimestamp(t0).strftime("%m-%d %H:%M")
    out["last"] = datetime.datetime.fromtimestamp(t1).strftime("%m-%d %H:%M")
    out["wall_h"] = round((t1 - t0) / 3600.0, 2)
    out["interval_s"] = interval

    # Battery spans: on battery, not charging, same boot, samples no further
    # apart than 2 intervals. `spans` is the denominator for EVERY rate below
    # (MB/h, duty, drops/h) so numerator and denominator share one window;
    # `long_spans` (>=3 h) are used only for the discharge slope, where short
    # or noisy stretches would dominate.
    batt = [d for d in recs if not d.get("batt_vbus") and not d.get("batt_chg")]
    spans = [s for s in contiguous_spans(batt, interval * 2.0) if len(s) >= 2]
    long_spans = [s for s in spans
                  if (s[-1]["ts"] - s[0]["ts"]) / 3600.0 >= span_hours_required]
    out["spans"] = len(spans)
    out["long_spans"] = len(long_spans)
    out["span_hours"] = round(sum((s[-1]["ts"] - s[0]["ts"]) / 3600.0
                                 for s in spans), 2)
    in_span = [d for s in spans for d in s]
    mv_slopes, pct_slopes = [], []
    for s in long_spans:
        pts_mv = smoothed([((d["ts"] - s[0]["ts"]) / 3600.0, float(d["batt_mv"]))
                           for d in s if d.get("batt_mv")], 0.25)
        pts_pct = smoothed([((d["ts"] - s[0]["ts"]) / 3600.0, float(d["batt_pct"]))
                            for d in s if d.get("batt_pct") is not None], 0.25)
        mv_slopes.append(theil_sen(pts_mv))
        pct_slopes.append(theil_sen(pts_pct))
    out["mv_per_h"] = weighted_median([(s, 1) for s in mv_slopes if s is not None])
    out["pct_per_h"] = weighted_median([(s, 1) for s in pct_slopes if s is not None])
    out["mv_iqr"] = [round(quantile([s for s in mv_slopes if s is not None], q), 1)
                     for q in (0.25, 0.75)] if any(mv_slopes) else None
    out["pct_iqr"] = [round(quantile([s for s in pct_slopes if s is not None], q), 2)
                      for q in (0.25, 0.75)] if any(pct_slopes) else None

    # Radio duty RELATIVE TO BATTERY TIME: total radio-up seconds of every
    # dump attempt (successful or not) divided by battery hours.
    durs = [d["duration_s"] for d in in_span
            if isinstance(d.get("duration_s"), (int, float))]
    out["radio_s"] = round(sum(durs), 0)
    out["duty"] = (round(100.0 * sum(durs) / (out["span_hours"] * 3600.0), 1)
                   if out["span_hours"] else None)

    ok = [d for d in in_span if d.get("dump_ok")]
    out["ok_pct"] = round(100.0 * len(ok) / len(in_span), 1) if in_span else None
    out["crawl_pct"] = round(100.0 * len([d for d in in_span
                                         if not d.get("dump_ok")
                                         or rate_kbps(d) < CRAWL_KBPS])
                             / len(in_span), 1) if in_span else None
    rate = [rate_kbps(d) for d in ok]
    out["rate_kbps"] = round(statistics.median(rate), 1) if rate else None
    out["rssi"] = (round(statistics.median([d["rssi_dbm"] for d in in_span
                                            if d.get("rssi_dbm") is not None]), 1)
                   if any(d.get("rssi_dbm") is not None for d in in_span) else None)

    delivered = sum(d["dump_bytes"] for d in ok if d.get("dump_bytes"))
    out["delivered_mb"] = round(delivered / 1e6, 1)
    if out["span_hours"]:
        out["mb_per_h"] = round(delivered / 1e6 / out["span_hours"], 2)
        if out["pct_per_h"]:
            out["pct_per_gb"] = round(abs(out["pct_per_h"])
                                      / (out["mb_per_h"] / 1000.0), 2)
        if out["mv_per_h"]:
            out["mv_per_gb"] = round(abs(out["mv_per_h"])
                                     / (out["mb_per_h"] / 1000.0), 1)
    # drops accrued while discharging, per battery hour
    drops = 0
    for s in spans:
        prev = None
        for d in s:
            v = d.get("dropped_frames")
            if v is not None and prev is not None and v >= prev:
                drops += v - prev
            prev = v
    out["drops_per_h"] = (round(drops / out["span_hours"], 1)
                          if out["span_hours"] else None)
    out["restarts"] = int(sum(1 for d in recs if d["_boot"] != recs[0]["_boot"]))
    return out


def ols(points):
    """Least squares y = a + b*x. Returns (a, b, r2)."""
    n = len(points)
    if n < 3:
        return None
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] * p[0] for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    den = n * sxx - sx * sx
    if den == 0:
        return None
    b = (n * sxy - sx * sy) / den
    a = (sy - b * sx) / n
    ybar = sy / n
    sst = sum((p[1] - ybar) ** 2 for p in points)
    sse = sum((p[1] - (a + b * p[0])) ** 2 for p in points)
    r2 = 1.0 - sse / sst if sst else 0.0
    return a, b, r2


def span_rows(label, recs, min_hours=3.0):
    """One row per qualifying battery span -- the raw evidence behind the
    bucket medians."""
    interval = int(recs[0].get("dump_int_s") or 300)
    batt = [d for d in recs if not d.get("batt_vbus") and not d.get("batt_chg")]
    rows = []
    for s in contiguous_spans(batt, interval * 2.0):
        hours = (s[-1]["ts"] - s[0]["ts"]) / 3600.0
        if hours < min_hours:
            continue
        mv = theil_sen(smoothed([((d["ts"] - s[0]["ts"]) / 3600.0, float(d["batt_mv"]))
                                 for d in s if d.get("batt_mv")], 0.25))
        pct = theil_sen(smoothed([((d["ts"] - s[0]["ts"]) / 3600.0, float(d["batt_pct"]))
                                  for d in s if d.get("batt_pct") is not None], 0.25))
        if mv is None:
            continue
        durs = [d["duration_s"] for d in s if isinstance(d.get("duration_s"), (int, float))]
        ok = [d for d in s if d.get("dump_ok")]
        drops = 0
        prev = None
        for d in s:
            v = d.get("dropped_frames")
            if v is not None and prev is not None and v >= prev:
                drops += v - prev
            prev = v
        rows.append({
            "bucket": label,
            "span": "%s -> %s" % (
                datetime.datetime.fromtimestamp(s[0]["ts"]).strftime("%m-%d %H:%M"),
                datetime.datetime.fromtimestamp(s[-1]["ts"]).strftime("%m-%d %H:%M")),
            "hours": hours,
            "mv_per_h": mv,
            "pct_per_h": pct if pct is not None else float("nan"),
            "mv_range": "%d-%d" % (s[0]["batt_mv"], s[-1]["batt_mv"]),
            "start_mv": s[0]["batt_mv"],
            "end_mv": s[-1]["batt_mv"],
            "duty": 100.0 * sum(durs) / (hours * 3600.0),
            "ok_pct": 100.0 * len(ok) / len(s),
            "mb_per_h": sum(d["dump_bytes"] for d in ok if d.get("dump_bytes")) / 1e6 / hours,
            "drops_per_h": drops / hours,
        })
    return rows


def smoothed(points, bin_h):
    """Median of y within bin_h-hour bins (reduces load/IR-drop noise)."""
    bins = collections.defaultdict(list)
    for x, y in points:
        bins[round(x / bin_h)].append(y)
    return [(k * bin_h, statistics.median(v)) for k, v in sorted(bins.items())]


def rate_kbps(d):
    if not d.get("dump_bytes") or not d.get("duration_s"):
        return 0.0
    return d["dump_bytes"] / d["duration_s"] / 1024.0


def weighted_median(pairs):
    pairs = [(v, w) for v, w in pairs if v is not None]
    if not pairs:
        return None
    pairs.sort(key=lambda p: p[0])
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= tot / 2.0:
            return round(v, 3)
    return round(pairs[-1][0], 3)


def fmt(v, spec="%s"):
    return "-" if v is None else spec % v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.expanduser(
        "~/BirdNET-Pi/data/node-telemetry"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--phase-windows", action="store_true",
                    help="also report by controller phase wall window")
    ap.add_argument("--spans", action="store_true", help="per-span detail")
    ap.add_argument("--model", action="store_true",
                    help="fit drain = idle + k*duty across buckets")
    args = ap.parse_args()

    recs = load(args.dir)
    print("loaded %d dump records with a header (%s ... %s)" % (
        len(recs),
        datetime.datetime.fromtimestamp(recs[0]["ts"]).strftime("%Y-%m-%d %H:%M"),
        datetime.datetime.fromtimestamp(recs[-1]["ts"]).strftime("%Y-%m-%d %H:%M")))

    # Battery + ECO subset: this is the only operating mode where the config
    # under test controls power (eco_effective=0 = USB/always-on radio).
    eco = [d for d in recs if d.get("eco_effective") and not d.get("batt_vbus")]
    print("battery + ECO-effective records: %d (%.0f%% of all), "
          "of which discharging: %d\n" % (
              len(eco), 100.0 * len(eco) / len(recs),
              len([d for d in eco if not d.get("batt_chg")])))

    groups = collections.defaultdict(list)
    for d in eco:
        groups[bucket_key(d)].append(d)
    rows = [summarize("int=%ds tx=%s fw=%s" % (k[0], k[1], k[2]), v)
            for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))]

    hdr = ("%-30s %5s %7s %8s %7s %15s %6s %5s %6s %7s %7s %8s" % (
        "bucket", "recs", "wall_h", "disch_h", "duty", "mV/h [IQR]", "%/h",
        "ok%", "crw%", "MB/h", "kB/s", "GB/%SoC"))
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        mv = fmt(r.get("mv_per_h"), "%+.1f")
        if r.get("mv_iqr"):
            mv += " [%+.0f,%+.0f]" % (r["mv_iqr"][0], r["mv_iqr"][1])
        print("%-30s %5d %7s %8s %7s %15s %7s %5s %6s %7s %7s %8s" % (
            r["bucket"], r["records"], fmt(r.get("wall_h"), "%.1f"),
            fmt(r.get("span_hours"), "%.1f"), fmt(r.get("duty"), "%.1f%%"),
            mv, fmt(r.get("pct_per_h"), "%+.2f"), fmt(r.get("ok_pct"), "%.0f"),
            fmt(r.get("crawl_pct"), "%.0f"), fmt(r.get("mb_per_h"), "%.1f"),
            fmt(r.get("rate_kbps"), "%.0f"), fmt(r.get("pct_per_gb"), "%.0f")))

    if args.phase_windows:
        print("\nby controller phase window (battery + ECO-effective records only):")
        hdr2 = ("%-12s %5s %8s %7s %15s %7s %6s %6s %7s %7s" % (
            "phase", "recs", "disch_h", "duty", "mV/h [IQR]", "%/h", "ok%",
            "MB/h", "drop/h", "GB/%SoC"))
        print(hdr2)
        print("-" * len(hdr2))
        for name, a, b in PHASE_WINDOWS:
            lo, hi = epoch(a), epoch(b)
            sub = [d for d in recs if lo <= d["ts"] <= hi and d.get("eco_effective")
                   and not d.get("batt_vbus")]
            r = summarize(name, sub)
            mv = fmt(r.get("mv_per_h"), "%+.1f")
            if r.get("mv_iqr"):
                mv += " [%+.0f,%+.0f]" % (r["mv_iqr"][0], r["mv_iqr"][1])
            print("%-12s %5d %8s %7s %15s %7s %6s %6s %7s %7s" % (
                name, r["records"], fmt(r.get("span_hours"), "%.1f"),
                fmt(r.get("duty"), "%.1f%%"), mv, fmt(r.get("pct_per_h"), "%+.2f"),
                fmt(r.get("ok_pct"), "%.0f"), fmt(r.get("mb_per_h"), "%.1f"),
                fmt(r.get("drops_per_h"), "%.0f"), fmt(r.get("pct_per_gb"), "%.0f")))
        print("\ncaveat: cpu80 is only separable by TIME (cpu_mhz reads 160 at dump time,")
        print("since v1.54 raises the clock after association) and its window is")
        print("confounded by the fw 1.68 -> 1.69 install on 09-05 08:47.")

    if args.spans:
        print("\nper-span detail (battery spans >=3 h, slope from 15-min medians):")
        hdr3 = ("%-22s %32s %6s %8s %8s %9s %6s %5s %6s %8s" % (
            "bucket", "span (start -> end)", "hours", "mV/h", "%/h", "mV range",
            "duty", "ok%", "MB/h", "drop/h"))
        print(hdr3)
        print("-" * len(hdr3))
        for k, v in sorted(groups.items(), key=lambda kv: kv[1][0]["ts"]):
            label = "int=%ds tx=%s fw=%s" % (k[0], k[1], k[2])
            for r in span_rows(label, v):
                print("%-22s %32s %6.1f %+8.1f %+8.2f %9s %5.0f%% %5.0f %6.1f %8.0f" % (
                    r["bucket"], r["span"], r["hours"], r["mv_per_h"], r["pct_per_h"],
                    r["mv_range"], r["duty"], r["ok_pct"], r["mb_per_h"],
                    r["drops_per_h"]))

    if args.model:
        print("\nenergy models: |drain| (mV/h) = idle + k * duty fraction. The Li-ion")
        print("curve is much steeper below ~3.55 V, so only spans that stay above it are")
        print("comparable; a flat fit = neither variable moved the needle.")
        span_pts = []
        for k, v in groups.items():
            label = "int=%ds tx=%s fw=%s" % (k[0], k[1], k[2])
            for r in span_rows(label, v):
                if r["mv_per_h"] < 0 and r["end_mv"] >= 3550:
                    span_pts.append((r["duty"] / 100.0, abs(r["mv_per_h"])))
        for name, pts in (("span-level (comparable SOC band)", span_pts),
                          ("bucket-level", [(r["duty"] / 100.0, abs(r["mv_per_h"]))
                                            for r in rows if r.get("mv_per_h")
                                            and r.get("duty") and r.get("span_hours", 0) >= 8
                                            and r["mv_per_h"] < 0])):
            fit = ols(pts)
            if not fit:
                print("\n%s: not enough points" % name)
                continue
            a, b, r2 = fit
            tot = a + 0.10 * b
            print("\n%s (%d points):" % (name, len(pts)))
            print("  idle (duty -> 0): %+.1f mV/h   radio: %+.1f mV/h per unit duty" % (a, b))
            print("  at 10%% duty: %.1f idle + %.1f radio mV/h  (radio = %.0f%% of drain)"
                  % (a, 0.10 * b, 100.0 * 0.10 * b / tot))
            print("  R^2 = %.2f" % r2)
            if r2 < 0.3:
                print("  -> duty explains almost none of the variance: the better lever is")
                print("     whatever is not in this model (SOC window, ambient temp, link)")

    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
