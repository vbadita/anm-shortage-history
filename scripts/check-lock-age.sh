#!/usr/bin/env bash
# Fail if any pinned package in a lock file was released less than MIN_DAYS ago
# (new releases get a grace period for regressions / yanked uploads to surface).
# certifi (CA bundle data) is exempt: newer is safer.
set -euo pipefail
MIN_DAYS="${MIN_DAYS:-14}"; cutoff=$(date -u -d "-${MIN_DAYS} days" +%F); bad=0
for lock in "$@"; do
  while IFS='==' read -r name _ ver; do
    [[ -z "$name" || "$name" == -* || "$name" == "certifi" ]] && continue
    released=$(curl -fsS "https://pypi.org/pypi/$name/$ver/json" | jq -r '.urls[0].upload_time[:10]')
    if [[ "$released" > "$cutoff" ]]; then echo "TOO NEW  $name==$ver ($released)"; bad=1
    else echo "ok       $name==$ver ($released)"; fi
  done < "$lock"
done
exit $bad
