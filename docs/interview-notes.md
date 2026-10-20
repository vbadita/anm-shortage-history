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
