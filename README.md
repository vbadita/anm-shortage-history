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
