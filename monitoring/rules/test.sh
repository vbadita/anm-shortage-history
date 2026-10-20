#!/usr/bin/env bash
# Lint + unit-test the PrometheusRule with promtool, using the same Prometheus
# version the cluster runs (kube-prometheus-stack 91.9.0 -> v3.15.0).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-$HERE/../../.venv/bin/python}"
IMAGE="quay.io/prometheus/prometheus:v3.15.0@sha256:efd719c99d83b060d9daefdcf00360461adf279f45ef5391f8d111892118753e"

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
# promtool reads plain rule files: extract .spec from the PrometheusRule object.
"$PYTHON" -c 'import sys, yaml; yaml.safe_dump(yaml.safe_load(open(sys.argv[1]))["spec"], open(sys.argv[2], "w"), sort_keys=False)' \
  "$HERE/anm-rules.yaml" "$work/anm-rules.spec.yaml"
cp "$HERE/tests.yaml" "$work/"
chmod -R a+rX "$work"   # promtool runs as nobody inside the container

promtool() { docker run --rm -v "$work:/rules:ro" -w /rules --entrypoint promtool "$IMAGE" "$@"; }
promtool check rules anm-rules.spec.yaml
promtool test rules tests.yaml
