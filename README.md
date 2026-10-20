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
