#!/usr/bin/env python3
"""Host smoke test for the night-sleep bracket in node-telemetry-exporter.py
(plan-2026-09-23-night-sleep-observability.md, Phase A). Not deployed to the
Pi — run on the laptop before scp:
    python3 avian/mic-node/night-observability-smoke.py

Drives NodeTelemetry._apply stage-by-stage with the record sequence of two
real night cycles and asserts the persisted night state + rendered metrics.
Record field values mirror what dumpd's _coerce produces for real v1.73 dump
headers (night_sleep/night_wake_s/night_slept_s arrive as ints).
"""
import importlib.util
import sys
from pathlib import Path

EXPORTER = Path(__file__).resolve().parent / "node-telemetry-exporter.py"
spec = importlib.util.spec_from_file_location("nte", EXPORTER)
nte = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nte)   # side-effect-free: main() is __name__ guarded


def check(name, got, want):
    status = "ok" if got == want else "FAIL"
    print("  %-4s %s: got %r want %r" % (status, name, got, want))
    return status == "ok"


def main():
    passed = True
    n = nte.NodeTelemetry("/nonexistent")

    # --- stage 1: day record, pre-first-night -------------------------------
    n._apply({"ts": 100, "dump_ok": True, "night_sleep": 0}, True)
    passed &= check("sleep (day)", n.night["sleep"], 0)
    passed &= check("wake (none yet)", n.night["wake_epoch"], 0)
    passed &= check("bracket (none yet)", n.night["bracket_ts"], 0.0)

    # --- stage 2: dusk handoff (night_sleep=1 + night_wake_s) ---------------
    n._apply({"ts": 2000, "dump_ok": True, "night_sleep": 1,
              "night_wake_s": 1790240000}, True)
    passed &= check("sleep after dusk", n.night["sleep"], 1)
    passed &= check("wake after dusk", n.night["wake_epoch"], 1790240000)
    passed &= check("bracket after dusk", n.night["bracket_ts"], 2000.0)

    # --- stage 3: PWRON mid-night wake edge (night_sleep=0, no wake_s) ------
    n._apply({"ts": 3000, "dump_ok": True, "night_sleep": 0}, True)
    passed &= check("wake persisted (mid-night)", n.night["wake_epoch"], 1790240000)
    passed &= check("bracket kept (mid-night)", n.night["bracket_ts"], 2000.0)

    # --- stage 4: morning wake (night_sleep=0 + night_slept_s) --------------
    n._apply({"ts": 3200, "dump_ok": True, "night_sleep": 0, "night_slept_s": 36000}, True)
    passed &= check("sleep after wake", n.night["sleep"], 0)
    passed &= check("wake persisted (morning)", n.night["wake_epoch"], 1790240000)
    passed &= check("slept (morning)", n.night["slept_s"], 36000)
    passed &= check("bracket kept (morning)", n.night["bracket_ts"], 2000.0)

    # --- stage 5: ordinary day records keep the cycle state -----------------
    for ts in (4000, 5000):
        n._apply({"ts": ts, "dump_ok": True, "night_sleep": 0}, True)
    passed &= check("wake kept (day)", n.night["wake_epoch"], 1790240000)
    passed &= check("bracket kept (day)", n.night["bracket_ts"], 2000.0)

    # --- stage 6: next dusk — new bracket replaces the cycle ----------------
    n._apply({"ts": 101000, "dump_ok": True, "night_sleep": 1,
              "night_wake_s": 1790326400}, True)
    passed &= check("sleep after next dusk", n.night["sleep"], 1)
    passed &= check("wake replaced", n.night["wake_epoch"], 1790326400)
    passed &= check("bracket replaced", n.night["bracket_ts"], 101000.0)
    passed &= check("slept cleared (new cycle)", n.night["slept_s"], 0)

    # --- stage 7: next morning ----------------------------------------------
    n._apply({"ts": 103000, "dump_ok": True, "night_sleep": 0, "night_slept_s": 36200}, True)
    passed &= check("slept (next morning)", n.night["slept_s"], 36200)
    passed &= check("wake persisted (next morning)", n.night["wake_epoch"], 1790326400)

    # --- rendered metrics (final state) -------------------------------------
    print("render")
    out = n.render()
    for r in ["birdnode_night_sleep 0",
              "birdnode_night_bracket_timestamp_seconds 101000.000",
              "birdnode_night_wake_timestamp_seconds 1790326400",
              "birdnode_night_slept_seconds 36200"]:
        passed &= check("render contains '%s'" % r, r in out, True)

    # --- pre-first-night defaults: all 0 (keeps alert `==0` branches alive) --
    print("pre-first-night defaults")
    n0 = nte.NodeTelemetry("/nonexistent")
    n0._apply({"ts": 50, "dump_ok": True}, True)
    out0 = n0.render()
    for r in ["birdnode_night_sleep 0",
              "birdnode_night_bracket_timestamp_seconds 0.000",
              "birdnode_night_wake_timestamp_seconds 0",
              "birdnode_night_slept_seconds 0"]:
        passed &= check("render contains '%s'" % r, r in out0, True)

    print("\n%s" % ("ALL PASSED" if passed else "FAILURES PRESENT"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())