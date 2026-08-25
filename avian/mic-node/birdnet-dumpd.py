#!/usr/bin/env python3
"""birdnet-dumpd — receive buffered-audio dumps from the T-SIM7080G mic node.

The node records continuously (48 kHz -> 24 kHz IMA-ADPCM, PSRAM ring) with
its WiFi radio off, and dumps the ring over TCP (:8557) on a duty cycle.
This service decodes the dump and writes 15 s, 24 kHz mono WAV segments into
StreamData, named by CAPTURE time:

    %Y-%m-%d-birdnet-UDP2-%H:%M:%S.wav

birdnet_analysis consumes them unchanged: ParseFileName takes the detection
date/time from the filename, and librosa resamples 24 kHz -> 48 kHz on load.
So backdated dumps appear on the Bird Up! collage/timeline at the true
capture time even though they arrive minutes late.

Dump protocol (see docs/buffered-recording-spec.md in the node repo):

    BUDP1\n
    node=<id>\n
    encoding=ima-adpcm\n
    rate=24000\n
    frame_samples=512\n
    frames=N\n
    capture_start_epoch=<unix sec, 0 = unknown>\n
    dropped_frames=<lifetime counter>
    tl_v=1
    <telemetry k=v lines — node fw v1.58+: fw_version, uptime_s, restart_count,
    boot_reason, prev_reboot?, batt_mv, batt_pct, batt_chg, batt_vbus,
    pmu_temp_c?, batt_mode, batt_eta_full_min, batt_life_min, esp_temp_c?,
    rssi_dbm, tx_dbm, eco_mode, eco_effective, cpu_mhz, buf_pending_frames,
    buf_used_pct, buf_dump_fails, capture_rate, dump_int_s>
    \n
    <N * 260 raw frame bytes>

Frame layout (260 B): int16 LE predictor | uint8 step index | uint8 reserved |
256 B = 512 nibbles (low nibble = earlier sample). Frames are independent.

Replies "OK\n" after the last frame is received and flushed.

Telemetry: every dump attempt with a complete header (success OR failure)
appends one JSON line to $NODE_TELEMETRY_DIR/node-telemetry-YYYY-MM-DD.jsonl
(default ~/BirdNET-Pi/data/node-telemetry, UTC day files, 45-day retention
via NODE_TELEMETRY_RETENTION_DAYS). The record is every header field
(numeric-coerced) plus ts, src_ip, dump_ok, dump_bytes, segments,
duration_s, and error (on failure). This is the staging point for a later
Prometheus exporter — note dropped_frames is a per-BOOT lifetime counter
(resets on node reboot; restart_count/boot_reason/prev_reboot identify the
boot). Telemetry rides the existing dump connection, so the node's ECO
radio duty cycle is unaffected.
"""

import calendar
import glob
import json
import logging
import os
import socket
import struct
import sys
import time
import wave

LISTEN_PORT = 8557
FRAME_BYTES = 260
FRAME_SAMPLES = 512
SEGMENT_SECONDS = 15

RECS_DIR = os.environ.get("RECS_DIR", os.path.expanduser("~/BirdSongs"))
STREAM_DATA = os.path.join(RECS_DIR, "StreamData")
# Liveness marker for external health checks: written after every completed
# dump. StreamData WAVs are consumed by birdnet_analysis within ~1-2 min of
# landing, so the dir is routinely EMPTY between dumps and "newest .wav" is
# not a reliable signal. The marker's mtime = last successful dump arrival;
# read by avian/api/health.php on the Pi (Bird Up! k8s birdup-health probe).
HEARTBEAT_FILE = os.path.join(STREAM_DATA, ".last-dump")

# Node telemetry staging (one JSONL line per dump attempt; see module docstring).
# NOT inside StreamData: that's the audio inbox swept by birdnet_analysis.
TELEMETRY_DIR = os.environ.get(
    "NODE_TELEMETRY_DIR", os.path.expanduser("~/BirdNET-Pi/data/node-telemetry"))
TELEMETRY_RETENTION_DAYS = int(os.environ.get("NODE_TELEMETRY_RETENTION_DAYS", "45"))

# Live-audio tee: decoded s16le PCM is also sent, fire-and-forget, to the
# local RTSP relay (rtsp-relay.py via mic-relay.sh -> mediamtx /mic) so the
# cluster BirdNET-Go can analyze the same raw mic audio. Loopback UDP is
# lossy under load by design: the tee must NEVER break the dump path.
TEE_ADDR = ("127.0.0.1", 8556)
TEE_RATE = 24000  # relay/ffmpeg pipeline is fixed at 24 kHz s16le mono
_tee_sock = None
_tee_rate_warned = False


def tee_pcm(samples: list, rate: int) -> None:
    """Best-effort tee of one decoded frame (list of int16) to the relay."""
    global _tee_sock, _tee_rate_warned
    if rate != TEE_RATE:
        if not _tee_rate_warned:
            log.warning("tee: dump rate %d != %d; skipping relay tee", rate, TEE_RATE)
            _tee_rate_warned = True
        return
    try:
        if _tee_sock is None:
            _tee_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _tee_sock.sendto(struct.pack("<%dh" % len(samples), *samples), TEE_ADDR)
    except Exception:
        pass  # relay down / loopback congestion — dumps must never notice

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [dumpd] %(levelname)s %(message)s",
)
log = logging.getLogger("dumpd")

# ---------------------------------------------------------------------------
# IMA ADPCM decoder (standard tables; must match the node encoder)
# ---------------------------------------------------------------------------

IMA_STEP = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
    253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
    1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
    3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
]
IMA_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


def decode_frame(frame: bytes) -> list:
    """260-byte frame -> list of 512 int16 samples."""
    predictor = struct.unpack_from("<h", frame, 0)[0]
    index = frame[2]
    if index > 88:
        index = 88
    out = []
    data = frame[4:]
    for byte in data:
        for nibble in (byte & 0x0F, byte >> 4):
            step = IMA_STEP[index]
            delta = step >> 3
            if nibble & 4:
                delta += step
            if nibble & 2:
                delta += step >> 1
            if nibble & 1:
                delta += step >> 2
            predictor += -delta if (nibble & 8) else delta
            predictor = max(-32768, min(32767, predictor))
            index += IMA_INDEX[nibble]
            index = max(0, min(88, index))
            out.append(predictor)
    return out


# ---------------------------------------------------------------------------
# Segment writer: slices the PCM stream into 15 s WAVs named by capture time
# ---------------------------------------------------------------------------

class SegmentWriter:
    def __init__(self, rate: int, start_epoch: float):
        self.rate = rate
        self.next_seg_epoch = start_epoch   # epoch of the next sample written
        self.samples_into_seg = 0
        self.wav = None
        self.path = None
        self.segments_written = 0

    def _open_segment(self):
        seg_epoch = self.next_seg_epoch
        name = time.strftime("%Y-%m-%d-birdnet-UDP2-%H:%M:%S.wav",
                             time.localtime(seg_epoch))
        self.path = os.path.join(STREAM_DATA, name)
        self.wav = wave.open(self.path, "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.rate)
        self.samples_into_seg = 0

    def _close_segment(self):
        if self.wav:
            self.wav.close()
            self.segments_written += 1
            log.info("wrote %s", os.path.basename(self.path))
            self.wav = None
            self.path = None

    def write(self, samples: list):
        seg_len = SEGMENT_SECONDS * self.rate
        i = 0
        while i < len(samples):
            if self.wav is None:
                self._open_segment()
            n = min(seg_len - self.samples_into_seg, len(samples) - i)
            self.wav.writeframes(struct.pack("<%dh" % n, *samples[i:i + n]))
            self.samples_into_seg += n
            self.next_seg_epoch += n / self.rate
            i += n
            if self.samples_into_seg >= seg_len:
                self._close_segment()

    def close(self):
        # Partial trailing segment is still valid audio; close it so analysis
        # picks it up (it matches on filename, any length is fine).
        self._close_segment()


def touch_heartbeat(bytes_total: int, segments: int) -> None:
    """Record a successful dump arrival (survives analysis consumption)."""
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write("%s %d bytes %d segments\n"
                    % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), bytes_total, segments))
    except OSError as exc:
        log.error("heartbeat marker write failed: %s", exc)


# ---------------------------------------------------------------------------
# Telemetry JSONL (per-dump-attempt record; staged for a later exporter)
# ---------------------------------------------------------------------------

_last_sweep_day = None


_TELEMETRY_STRING_KEYS = {"node", "encoding", "boot_reason", "prev_reboot",
                          "fw_version"}


def _coerce(key: str, v: str):
    if key in _TELEMETRY_STRING_KEYS:
        return v
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _sweep_telemetry(today: str) -> None:
    """Once per UTC day, delete telemetry files older than the retention."""
    global _last_sweep_day
    if _last_sweep_day == today:
        return
    _last_sweep_day = today
    cutoff = time.time() - TELEMETRY_RETENTION_DAYS * 86400
    for path in glob.glob(os.path.join(TELEMETRY_DIR, "node-telemetry-*.jsonl")):
        try:
            day = os.path.basename(path)[len("node-telemetry-"):len("node-telemetry-") + 10]
            if calendar.timegm(time.strptime(day, "%Y-%m-%d")) < cutoff:
                os.remove(path)
                log.info("telemetry retention: removed %s", path)
        except (ValueError, OSError) as exc:
            log.warning("telemetry retention: skipping %s: %s", path, exc)


def write_telemetry(fields: dict, addr, ok: bool, bytes_got: int,
                    segments: int, duration: float, error) -> None:
    """Append one JSON line per dump attempt. Never breaks the dump path."""
    try:
        rec = {"ts": time.time(), "src_ip": addr[0], "dump_ok": ok,
               "dump_bytes": bytes_got, "segments": segments,
               "duration_s": round(duration, 2)}
        if error:
            rec["error"] = str(error)
        for k, v in fields.items():
            if k not in rec:
                rec[k] = _coerce(k, v)
        os.makedirs(TELEMETRY_DIR, exist_ok=True)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        _sweep_telemetry(today)
        with open(os.path.join(TELEMETRY_DIR,
                               "node-telemetry-%s.jsonl" % today), "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception as exc:
        log.error("telemetry write failed: %s", exc)


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

def read_header(conn: socket.socket) -> dict:
    buf = b""
    while b"\n\n" not in buf:
        chunk = conn.recv(1024)
        if not chunk:
            raise ConnectionError("eof during header")
        buf += chunk
        if len(buf) > 4096:
            raise ValueError("header too large")
    head, rest = buf.split(b"\n\n", 1)
    lines = head.decode("ascii", "replace").strip().split("\n")
    if not lines or lines[0] != "BUDP1":
        raise ValueError("bad magic")
    fields = {}
    for line in lines[1:]:
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip()] = v.strip()
    if fields.get("encoding") != "ima-adpcm":
        raise ValueError("unsupported encoding: %s" % fields.get("encoding"))
    return fields, rest


def handle(conn: socket.socket, addr):
    # Timeouts must cover the WHOLE exchange, including the header: a node that
    # panics/reboots mid-dump leaves a half-open corpse; blocking on it forever
    # wedges this single-threaded daemon (field-observed 2026-08-17: every dump
    # failed until restart). Keepalive reaps corpses even if a code path blocks.
    conn.settimeout(60)
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    fields, rest = read_header(conn)
    t0 = time.time()
    rate = int(fields.get("rate", 24000))
    frames = int(fields["frames"])
    capture_start = float(fields.get("capture_start_epoch", "0"))
    dropped = int(fields.get("dropped_frames", "0"))
    if capture_start <= 0:
        capture_start = time.time() - frames * FRAME_SAMPLES / rate
        log.warning("dump from %s had no capture timestamp; using arrival-based time", addr)

    log.info("dump from %s: %d frames (%.1f s), capture_start=%s, node_dropped=%d",
             addr, frames, frames * FRAME_SAMPLES / rate,
             time.strftime("%H:%M:%S", time.localtime(capture_start)), dropped)

    got = 0
    segments = 0
    try:
        writer = SegmentWriter(rate, capture_start)
        need = frames * FRAME_BYTES
        pending = rest
        while got < need:
            if not pending:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    raise ConnectionError("eof at %d/%d bytes" % (got, need))
                pending = chunk
            # decode whole frames out of pending
            whole = (len(pending) // FRAME_BYTES) * FRAME_BYTES
            whole = min(whole, need - got)
            if whole == 0:
                # wait for more data to complete a frame
                chunk = conn.recv(1 << 16)
                if not chunk:
                    raise ConnectionError("eof at %d/%d bytes (partial frame)" % (got, need))
                pending += chunk
                continue
            block, pending = pending[:whole], pending[whole:]
            for off in range(0, whole, FRAME_BYTES):
                samples = decode_frame(block[off:off + FRAME_BYTES])
                writer.write(samples)
                tee_pcm(samples, rate)
            got += whole

        writer.close()
        conn.sendall(b"OK\n")
        segments = writer.segments_written
        log.info("dump complete: %d bytes, %d segments", got, segments)
        touch_heartbeat(got, segments)
    except Exception as exc:
        # Failed attempts carry telemetry too: a flaky link is exactly when
        # dropped_frames starts climbing, and the header already arrived.
        write_telemetry(fields, addr, ok=False, bytes_got=got, segments=segments,
                        duration=time.time() - t0, error=exc)
        raise
    write_telemetry(fields, addr, ok=True, bytes_got=got, segments=segments,
                    duration=time.time() - t0, error=None)


def main():
    os.makedirs(STREAM_DATA, exist_ok=True)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", LISTEN_PORT))
    srv.listen(2)
    log.info("listening on :%d, writing to %s", LISTEN_PORT, STREAM_DATA)
    while True:
        conn, addr = srv.accept()
        try:
            handle(conn, addr)
        except Exception as exc:
            log.error("dump from %s failed: %s", addr, exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
