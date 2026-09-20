#!/usr/bin/env python3
"""birdup node-telemetry exporter — Bird Up! → Prometheus.

Exposes four collector groups on one HTTP endpoint (:9558, LAN, no auth):

  1. node telemetry   — tails ~/BirdNET-Pi/data/node-telemetry/*.jsonl
                        (written by birdnet-dumpd.py per dump attempt) and
                        exports the latest record state as `birdnode_*`
                        gauges/counters. Data cadence is ~5 min; Prometheus
                        scrapes at 60 s.
                        Since tl_v=2 the records also carry dump-health
                        fields (retry backoff, prev-attempt outcome/stats,
                        dump cap, dumpd protocol version) which are exported
                        when present; records from older node firmware simply
                        omit them.
  2. backend services — `birdup_service_active{unit=...}` from unprivileged
                        `systemctl is-active`, plus dump-heartbeat marker
                        (StreamData/.last-dump) age and birds.db age.
  3. detections       — `birdup_detections_*` from ~/BirdNET-Pi/scripts/
                        birds.db (read-only). COUNT(*) is a full scan, so
                        the exporter tracks the last-seen rowid and counts
                        only NEW rows per scrape, persisting cumulative
                        counters in ~/.local/state/birdup-exporter/state.json.
  4. web front end    — THIS SERVICE DOES NOT DO THIS. Caddy metrics (:2020),
                        php-fpm exporter (:9253) and mtail (:3903) are
                        separate services (see docs/deployment/README.md).

Standard library only (no pip deps on the Pi). Runs as user dylanberry,
see avian/mic-node/birdup-exporter.service.

ECO constraint: never contacts the ESP32 node (192.168.86.51) directly —
the JSONL is the only sanctioned node data source.
"""

import datetime
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_PORT = int(os.environ.get("BIRDUDP_EXPORTER_PORT", "9558"))
TELEMETRY_DIR = os.environ.get(
    "NODE_TELEMETRY_DIR", os.path.expanduser("~/BirdNET-Pi/data/node-telemetry"))
STREAM_DATA = os.environ.get(
    "STREAM_DATA_DIR", os.path.expanduser("~/BirdSongs/StreamData"))
HEARTBEAT_FILE = os.path.join(STREAM_DATA, ".last-dump")
BIRDS_DB = os.environ.get(
    "BIRDS_DB", os.path.expanduser("~/BirdNET-Pi/scripts/birds.db"))
STATE_FILE = os.environ.get(
    "BIRDUDP_STATE_FILE",
    os.path.expanduser("~/.local/state/birdup-exporter/state.json"))
SERVICES = os.environ.get(
    "BIRDUDP_SERVICES",
    "birdnet_analysis,birdnet-dumpd,caddy,php8.4-fpm").split(",")
TAIL_TICK_S = 5          # JSONL re-poll interval (records arrive every ~5 min)
SCRAPE_MAX_S = 10        # hard cap for a single /metrics render

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [exporter] %(levelname)s %(message)s")
log = logging.getLogger("exporter")

# ---------------------------------------------------------------------------
# Node telemetry: JSONL tailing
# ---------------------------------------------------------------------------

# Record fields -> (metric name, type). Values are read from the MOST RECENT
# record only (the exporter is a state sampler, not an event counter).
# dropped_frames is a per-BOOT lifetime counter — exporting the latest value
# as a counter is correct: Prometheus increase()/rate() handle the boot reset.
_FIELD_METRICS = [
    # (json key, metric name, kind, help)
    ("batt_pct", "birdnode_battery_soc_percent", "gauge",
     "Battery state of charge as reported by the node (percent; -1 = none)."),
    ("batt_mv", "birdnode_battery_millivolts", "gauge",
     "Battery voltage as reported by the node (millivolts)."),
    ("batt_chg", "birdnode_battery_charging", "gauge",
     "Battery charging flag (0/1)."),
    ("batt_vbus", "birdnode_battery_vbus", "gauge",
     "USB VBUS present flag (0/1)."),
    ("batt_mode", "birdnode_battery_mode", "gauge",
     "Battery mode: 0 unknown, 1 charging, 2 discharging."),
    ("batt_eta_full_min", "birdnode_battery_eta_full_minutes", "gauge",
     "Estimated minutes until battery full (-1 = unknown)."),
    ("batt_life_min", "birdnode_battery_life_minutes", "gauge",
     "Estimated battery life remaining in minutes (-1 = unknown)."),
    ("esp_temp_c", "birdnode_esp_temp_celsius", "gauge",
     "ESP32 die temperature (celsius)."),
    ("pmu_temp_c", "birdnode_pmu_temp_celsius", "gauge",
     "PMU temperature (celsius)."),
    ("rssi_dbm", "birdnode_rssi_dbm", "gauge",
     "WiFi RSSI (dBm)."),
    ("tx_dbm", "birdnode_tx_dbm", "gauge",
     "WiFi TX power (dBm)."),
    ("cpu_mhz", "birdnode_cpu_mhz", "gauge",
     "ESP32 CPU clock (MHz)."),
    ("eco_mode", "birdnode_eco_mode", "gauge",
     "ECO mode configured (0/1)."),
    ("eco_effective", "birdnode_eco_effective", "gauge",
     "ECO mode effective (0/1)."),
    ("buf_pending_frames", "birdnode_buf_pending_frames", "gauge",
     "Ring buffer frames waiting to dump."),
    ("buf_used_pct", "birdnode_buf_used_percent", "gauge",
     "Ring buffer used (percent of capacity)."),
    ("buf_dump_fails", "birdnode_buf_dump_fails", "gauge",
     "Ring dump failure count (per boot)."),
    ("uptime_s", "birdnode_uptime_seconds", "gauge",
     "Node uptime (seconds)."),
    ("restart_count", "birdnode_restart_count", "gauge",
     "Node persisted boot counter."),
    ("capture_rate", "birdnode_capture_rate", "gauge",
     "Capture sample rate (Hz)."),
    ("dump_int_s", "birdnode_dump_interval_seconds", "gauge",
     "Configured dump interval (seconds)."),
    ("dropped_frames", "birdnode_dropped_frames_total", "counter",
     "Lifetime dropped frames per node boot (resets on reboot)."),
    # tl_v=2 dump-health fields (node header; present only on newer firmware).
    ("retry_backoff_s", "birdnode_dump_backoff_seconds", "gauge",
     "Current retry backoff before the next dump attempt (0 = none; grows 30-900 s on consecutive-fail streaks)."),
    ("dump_max_frames", "birdnode_buf_dump_max_frames", "gauge",
     "User-configured dump frame cap (effective cap may be larger due to the production floor)."),
    ("prev_attempt_ms", "birdnode_last_attempt_milliseconds", "gauge",
     "Wall-clock duration of the node's PREVIOUS dump attempt (0 = none yet)."),
    ("prev_bytes_sent", "birdnode_last_attempt_bytes", "gauge",
     "Bytes the node's PREVIOUS dump attempt moved (0 = none yet)."),
    ("dumpd_v", "birdnode_dumpd_version", "gauge",
     "birdnet-dumpd protocol version that wrote this envelope record (absent = v1)."),
    # tl_v=2 crawl-watchdog fields (node v1.70+; absent on older firmware).
    ("crawl_count", "birdnode_crawl_count", "gauge",
     "Consecutive crawl-classified dump attempts (slow-TX link detector; >=3 triggers a driver restart)."),
    ("crawl_last_kbps", "birdnode_crawl_kbps", "gauge",
     "Throughput of the node's previous dump attempt (KB/s)."),
    ("crawl_thresh_kbps", "birdnode_crawl_threshold_kbps", "gauge",
     "Configured crawl threshold (KB/s); attempts slower than this count toward radio self-heal."),
]

# Last-record dump statistics (from the JSONL envelope written by dumpd).
_DUMP_STAT_METRICS = [
    ("dump_bytes", "birdnode_last_dump_bytes", "gauge", "Bytes in last dump."),
    ("segments", "birdnode_last_dump_segments", "gauge", "Segments in last dump."),
    ("duration_s", "birdnode_last_dump_duration_seconds", "gauge",
     "Duration of last dump transfer (seconds)."),
]


class NodeTelemetry:
    """Tails the JSONL day files, keeps the latest record state."""

    def __init__(self, telemetry_dir: str):
        self.dir = telemetry_dir
        self.lock = threading.Lock()
        self.latest = {}            # record fields (key -> value)
        self.last_ts = 0.0          # ts of newest record; 0 until seen
        self.records_ok = 0         # counters accumulate from exporter start
        self.records_fail = 0
        self._handles = {}          # path -> open file handle
        self._seeded = False

    def _day_files(self) -> list:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        paths = [os.path.join(self.dir, "node-telemetry-%s.jsonl" % day)
                 for day in (today, yesterday)]
        return [p for p in paths if os.path.isfile(p)]

    def _seed(self) -> None:
        """On startup read only the newest line (do not re-count history)."""
        files = sorted(self._day_files())
        if not files:
            return
        newest = files[-1]
        try:
            with open(newest, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if size == 0:
                    return
                f.seek(max(0, size - (1 << 13)))
                data = f.read().splitlines()
                if not data:
                    return
                rec = json.loads(data[-1].decode("utf-8", "replace"))
                self._apply(rec, count=False)
                self._seeded = True
                log.info("seeded from %s (ts=%.0f)", newest, self.last_ts)
        except Exception as exc:
            log.warning("seed failed: %s", exc)

    def _apply(self, rec: dict, count: bool) -> None:
        with self.lock:
            if count:
                if rec.get("dump_ok") is True:
                    self.records_ok += 1
                elif rec.get("dump_ok") is False:
                    self.records_fail += 1
                # pre-1.58 records may omit dump_ok entirely; still update state
            self.latest.update(rec)
            ts = rec.get("ts")
            if isinstance(ts, (int, float)) and ts > self.last_ts:
                self.last_ts = ts

    def tail_once(self) -> None:
        if not self._seeded:
            self._seed()
        for path in self._day_files():
            try:
                st = os.stat(path)
                fh = self._handles.get(path)
                if fh is None:
                    fh = open(path, "rb")
                    fh.seek(0, os.SEEK_END)
                    self._handles[path] = fh
                if st.st_size < fh.tell():
                    # File rotated/truncated (unlikely: day files are append-only)
                    fh.seek(0, os.SEEK_END)
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line.decode("utf-8", "replace"))
                        self._apply(rec, count=True)
                    except (ValueError, TypeError) as exc:
                        log.warning("bad jsonl line in %s: %s", path, exc)
            except OSError as exc:
                log.warning("tail %s: %s", path, exc)
        # Drop handles for files that no longer exist (retention sweep).
        for path in list(self._handles):
            if path not in self._day_files():
                try:
                    self._handles.pop(path).close()
                except OSError:
                    pass

    def render(self) -> str:
        with self.lock:
            latest = dict(self.latest)
            last_ts = self.last_ts
            ok, fail = self.records_ok, self.records_fail
        out = []
        out.append("# HELP birdnode_dump_records_total Number of dump attempts observed since exporter start.")
        out.append("# TYPE birdnode_dump_records_total counter")
        out.append("birdnode_dump_records_total{result=\"ok\"} %d" % ok)
        out.append("birdnode_dump_records_total{result=\"fail\"} %d" % fail)
        out.append("# HELP birdnode_telemetry_last_record_timestamp_seconds Unix epoch of the newest telemetry record (0 until first record).")
        out.append("# TYPE birdnode_telemetry_last_record_timestamp_seconds gauge")
        out.append("birdnode_telemetry_last_record_timestamp_seconds %.3f" % last_ts)
        node = latest.get("node", "unknown")
        info = {'node': node if isinstance(node, str) else "unknown",
                'fw_version': str(latest.get("fw_version", "unknown")),
                'boot_reason': str(latest.get("boot_reason", "unknown")),
                'prev_reboot': str(latest.get("prev_reboot", "unknown"))}
        labels = ', '.join('%s="%s"' % (k, _label_escape(v)) for k, v in info.items())
        out.append("# HELP birdnode_info Static identity of the most recent node boot (prev_reboot = latched cause of the previous reboot, if the node provided one).")
        out.append("# TYPE birdnode_info gauge")
        out.append("birdnode_info{%s} 1" % labels)
        # prev_result is a string (tl_v=2), so it cannot go through the generic
        # numeric loop below (_fmt would emit NaN). Exported as the single
        # current result: prometheus_last_result{result="stall"} == 1 fires the
        # streak alert; when the next record flips the value, the old label set
        # simply stops being emitted (stale after the scrape gap, alert clears).
        prev_result = latest.get("prev_result")
        if isinstance(prev_result, str) and prev_result:
            out.append("# HELP birdnode_last_result Outcome of the node's PREVIOUS dump attempt. Enum: none, ok, stall, eof, connect, hdr, ack_timeout, assoc (none = no attempt yet; assoc = association timed out, nothing sent).")
            out.append("# TYPE birdnode_last_result gauge")
            out.append("birdnode_last_result{result=\"%s\"} 1" % _label_escape(prev_result))
        for key, name, kind, help_ in _FIELD_METRICS + _DUMP_STAT_METRICS:
            if key not in latest:
                continue
            out.append("# HELP %s %s" % (name, help_))
            out.append("# TYPE %s %s" % (name, kind))
            out.append("%s %s" % (name, _fmt(latest[key])))
        return "\n".join(out) + "\n"


def _label_escape(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    try:
        f = float(v)
        return ("%.6f" % f).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "NaN"


# ---------------------------------------------------------------------------
# Backend app collector (Pi services + dump heartbeat + db age)
# ---------------------------------------------------------------------------

def service_active(unit: str) -> int:
    try:
        r = subprocess.run(["systemctl", "is-active", unit],
                           capture_output=True, text=True, timeout=5)
        return 1 if r.returncode == 0 and r.stdout.strip() == "active" else 0
    except Exception:
        return 0


def mtime_of(path: str):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def render_backend() -> str:
    out = []
    out.append("# HELP birdup_service_active Whether a Bird Up! systemd unit is active (1) or not (0).")
    out.append("# TYPE birdup_service_active gauge")
    for unit in SERVICES:
        out.append('birdup_service_active{unit="%s"} %d' % (unit, service_active(unit)))
    lt = mtime_of(HEARTBEAT_FILE)
    out.append("# HELP birdup_last_dump_timestamp_seconds Unix epoch mtime of StreamData/.last-dump (node heartbeat; 0 if absent).")
    out.append("# TYPE birdup_last_dump_timestamp_seconds gauge")
    out.append("birdup_last_dump_timestamp_seconds %.3f" % (lt or 0.0))
    if os.path.isdir(STREAM_DATA):
        try:
            n = sum(1 for f in os.listdir(STREAM_DATA)
                    if f.lower().endswith((".wav", ".mp3", ".raw")))
        except OSError:
            n = 0
    else:
        n = 0
    out.append("# HELP birdup_streamdata_files Number of unconsumed audio files in StreamData (normally 0 between dumps).")
    out.append("# TYPE birdup_streamdata_files gauge")
    out.append("birdup_streamdata_files %d" % n)
    db_mtime = mtime_of(BIRDS_DB)
    db_age = (time.time() - db_mtime) if db_mtime else -1
    out.append("# HELP birdup_birds_db_age_seconds Age of the birds.db detections database file.")
    out.append("# TYPE birdup_birds_db_age_seconds gauge")
    out.append("birdup_birds_db_age_seconds %.3f" % db_age)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Detections collector (birds.db, incremental rowid counting)
# ---------------------------------------------------------------------------

class Detections:
    """Cumulative detection counters using last-rowid increments + a state file.

    birds.db grows by thousands of rows/day in season; COUNT(*) is a full
    scan, so this never does a full COUNT after startup. State is persisted
    so counters survive exporter restarts.
    """

    def __init__(self, db_path: str, state_file: str):
        self.db = db_path
        self.state_file = state_file
        self.lock = threading.Lock()
        self.last_rowid = 0
        self.total = 0
        self.species = set()
        self.conf_sum = 0.0
        self.conf_count = 0
        self.last_det_epoch = 0.0
        self._load_state()

    def _load_state(self) -> None:
        try:
            with open(self.state_file) as f:
                st = json.load(f)
            self.last_rowid = int(st.get("last_rowid", 0))
            self.total = int(st.get("total", 0))
            self.species = set(st.get("species", []))
            self.conf_sum = float(st.get("conf_sum", 0.0))
            self.conf_count = int(st.get("conf_count", 0))
            self.last_det_epoch = float(st.get("last_det_epoch", 0.0))
        except (OSError, ValueError, TypeError):
            log.info("no prior state; computing full birds.db baseline "
                     "(one-time, may take a while on a large db)")

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"last_rowid": self.last_rowid,
                           "total": self.total,
                           "species": sorted(self.species),
                           "conf_sum": self.conf_sum,
                           "conf_count": self.conf_count,
                           "last_det_epoch": self.last_det_epoch}, f)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            log.warning("state write failed: %s", exc)

    def _seed_baseline(self) -> None:
        """Full-scan counts only when no state file exists (first boot / reset)."""
        try:
            conn = sqlite3.connect(
                "file:%s?mode=ro" % self.db, uri=True, timeout=5)
            conn.execute("PRAGMA busy_timeout=5000")
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(MAX(rowid),0) FROM detections")
            base = cur.fetchone()[0]
            # De-normalize Hidden rows: advance past them but do not count.
            self.last_rowid = int(base)
            cur.execute("SELECT COUNT(*), COALESCE(SUM(Confidence),0), "
                        "COALESCE(COUNT(Confidence),0), COUNT(DISTINCT Sci_Name) "
                        "FROM detections WHERE Hidden=0")
            row = cur.fetchone()
            if row:
                self.total, self.conf_sum, self.conf_count, n_species = row
                self.total = int(self.total)
                self.conf_count = int(self.conf_count)
                self.species = set(s[0] for s in
                                   cur.execute("SELECT DISTINCT Sci_Name FROM detections WHERE Hidden=0"))
            cur.execute("SELECT Date, Time FROM detections "
                        "ORDER BY Date DESC, Time DESC LIMIT 1")
            newest = cur.fetchone()
            if newest and newest[0]:
                self.last_det_epoch = _local_epoch(newest[0], newest[1] or "00:00:00")
            conn.close()
            self._save_state()
            log.info("birds.db baseline: total=%d species=%d last_rowid=%d",
                     self.total, len(self.species), self.last_rowid)
        except sqlite3.Error as exc:
            log.warning("birds.db baseline failed: %s", exc)

    def refresh(self) -> None:
        if not os.path.isfile(self.db):
            return
        with self.lock:
            if self.last_rowid == 0 and self.total == 0 and not self.species:
                self._seed_baseline()
                return
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % self.db,
                                   uri=True, timeout=5)
            conn.execute("PRAGMA busy_timeout=5000")
            cur = conn.cursor()
            cur.execute("SELECT rowid, Date, Time, Sci_Name, Confidence, Hidden "
                        "FROM detections WHERE rowid > ? ORDER BY rowid",
                        (self.last_rowid,))
            rows = cur.fetchall()
            conn.close()
        except sqlite3.Error as exc:
            log.warning("detections refresh failed: %s", exc)
            return
        if not rows:
            return
        max_rowid = self.last_rowid
        with self.lock:
            for rowid, date, tm, sci, conf, hidden in rows:
                if rowid > max_rowid:
                    max_rowid = rowid
                if hidden:
                    continue
                self.total += 1
                if sci:
                    self.species.add(sci)
                if conf is not None:
                    self.conf_sum += float(conf)
                    self.conf_count += 1
                if date:
                    ep = _local_epoch(date, tm or "00:00:00")
                    if ep > self.last_det_epoch:
                        self.last_det_epoch = ep
            self.last_rowid = max_rowid
            self._save_state()
            log.info("detections +%d new (total=%d species=%d)",
                     len(rows), self.total, len(self.species))

    def render(self) -> str:
        with self.lock:
            total, n_species, conf_sum, conf_n, last_ep = (
                self.total, len(self.species), self.conf_sum,
                self.conf_count, self.last_det_epoch)
        out = []
        out.append("# HELP birdup_detections_total Lifetime bird detections (Hidden=0), cumulative across exporter restarts.")
        out.append("# TYPE birdup_detections_total counter")
        out.append("birdup_detections_total %d" % total)
        out.append("# HELP birdup_detections_species Distinct species count (lifetime).")
        out.append("# TYPE birdup_detections_species gauge")
        out.append("birdup_detections_species %d" % n_species)
        out.append("# HELP birdup_last_detection_timestamp_seconds Unix epoch of the newest detection.")
        out.append("# TYPE birdup_last_detection_timestamp_seconds gauge")
        out.append("birdup_last_detection_timestamp_seconds %.3f" % last_ep)
        out.append("# HELP birdup_detection_confidence_sum Sum of detection confidences (lifetime).")
        out.append("# TYPE birdup_detection_confidence_sum counter")
        out.append("birdup_detection_confidence_sum %.6f" % conf_sum)
        out.append("# HELP birdup_detection_confidence_count Number of detections with confidence.")
        out.append("# TYPE birdup_detection_confidence_count counter")
        out.append("birdup_detection_confidence_count %d" % conf_n)
        return "\n".join(out) + "\n"


def _local_epoch(date_str: str, time_str: str) -> float:
    try:
        dt = datetime.datetime.strptime(
            "%s %s" % (date_str, time_str), "%Y-%m-%d %H:%M:%S")
        return dt.timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class MetricsHandler(BaseHTTPRequestHandler):
    server_version = "birdup-exporter/1.1"

    def do_GET(self):
        if self.path not in ("/", "/metrics"):
            self.send_error(404)
            return
        parts = []
        node = self.server.node
        det = self.server.detections
        start = time.time()
        det.refresh()
        parts.append(node.render())
        parts.append(render_backend())
        parts.append(det.render())
        body = ("".join(parts)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if time.time() - start > SCRAPE_MAX_S:
            log.warning("/metrics render took >%ds", SCRAPE_MAX_S)

    def log_message(self, fmt, *args):
        pass


class MetricsServer(ThreadingHTTPServer):
    """Carries references to the collectors."""

    node = None
    detections = None


_REQ_LOCK = threading.Lock()  # serialise renders (cheap; avoids sqlite contention)


def main():
    node = NodeTelemetry(TELEMETRY_DIR)
    det = Detections(BIRDS_DB, STATE_FILE)

    def tail_loop():
        while True:
            try:
                node.tail_once()
            except Exception as exc:
                log.error("tail loop: %s", exc)
            time.sleep(TAIL_TICK_S)

    threading.Thread(target=tail_loop, daemon=True).start()
    httpd = MetricsServer(("0.0.0.0", LISTEN_PORT), MetricsHandler)
    httpd.node = node
    httpd.detections = det
    log.info("listening on :%d (telemetry %s, db %s)", LISTEN_PORT,
             TELEMETRY_DIR, BIRDS_DB)
    httpd.serve_forever()


if __name__ == "__main__":
    sys.exit(main())