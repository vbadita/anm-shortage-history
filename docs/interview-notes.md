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

## Phase 3 — Container
**Why a multi-stage build when nothing is compiled?**
The runtime stage receives only the finished venv: no pip cache, no requirements file, no install-time state.
With compiled dependencies it also keeps compilers out of the image. Each stage starts from the same pinned base,
so the venv's absolute paths and Python ABI match.

**Why a numeric UID in `USER`?**
Kubernetes `runAsNonRoot: true` checks the UID before starting the container; with a user *name* it cannot know
the name maps to non-zero and refuses to run it. A fixed UID also makes volume ownership predictable.

**Why is `docker images` bigger than the sum of the layers?**
With the containerd image store, Docker keeps the compressed layer blobs (what registries store and nodes pull)
and the unpacked snapshots. 48.7 MB compressed + 175 MB unpacked ≈ the 224 MB shown. Optimise the compressed size
for pull time, the unpacked size for node disk.

## Phase 4 — Kubernetes
**What happens to the data when a pod dies? When is it actually lost?**
Nothing: the PVC is a separate object bound to a PV; a new pod mounts the same volume (we killed a backfill pod
mid-run, the replacement continued from row 15). Data is lost when the PVC is deleted and the StorageClass has
`reclaimPolicy: Delete` — or when the node holding a local volume is lost.

**Your container takes 30 s to stop. Why?**
The process is PID 1 and has no SIGTERM handler; the kernel does not apply default signal actions to init, so
SIGTERM is ignored and the kubelet SIGKILLs after `terminationGracePeriodSeconds`. Fix: handle SIGTERM (or run
under an init like tini). Exec-form ENTRYPOINT keeps your process as PID 1 and receiving the signal.

**`concurrencyPolicy: Forbid` — is that enough to guarantee a single writer?**
No. It only prevents a CronJob overlapping with itself. Another Job, a manual `create job --from`, or a
replacement pod created while the old one is still terminating can all overlap. Enforce it where the data is
(a lock), and make the job idempotent.

**Why requests but no CPU limit?**
Requests reserve capacity for scheduling; a CPU limit enforces CFS throttling even when the node is idle, which
only slows a short batch job. Memory is different: it cannot be throttled, so a memory limit is what protects the
node (the container is OOM-killed instead).

## Phase 5 — Prometheus, Grafana, alerts
**What does `honor_labels: true` do and why does the Pushgateway need it?**
When a scraped series has a label the scrape config also sets (job, instance), Prometheus keeps its own and renames
the scraped one to `exported_<label>`. For a Pushgateway the scraped labels are the meaningful ones (the job that
pushed), so `honor_labels` keeps them. Only use it for targets you trust, since they can then set any label.

**How do you alert that a daily job "stopped working" with Prometheus?**
Export a "last success" timestamp, alert on its age (`time() - ts > threshold`), and add an `absent()` alert for the
case where the series disappears (absent data makes threshold alerts silently not fire). Pair with a Job-failure
alert from kube-state-metrics for fast detection; the freshness alert catches everything else (suspended CronJob,
scheduler problems, pushes failing).

**How do you test alert rules?**
`promtool test rules` with synthetic series and expected alerts at given times, run in CI with the same Prometheus
version as production. Check that tests can fail (mutation test). Watch the 5-minute lookback delta when choosing
sample intervals.
