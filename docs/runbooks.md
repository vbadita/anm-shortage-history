# Runbooks

Each alert in `monitoring/rules/anm-rules.yaml` links here. First commands assume `kubectl` context `kind-anm`.

## AnmSyncStale
**Meaning:** no successful daily sync for > 26 h (freshness SLO). Severity: critical.
1. Is the CronJob running at all? `kubectl -n anm get cronjob anm-sync` — check `SUSPEND` and `LAST SCHEDULE`.
2. Recent Jobs and their status: `kubectl -n anm get jobs -l app.kubernetes.io/component=sync`.
3. Logs of the newest run: `kubectl -n anm logs job/<name>` — look at `run_end.result` and `exit_code`
   (1 download failed, 2 fatal, 3 metrics push failed).
4. If runs succeed but the alert fires: the push is failing (exit 3) or the Pushgateway lost state → AnmSyncMetricsMissing.
5. Recover: fix the cause, then `kubectl -n anm create job sync-now --from=cronjob/anm-sync`.

## AnmSyncMetricsMissing
**Meaning:** Prometheus has no `anm_last_success_timestamp_seconds{mode="sync"}`. AnmSyncStale cannot work. Severity: warning.
1. Pushgateway up? `kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus-pushgateway`; target `up{job="prometheus-pushgateway"}`.
2. Did it restart and lose state? It persists to `/data/pushgateway.data` every minute — check its PVC and args.
3. Fresh install before the first sync is expected to fire this: run a sync manually.

## AnmDownloadsFailing
**Meaning:** the last run had `failed` (HTTP/network) or `invalid` (not a PDF/XLSX) downloads. Severity: warning.
1. Which URLs? Logs: `{event="download", result=~"failed|invalid"}` (LogQL in Phase 6) or
   `kubectl -n anm logs job/<name> | jq 'select(.event=="download" and .result!="new")'`.
2. `invalid` with `content_type` text/html → the source served an error page; open the URL in a browser.
3. `failed` → check `anm_requests_total` by `status_code`; 5xx/timeouts point at the source (see AnmSourceDown, Phase 6).

## AnmJobFailed
**Meaning:** the newest Job of the CronJob, or a standalone Job (backfill), has condition Failed. Severity: critical.
1. `kubectl -n anm describe job <job_name>` — reason: `BackoffLimitExceeded` or `DeadlineExceeded`.
2. Failed pods are kept (`restartPolicy: Never`): `kubectl -n anm get pods -l job-name=<job_name>`;
   `kubectl -n anm logs <pod>`; for OOM: `kubectl -n anm get pod <pod> -o jsonpath='{.status.containerStatuses[0].lastState}'`.
3. The alert clears when a newer run of the same CronJob succeeds.

## AnmNoNewVersionLong
**Meaning:** no new file for 30 days. Severity: info (not paged). Often normal: median gap is ~36 days.
1. Open the page; is a file still linked? Does `sync` log `page_fetch` with a non-empty `links` list?
2. If the page links a different kind of file or a different folder, update discovery.

## AnmParseFailing
**Meaning:** a downloaded version could not be parsed (`LayoutChanged` or malformed PDF); the search page misses it. Severity: warning.
1. Logs: `kubectl -n anm logs job/<name> | jq 'select(.event=="parse" and .result!="ok")'` — the error says which header/row was unexpected.
2. Open the PDF (link in the log `url`). If ANM changed the table, adapt `parse.py`, add the page as a test fixture, bump `PARSER_VERSION`.
3. Rebuild: `kubectl -n anm delete job anm-parse --ignore-not-found && kubectl apply -f k8s/50-job-parse.yaml` (atomic swap; the old database keeps serving meanwhile).

## AnmWebDown
**Meaning:** Prometheus cannot scrape `anm-web` (or the target disappeared). Severity: critical.
1. `kubectl -n anm get deploy,pods -l app.kubernetes.io/name=anm-web` — CrashLoopBackOff? `kubectl -n anm logs deploy/anm-web --previous`.
2. Not Ready (readiness = `/healthz`): the database is missing or unreadable → run the parse Job.
3. Public side: `curl -I https://apps.badita.com/istoric-lipsa-medicament-vaccin/` — 502 from nginx means the NodePort/upstream; check `sudo tail /var/log/nginx/apps.badita.com.error.log` (SELinux denials show as `13: Permission denied`).
