# Weather fixtures — provenance

A fixture whose origin is unstated becomes unverifiable the day the upstream
API drifts: you cannot tell a shape the API no longer produces from a shape it
never produced. So each capture records where it came from and when.

## `taf_cyhz_cyzx_cyaw_20260811.json`

| | |
|---|---|
| **Source** | `https://aviationweather.gov/api/data/taf?format=json` (production station set) |
| **Captured** | 2026-08-11, on-box, where the brief's fetch already runs legitimately |
| **Content** | Verbatim response. 3 records — CYHZ (11 forecast blocks), CYZX (8), CYAW (8) |

**Why these three.** They cover the block shapes the producer must handle
rather than only the common one:

* **CYHZ** — four `PROB` blocks (`probability: 30`), the case that decides
  whether a *possible* interval may become an item's asserted extent. It may
  not; see `weather_feed_items`.
* **CYZX** — a real `BECMG` block carrying `timeBec` (1786496400) strictly
  INSIDE its own `timeFrom`/`timeTo` window (1786489200 → 1786503600). This is
  the record that proves `timeBec` is a transition-completion moment and not a
  second spelling of the block's start.
* **CYAW** — a shorter validity window (12h vs 24h), so nothing can quietly
  assume a fixed forecast length.

**Shape notes.** All times are **epoch seconds**: `validTimeFrom`/`validTimeTo`
at record level, `timeFrom`/`timeTo` per block. `fcstChange` is absent on the
base block and one of `FM` / `TEMPO` / `PROB` / `BECMG` otherwise.

Re-capturing: replace the file and update the counts above in the same commit.
