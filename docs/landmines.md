# Bird Up! — Hard-Won Lessons

Split out of `AGENTS.md` (2026-10-10). Every item here cost a live outage or a wasted
debugging session. Read before changing the analysis pipeline, the cutout toolchain, or Pi services.

---

## Hard-won lessons

- The birdnet venv must include **resampy**: dumpd writes 24 kHz WAVs and
  `analysis.py` resamples with `res_type='kaiser_fast'` (resampy-only in
  librosa). Without it every analysis fails and the StreamData backlog
  crash-loops the service, flooding the journal (2026-08-17: 4k-file backlog,
  journal vacuumed). Also: **rembg is pinned ≤2.0.68** — 2.0.70+ requires
  pillow≥12.1, which violates streamlit's `pillow<12` constraint.
- A Gemini API **429 can mean billing/credits exhausted**, not rate limiting.
- Cutouts: **BiRefNet locally, u2netp only on the Pi** — u2netp over-removes
  pale plumage (belly/vent holes).
- Style-reference prints can reinforce wrong-genus drift (a swift collapsed to a
  swallow because the style print had swallows); `ANTI_REF_TRIGGERS` in
  `pregen.py` is the fix mechanism.
- "Missed" detections are usually **threshold-filtered, not pipeline failures** —
  chunk scores just under `CONFIDENCE` are silently dropped.
- Pi sudo for `dylanberry` is **not** passwordless; the service-restart
  workaround is in `CONTEXT.local.md`.
