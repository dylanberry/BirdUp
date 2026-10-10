# Bird Up! — Open Work

Split out of `AGENTS.md` (2026-10-10). What is still outstanding on the live deployment.

---

## Open work

1. Generate the remaining ~120 Ontario species illustrations (priority list:
   `avian/scripts/ontario_toronto_labels.txt`; pipeline: avian/scripts/README.md).
2. Rotate credentials previously exposed in chat: Caddy `basic_auth` password,
   BirdWeather token.
3. Solar install (AGM chosen over LiFePO4 — LiFePO4 can't charge below 0 °C).
4. Optional: Perch v2 classifier trial (download model + labels to the Pi first;
   plan a fallback before switching `MODEL`).
5. Field-validate detection levels on the mic-node audio path (A/B vs old USB lav).
