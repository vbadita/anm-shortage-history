# Interview notes

## Phase 0 — Setup
**Why pin a SHA-256 for downloaded binaries, not just the version tag?**
A tag is a mutable pointer; a release asset can be re-uploaded. The hash pins the exact bytes, so a
tampered or swapped binary fails the install (`sha256sum --check` → non-zero exit → `set -e` stops).

**Why must kubectl be close to the cluster version?**
Kubernetes' version-skew policy supports kubectl within ±1 minor version of the API server. Outside that,
fields or API versions can silently differ.

**What is an RPM epoch and why did it matter?**
The `N:` prefix in `3:29.9.1` overrides version comparison. docker-ce and docker-ce-cli use different epochs
(3 vs 1), so pinning by version string alone failed for the CLI.

## Phase 1 — Collector
**How do you make a batch job idempotent?**
Key every unit of work by something stable (URL, content hash), record completed work durably before moving on
(append + fsync per row), and skip what is recorded. Re-running our backfill made zero downloads.

**How do you write a file so it is never half-written or overwritten?**
Write to a temp file in the same directory, fsync, then `os.link(tmp, final)`: the link is atomic and fails if
`final` exists. Readers see either nothing or the complete file.

**Which errors do you retry?**
Only those that can succeed later: network errors, timeouts, 429, 5xx — with exponential backoff, jitter (so
many clients don't retry in lockstep) and `Retry-After`. A 404 is a final answer; retrying it just adds load.

## Phase 2 — Metrics
**Why can't Prometheus scrape a CronJob directly, and what does the Pushgateway cost you?**
Pull needs a stable, long-lived target; a 1-second pod is neither. The Pushgateway holds the last pushed values,
but they never expire (stale data looks healthy), there is no `up` for the job, counters reset every run, and it
is a single point of failure. Alert on the age of a success timestamp, not on the presence of data.

**PUT vs POST to the Pushgateway?**
PUT replaces the whole group; POST replaces only the metric names in the request. With PUT, a failed run that
omits `last_success` would erase it and the freshness alert would see *no data* instead of an *old* timestamp.

**Why export explicit zeros, e.g. `anm_downloads_total{result="failed"} 0`?**
A missing series is not zero: `x > 0` on absent data returns nothing, and so does `x == 0`. Pre-creating the
label combinations makes "no failures" a measurable fact.
