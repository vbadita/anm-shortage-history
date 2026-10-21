# ANM medicine discontinuity history

Collects every historical version of the ANM "Notificări discontinuitate medicamente"
file and runs the collector on Kubernetes as a monitored daily job.

## Toolchain (Phase 0, linux/arm64)

| Tool | Version | Installed via |
|---|---|---|
| Python | 3.12.15 | dnf (`python3.12`) |
| Docker Engine / CLI | 29.9.1 | dnf, Docker CE repo, `dnf versionlock` |
| containerd / buildx | 2.4.2 / 0.38.0 | dnf, `dnf versionlock` |
| kind | v0.33.0 | `scripts/install-tools.sh` (SHA-256 pinned) |
| kubectl | v1.37.1 | `scripts/install-tools.sh` |
| Helm | v4.3.0 | `scripts/install-tools.sh` |
| k9s | v0.51.0 | `scripts/install-tools.sh` |
| kubectx / kubens | v0.11.0 | `scripts/install-tools.sh` |
| stern | v1.34.0 | `scripts/install-tools.sh` |
| jq | 1.8.2 | `scripts/install-tools.sh` |

Install the CLI tools: `./scripts/install-tools.sh` (override target with `BIN_DIR=...`).

## Phase 1 — Collector (`app/anm_collector`)

```
            ┌──────────── discovery ────────────┐
CDX prefix ─┤ 200-captures under the folder     │
CDX page  ──┤ 52 archived page versions → links ├─► data/candidates.csv
live page ──┤ current link                      │
known     ──┤ names verified by hand            │
            └───────────────────────────────────┘
                              │ per candidate
       live anm.ro ──404/err──► web.archive.org/web/<ts>id_/<url>
                              │   (skipped when the CDX digest = base32(sha1) we already hold)
       magic bytes? ── no ──► invalid      sha256 seen? ── yes ──► duplicate
                              │
       data/raw/<YYYY-MM-DD>_<sha256[:12]>.<ext>  (write tmp + os.link: atomic, never overwrites)
                              │
       data/manifest.csv  (append-only, fsync per row)
```

Run locally:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r app/requirements-dev.txt
(cd app && ../.venv/bin/pytest -q && ../.venv/bin/ruff check .)
ANM_DATA_DIR=data PYTHONPATH=app .venv/bin/python -m anm_collector backfill   # once
ANM_DATA_DIR=data PYTHONPATH=app .venv/bin/python -m anm_collector sync       # daily
ANM_DATA_DIR=data PYTHONPATH=app .venv/bin/python -m anm_collector report
```

| Env var | Default | Meaning |
|---|---|---|
| `ANM_DATA_DIR` | `data` | manifest, candidates, `raw/` |
| `ANM_MIN_INTERVAL_SECONDS` | `2.0` | global gap between requests (values < 2 are rejected) |
| `ANM_HTTP_TIMEOUT_SECONDS` | `60` | read timeout per request |
| `ANM_MAX_RETRIES` | `4` | retries for network errors, 429 and 5xx (exp. backoff 2·2ⁿ s + jitter, honours `Retry-After`) |
| `ANM_USER_AGENT` | project UA with repo URL | |
| `ANM_PUSHGATEWAY_URL` | unset | used from Phase 2 |
| `ANM_STORAGE_BACKEND` | `local` | `s3` arrives in Phase 9 |

Exit codes: `0` ok · `1` a file could not be fetched or was invalid · `2` fatal (config, CDX or live page unreachable).

Manifest statuses: `new` (stored), `duplicate` (same sha256 already stored), `invalid` (failed the magic-byte check),
`failed` (HTTP/network error). A URL that 404s live and has no Wayback capture is reported as `not_found`
(a fact about the source, not a job failure). `wayback_digest` is an extra column: base32(SHA-1), the same
value CDX reports, so archived captures of bytes we already hold are skipped without a request.

**Result of the first backfill (2026-10-20):** 68 unique versions, 2017-07-10 → 2026-10-09, all PDF,
23 MB, all still served live by anm.ro. Discovery: 36 only via archived page links, 18 only via the CDX
folder search, 10 via both, 4 via the known list (2 of them found nowhere else). Biggest gap: 288 days
(2018-05-31 → 2019-03-15).

### Known limitations
- `sync` detects a new file by **URL**. If ANM replaced a file's content under the same name, sync would not
  notice (backfill would, via differing Wayback digests).
- The page is archived from 2017-07; any versions published before that, or under an older page URL, are not covered.
- Gaps in the history are gaps in what was *linked or archived*, not necessarily in what ANM published.

## Phase 2 — Metrics (Pushgateway)

A `sync` run lives ~1 s; Prometheus pulls every 15–60 s from stable targets. So the job **pushes** its
metrics at the end of the run to the Pushgateway, which keeps them; Prometheus scrapes the Pushgateway.

Grouping key: `{job="anm_collector", mode="sync|backfill"}` — each mode has its own slot.
Push method: **POST (pushadd)** — only the metric names present in the push are replaced. A failed run
does not include `anm_last_success_timestamp_seconds` (the gauge is unregistered, because an unset
unlabelled gauge would export `0`), so the last real success survives and keeps ageing.

| Metric | Type | Meaning |
|---|---|---|
| `anm_requests_total{source,status_code}` | counter (per run) | HTTP attempts incl. retries; `source` = live, cdx, wayback; `status_code="error"` = network error |
| `anm_request_duration_seconds{source}` | histogram | request latency, buckets 0.1 s … 120 s |
| `anm_files_discovered` | gauge | candidate URLs found this run (sync: links on the live page) |
| `anm_versions_unique` | gauge | distinct versions stored (manifest rows with status `new`) |
| `anm_downloads_total{source,result}` | counter (per run) | result = new, duplicate, failed, invalid; all 8 series always exported (explicit 0) |
| `anm_last_success_timestamp_seconds` | gauge | set **only** when the run succeeds |
| `anm_last_new_version_timestamp_seconds` | gauge | newest `new` row in the manifest (survives Pushgateway restarts) |
| `anm_run_duration_seconds` | gauge | wall time of the run |
| `push_time_seconds` / `push_failure_time_seconds` | gauge | added by the Pushgateway itself |

Exit code `3`: the run itself was fine but the metrics push failed (a data failure, 1 or 2, takes precedence).

Local Pushgateway:
```bash
docker run -d --name pushgateway -p 127.0.0.1:9091:9091 \
  prom/pushgateway@sha256:74fa117cef2d7e383112d25139ff1c2d2e309c35389a9e0554a47136a1482e48   # v1.11.3
ANM_PUSHGATEWAY_URL=http://localhost:9091 ANM_DATA_DIR=data PYTHONPATH=app .venv/bin/python -m anm_collector sync
curl -s localhost:9091/metrics | grep ^anm_
curl -X DELETE localhost:9091/metrics/job/anm_collector/mode/sync   # drop a group
```

**Pushgateway downsides (and what we do about them):** values never expire → alert on the *age* of
`last_success`, not its presence; no `up` for the job → freshness + kube-state-metrics alerts (Phase 5);
per-run counters → read them as "last run", never `rate()`; data lost on restart / single point of failure →
`absent()` alert (Phase 5), Phase 7 case 3.

## Phase 3 — Container image

`Dockerfile` (repo root), two stages on `python:3.12.15-slim-trixie` pinned by multi-arch index digest:
**builder** creates `/opt/venv` and installs `app/requirements.txt` with `--no-deps` (exactly the lock);
**runtime** copies only the venv and `app/anm_collector`, precompiles `.pyc` (the root filesystem is
read-only at runtime) and runs as UID/GID **10001** (numeric, so Kubernetes `runAsNonRoot` can verify it).
`.dockerignore` is an allow-list: the build context is ~40 kB, `data/`, `.venv/`, `.git/` never leave the host.

```bash
SHA=$(git rev-parse --short HEAD)
docker build --build-arg GIT_SHA=$SHA -t anm-collector:$SHA .
docker volume create anm-data          # empty named volume inherits /data ownership (10001) from the image
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges \
  --network host -e ANM_PUSHGATEWAY_URL=http://localhost:9091 \
  -v anm-data:/data anm-collector:$SHA sync
```

| Layer | Size | Content |
|---|---|---|
| Debian 13 rootfs | 108 MB | base |
| ca-certificates, netbase, tzdata | 4.4 MB | base |
| CPython 3.12.15 (built from source, build deps purged in the same layer) | 43.8 MB | base |
| user `anm` + `/data` | 33 kB | ours |
| `/opt/venv` (requests, urllib3, idna, charset-normalizer, certifi, prometheus_client, pip) | 17.7 MB | ours |
| `app/anm_collector` + `.pyc` | 1.3 MB | ours |

Sizes: **48.7 MB compressed** (pushed/pulled), 175 MB unpacked. `docker images` shows 224 MB because the
containerd image store keeps both the compressed blobs and the unpacked snapshot.

A bind-mounted host directory owned by another UID fails with `PermissionError` → logged as JSON
`run_fatal phase=storage`, exit 2 (Kubernetes solves ownership with `fsGroup`).

## Phase 4 — Kubernetes (kind, plain YAML)

```
Namespace anm  (Pod Security Admission: enforce=restricted)
 ├─ ConfigMap anm-config ──envFrom──┐
 ├─ PVC anm-data (1Gi, RWO) ──mount /data──┐
 ├─ Job anm-backfill ──► Pod ──────────────┤   once
 └─ CronJob anm-sync ──► Job ──► Pod ──────┘   09:17 Europe/Bucharest
```

```bash
kind create cluster --config k8s/cluster/kind-config.yaml        # kind v0.33.0, node v1.37.0 (digest)
SHA=$(git rev-parse --short HEAD); docker build --build-arg GIT_SHA=$SHA -t anm-collector:$SHA .
kind load docker-image anm-collector:$SHA --name anm              # no registry yet: copy into the node
kubectl apply -f k8s/00-namespace.yaml && kubectl apply -f k8s/   # namespace must exist first
kubectl -n anm create job sync-now --from=cronjob/anm-sync        # run the CronJob on demand
kubectl apply -f k8s/tools/pvc-inspect.yaml && kubectl -n anm logs -f pvc-inspect   # look at /data
```

| Object | Key settings and why |
|---|---|
| Namespace | `pod-security.kubernetes.io/enforce: restricted` — the API server rejects pods that are not non-root, capability-free, seccomp-confined (proven with a plain `kubectl run`: 4 violations, Forbidden) |
| ConfigMap | all `ANM_*` settings; injected with `envFrom` |
| PVC | RWO, 1Gi, `standard` (local-path: a directory on the node, `reclaimPolicy: Delete`, `WaitForFirstConsumer`) |
| Job `anm-backfill` | `backoffLimit: 2`, `activeDeadlineSeconds: 3600`, `ttlSecondsAfterFinished: 7d`, `restartPolicy: Never` (failed pods stay for logs) |
| CronJob `anm-sync` | `17 9 * * *`, `timeZone: Europe/Bucharest` (09:17 local = 06:17 UTC now, 07:17 UTC after DST ends), `concurrencyPolicy: Forbid`, `startingDeadlineSeconds: 3600`, history 3/3, job `backoffLimit: 2`, `activeDeadlineSeconds: 900` |
| Pod security | `runAsNonRoot`, UID/GID 10001, `fsGroup` 10001, seccomp `RuntimeDefault`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, drop `ALL` capabilities, no service-account token |
| Resources | requests 50m CPU / 64Mi; limit 256Mi memory; **no CPU limit** (a limit only throttles a 1-second batch job; the request already reserves its share). Measured peak during backfill: 40 MiB |

**What happens to data when a pod dies** (demonstrated): the PVC and its files outlive any pod. Killing the
backfill pod after 15 downloads → the Job created a new pod, which started with `manifest_rows: 15` and downloaded
the remaining 53 (15 + 53 = 68, nothing twice). Deleting the **PVC** is what destroys data: with
`reclaimPolicy: Delete` the PV and its directory on the node were removed (68 files → gone).

**Graceful shutdown:** Python as PID 1 ignores SIGTERM (Linux gives init no default signal actions). Before the
fix a deleted pod kept downloading for 30 s until the kubelet sent SIGKILL. Now a SIGTERM handler sets a stop flag
checked before every request: `run_terminated` was logged 108 ms after the signal, the pod was gone in 1.1 s.

**Single writer:** the backfill Job and the sync CronJob share the PVC; `concurrencyPolicy` only stops a CronJob
overlapping *itself*. A `flock` on `/data/.lock` makes a second run exit 2 (`lock_held`). During the kill test
the Job created the replacement pod while the old one was still terminating — exactly the overlap the lock covers.

### Known limitations (local cluster)
- local-path does **not enforce** the PVC size (`df` in the pod shows the host's 30 GB disk); "PVC full" must be simulated.
- The kind node has 1 CPU and the control plane already requests 950m: only one of our pods fits at a time.
  Monitoring components in Phase 5 need reduced CPU requests.
- `flock` protects writers on one node (RWO). It is not reliable on NFS/RWX volumes.

## Phase 5 — Prometheus, Grafana, alert rules

```
CronJob pod ──push──► Pushgateway (PVC + --persistence.file) ◄──scrape, honor_labels──┐
kube-state-metrics (Job/CronJob state) ◄─────────────────────────────scrape────────────┤
                                       Prometheus v3.15.0 ── PrometheusRule anm-collector ──► Alertmanager
                                            ▲
                       Grafana 13.2.3 ──────┘  dashboard "ANM collector" from ConfigMap anm-dashboard
```

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
kubectl create namespace monitoring
kubectl -n monitoring create secret generic grafana-admin \
  --from-literal=admin-user=admin --from-literal=admin-password="$(openssl rand -base64 24)"   # not in Git
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack --version 91.9.0 \
  -n monitoring -f monitoring/kube-prometheus-stack-values.yaml --wait
helm upgrade --install prometheus-pushgateway prometheus-community/prometheus-pushgateway --version 3.9.0 \
  -n monitoring -f monitoring/pushgateway-values.yaml --wait
kubectl apply -f monitoring/rules/anm-rules.yaml
kubectl -n monitoring create configmap anm-dashboard --from-file=monitoring/dashboards/anm-collector.json \
  --dry-run=client -o yaml | kubectl label --local -f - grafana_dashboard=1 -o yaml | kubectl apply -f -
monitoring/rules/test.sh        # promtool check + unit tests (Prometheus v3.15.0 image, pinned by digest)
```

Chart versions: kube-prometheus-stack **91.9.0** (last of the 91.x major; 92.0.0 was a fresh major exactly at
the 2-week cut-off), prometheus-pushgateway **3.9.0** (Pushgateway v1.11.3).

Sizing for the 1-CPU kind node: CoreDNS scaled to 1 replica (no HA on one node anyway), every component has
a small explicit CPU request (requests are scheduling reservations; total for monitoring ≈ 75m), no CPU limits.
kind's etcd/scheduler/controller-manager/kube-proxy listen on 127.0.0.1 inside the node, so their scrape jobs
and rules are disabled (they would be permanently DOWN). Result: 14/14 targets `up`.

**`honor_labels: true`** on the Pushgateway ServiceMonitor: series keep the labels the job pushed
(`job="anm_collector", mode="sync"`). Without it Prometheus resolves the conflict by renaming them to
`exported_job` and stamping `job="prometheus-pushgateway"` on everything — every pushing job would look the same.

**Pushgateway persistence:** the chart mounts a PVC at `/data` but only `--persistence.file` makes the process use
it (found after the first install). Verified: value identical before and after deleting the pod.

### Alert rules (`monitoring/rules/anm-rules.yaml`), in plain words

| Alert | PromQL (simplified) | In plain words | Severity |
|---|---|---|---|
| AnmSyncStale | `time() - max by (job, mode) (anm_last_success_timestamp_seconds{mode="sync"}) > 26*3600` for 5m | "now" minus "when the last sync succeeded" is more than 26 hours | critical |
| AnmSyncMetricsMissing | `absent(anm_last_success_timestamp_seconds{mode="sync"})` for 30m | the success timestamp does not exist at all, so the rule above cannot fire | warning |
| AnmDownloadsFailing | `sum by (job, mode) (anm_downloads_total{result=~"failed\|invalid"}) > 0` | the last run counted at least one failed or invalid download (counters are per run) | warning |
| AnmJobFailed | newest CronJob Job with `kube_job_failed{condition="true"}`, **or** a failed Job without a CronJob owner | the most recent sync Job gave up (backoffLimit / deadline), or the backfill Job did | critical |
| AnmNoNewVersionLong | `time() - max(anm_last_new_version_timestamp_seconds{mode="sync"}) > 30*86400` for 1h | no new file for 30 days | info |

Recording rule `anm:cronjob_job_start_time_seconds` = `kube_job_status_start_time * on (namespace, job_name)
group_left (owner_name) kube_job_owner{owner_kind="CronJob"}`: "the start time of every Job, labelled with the
CronJob that created it". `kube_job_owner` is always 1, so the multiplication keeps the start time; `group_left`
copies `owner_name` across. AnmJobFailed then keeps only Jobs whose start time equals the max for their CronJob.

Why not `kube_job_status_failed > 0`? It counts failed **pods** — the Phase 4 backfill shows 1 although the Job
succeeded on retry. A mutation test (swapping in that expression) makes 3 promtool tests fail, including a false
critical alert for `anm-backfill`.

**AnmNoNewVersionLong and the data:** of 67 historical gaps between versions, 38 exceeded 30 days (median 36 d,
p90 108 d). At 30 days the alert mostly reports ANM's normal rhythm; ~90 days (9 of 67) would mean "unusual".
Kept at 30 days / info as specified.

Unit tests: `monitoring/rules/tests.yaml`, 11 cases (stale vs. daily successes, absent, failed+invalid, newest
vs. older CronJob Jobs, standalone Job, 30-day gap). Lesson: test samples must be closer than the 5-minute
lookback delta, or the series is "absent" between samples and `for:` never completes.

### Dashboard "ANM collector" (`monitoring/dashboards/anm-collector.json`)

| Panel | PromQL | In plain words |
|---|---|---|
| Last successful sync (age) | `time() - max(anm_last_success_timestamp_seconds{mode="sync"})` | seconds since the last good sync; orange > 24h, red > 26h |
| Versions stored | `max(anm_versions_unique)` | distinct file versions on disk |
| Failed/invalid downloads | `sum(anm_downloads_total{result=~"failed\|invalid"})` | problems in the last run; red ≥ 1 |
| Last new version (age) | `time() - max(anm_last_new_version_timestamp_seconds{mode="sync"})` | how long since ANM published something new |
| Run duration | `anm_run_duration_seconds` | wall time of the last run, per mode |
| Versions over time | `max(anm_versions_unique)` | the history growing, one step per new file |
| Requests by status code | `sum by (source, status_code) (anm_requests_total)` | what the last run asked and got back (live/cdx/wayback × 200/404/503/error) |
| Downloads by result | `sum by (result) (anm_downloads_total)` | new / duplicate / failed / invalid in the last run |
| Latency p50/p95 | `histogram_quantile(0.95, sum by (le, source) (anm_request_duration_seconds_bucket))` | estimated request latency; interpolated inside buckets, so with few samples it is only as precise as the bucket bounds |

## Public access: https://apps.badita.com/istoric-lipsa-medicament-vaccin/ (search) and /anm/grafana/ (Grafana)

```
Browser ──TLS (Cloudflare edge cert)──► Cloudflare ──TLS, Full (strict)──► VM :443 nginx 1.26 ──► 172.18.0.2:30300
                                        WAF / DDoS     Origin CA cert         firewalld: Cloudflare   kind node NodePort
                                                       (apps.badita.com,      IP ranges only           (Docker bridge,
                                                        valid to 2041)                                  not public)
```

Only Grafana is exposed (it has its own login; signup and anonymous access disabled, `cookie_secure`,
`cookie_samesite=strict`). Prometheus, Alertmanager and the Pushgateway have no authentication and stay private.

| Piece | Where | Notes |
|---|---|---|
| DNS | Cloudflare: `apps` A `130.61.172.251`, **proxied** | resolves to Cloudflare anycast IPs, not ours |
| TLS mode | Cloudflare SSL/TLS: **Full (strict)** | Cloudflare verifies the origin cert |
| Origin cert | `/etc/nginx/tls/apps.badita.com.{pem,key}` (root, key 0600) | Cloudflare Origin CA; never in Git |
| nginx site | `deploy/nginx/apps.badita.com.conf` → `/etc/nginx/conf.d/` | `/anm/` → Grafana, everything else 404; unknown SNI → handshake refused |
| Firewall | `deploy/nginx/update-cloudflare-ips.sh` + weekly `cloudflare-ips.timer` | ipsets `cloudflare-v4/-v6`; 443 accepted only from them; same list feeds nginx `set_real_ip_from` |
| SELinux | port 30300 → `http_port_t`, `httpd_can_network_relay=on` | narrower than `httpd_can_network_connect` (still off) |
| Grafana | `grafana.ini.server.root_url=https://apps.badita.com/anm/`, `serve_from_sub_path`, Service NodePort 30300 | in `monitoring/kube-prometheus-stack-values.yaml` |

Install (as root, after the Phase 5 Helm release):
```bash
dnf module enable nginx:1.26 && dnf install nginx && dnf versionlock add nginx nginx-core nginx-filesystem
install -d -m 0700 /etc/nginx/tls   # copy the Origin cert (0644) and key (0600) here
install -m 0644 deploy/nginx/apps.badita.com.conf /etc/nginx/conf.d/
install -m 0755 deploy/nginx/update-cloudflare-ips.sh /usr/local/sbin/
install -m 0644 deploy/nginx/cloudflare-ips.{service,timer} /etc/systemd/system/
semanage port -a -t http_port_t -p tcp 30300 && setsebool -P httpd_can_network_relay 1
systemctl daemon-reload && systemctl enable --now cloudflare-ips.timer && systemctl start cloudflare-ips.service
nginx -t && systemctl enable --now nginx
```

Verified: through Cloudflare `HTTP 200` with `cf-ray: …-FRA`; origin log shows the real client IP (realip);
`/`, `/prometheus` → 404; Grafana default password → 401; direct connections to `130.61.172.251:443` from
external probes (DE, NL, IR) → blocked, while the same probes reach port 22 (control).

Limitations: the upstream is the kind node's Docker bridge IP (changes if the cluster is recreated); the kind node
container has restart policy `on-failure` — survival across a host reboot is untested.

## Phase 5b — Parse the history and search it (Checkpoint A: data)

```
data/raw/*.pdf ──parse (pdfplumber, cell rectangles)──► data/anm.sqlite ──► search (CLI now, web next)
                  once per sha256 + PARSER_VERSION          versions, rows (47,894), 3,298 products
```

```bash
ANM_DATA_DIR=data PYTHONPATH=app .venv/bin/python -m anm_collector parse            # parse pending versions (~9 s each)
ANM_DATA_DIR=data PYTHONPATH=app .venv/bin/python -m anm_collector search fraxiparine  # brand, DCI or holder
```

Modes: `backfill` and `parse` parse everything pending; `sync` parses only what it downloaded (keeps it inside its
15-minute deadline). A version is parsed once per `PARSER_VERSION`; bumping it re-parses all versions. Exit code `4`:
a version's layout was not recognised (`LayoutChanged`) — the parser refuses to guess column meaning.

Quirks found by surveying all 68 versions and handled (each has a test on a real page in `app/tests/fixtures/`):

| Quirk | Versions | Handling |
|---|---|---|
| US `m/d/yyyy` dates (`1/12/2017` = 12 Jan) | 2019-06 … 2019-11, 2021-11-19 | format by separator; verified against the same row printed `12.01.2017` a version earlier |
| empty 12th column (extra border line) | 2020-08-28, 2024-04 … 06, … | trailing all-empty columns dropped |
| text overflowing into the date cell (`M 15.05.2026`) | many | the date is *searched* inside the cell |
| header split over two table rows | 2024-10-14 | header detected by content (parser v2) |
| U+2010 hyphen vs `-` (`anti‐factor`) | 2021-11 … 2023-01 | dash variants folded (parser v3): 3,328 → 3,298 products |
| micro sign `µ` → Greek capital mu after NFKD | all | folded to `u` |

Verification: in 61/68 versions the rows are exactly `Nr crt` 1…N; the other 7 were checked against the PDF text —
ANM's own numbering gap (2022-11-03 skips 544), ANM's duplicate number (2020-10-19 has 464 twice), and garbled
numbers from overlapping text; no row lost.

Product = folded name + form + strength. A name-only key would merge 69 genuinely different products
(VFEND 200 mg tablets vs infusion, SINGULAIR 4 mg chewable vs granules), so it was rejected.

**Known limitations:** truncated cells in a few versions (2024-02-14) create a separate one-version product;
"not in the current file" means *no longer listed* — ANM does not say whether the shortage ended or the entry was removed.

## Phase 5b — Checkpoint B: the search page

**https://apps.badita.com/istoric-lipsa-medicament-vaccin/** — search a medicine by brand name, DCI or holder; see whether it is in ANM's
current list and how its status changed across all 68 versions, with a link to each original ANM PDF.
Grafana moved to **https://apps.badita.com/anm/grafana/**.

```
Cloudflare ─► nginx :443 ─┬─ /anm/grafana/*  ─► Grafana  NodePort 30300
                          ├─ /istoric-lipsa-medicament-vaccin/* ─► anm-web  NodePort 30380 ─► /data/anm.sqlite (PVC, read-only)
                          └─ /anm/*          ─► 301 to the path above (old links)
CronJob anm-sync: download new PDF ─► parse it ─► append to anm.sqlite (one transaction)
Job anm-parse:    full rebuild into anm.sqlite.building ─► os.replace() (atomic swap)
```

| Piece | Details |
|---|---|
| Web app | `app/anm_collector/web/` — FastAPI 0.142.2 + Jinja2, server-rendered, no JavaScript; Romanian UI; `python -m anm_collector.web` |
| Routes | `/anm/` search, `/anm/produs?k=` history, `/anm/static/`; `/healthz` and `/metrics` outside `/anm` (not proxied, in-cluster only) |
| Security | Jinja2 autoescape (tested with `<script>`), CSP `default-src 'none'`, `X-Frame-Options: DENY`, query ≤ 100 chars, read-only root FS and read-only data volume, restricted PSA |
| Probes | readiness `/healthz` (database readable, ≥1 version) · liveness TCP only (restarting cannot fix a missing database) |
| Metrics | scraped directly by a ServiceMonitor (long-running → no Pushgateway): `anm_web_requests_total{route,status}`, `anm_web_request_duration_seconds`, `anm_web_latest_version_timestamp_seconds`, `process_*` |
| Alerts | `AnmWebDown` (`up == 0 or absent(up)` for 5m), `AnmParseFailing` (a downloaded version could not be parsed) |
| SQLite | rollback journal (not WAL): a WAL reader must write `-shm`, impossible on a read-only mount |
| Resources | web 10m / 96Mi, limit 256Mi (uses ~60 MiB). Parsing pods 25m / 128Mi, **limit 512Mi** — measured peak 312 MiB, the old 256Mi limit would have OOM-killed the first sync that parsed a new file |

Deploy (after the image is loaded into kind):
```bash
kubectl apply -f k8s/40-cronjob-sync.yaml -f k8s/50-job-parse.yaml -f k8s/60-web.yaml -f k8s/61-web-servicemonitor.yaml
kubectl -n anm wait --for=condition=Complete job/anm-parse --timeout=3600s     # ~17 min on the 1-CPU node
sudo semanage port -a -t http_port_t -p tcp 30380 && sudo install -m 0644 deploy/nginx/apps.badita.com.conf /etc/nginx/conf.d/
sudo nginx -t && sudo systemctl reload nginx
```

Dependency hygiene learned the hard way: the first web pod crashed with `No module named 'opentelemetry'` — the lock file
had been built from a hand-written package list. Now `scripts/lock.sh` resolves in a fresh venv and freezes everything,
the Docker build runs `pip check`, `app/constraints.txt` holds back transitive packages, and
`scripts/check-lock-age.sh` fails if any locked package is younger than 14 days (it caught 5).

Notes: `kubectl apply -f k8s/` fails on `30-job-backfill.yaml` once that Job exists (a Job's pod template is immutable);
apply files individually until Phase 8 (Kustomize/Argo CD) handles run-once Jobs. A request served during an nginx
reload may still use the old config (old workers finish requests on existing keep-alive connections).

**Path change (2026-10-21):** the search page moved from `/anm/` to `/istoric-lipsa-medicament-vaccin/` (set by `ANM_BASE_PATH`; every
template link uses it). nginx answers `/anm/...` with a permanent redirect to the same path and query under the new
prefix, so shared links keep working; Grafana stays at `/anm/grafana/` (longest prefix wins in nginx).

## Search engines (SEO)

| Piece | Where | Notes |
|---|---|---|
| `robots.txt` | nginx, domain root | `Allow: /` + `Sitemap:`; Cloudflare's managed robots.txt prepends its content-signal comments |
| `sitemap.xml` | `/istoric-lipsa-medicament-vaccin/sitemap.xml` (web app) | home + one URL per product (3,299); `lastmod` = newest ANM version listing the product |
| titles / descriptions | templates | product description summarises the current status (Google often uses it as the snippet) |
| canonical | home + product pages | same encoding as links and sitemap (tested), so one URL per page |
| `noindex, follow` | default in `base.html`; search results, 404 | only home and product pages are indexable |

Getting indexed (manual, needs the site owner's Google account): Google Search Console → add **Domain property**
`badita.com` (TXT record in Cloudflare DNS) → Sitemaps → submit the sitemap URL → URL Inspection → *Request indexing*
for the home page. Google's Indexing API is only allowed for job postings and livestreams. Bing/Yandex: enable
Cloudflare *Crawler Hints* (IndexNow).
