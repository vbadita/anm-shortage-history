#!/usr/bin/env bash
# Install pinned CLI tools for linux/arm64 into $BIN_DIR.
# Every artifact is verified against a hard-coded SHA-256: a re-uploaded
# release asset with the same tag fails the install instead of passing silently.
set -euo pipefail

BIN_DIR="${BIN_DIR:-/usr/local/bin}"
ARCH="arm64"
GH="https://github.com"

# name | version | url | sha256 | path of binary inside archive ("-" = raw binary)
TOOLS=(
  "kind|v0.33.0|$GH/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-$ARCH|20022bee6cfcd5086cb7234d218e3454e6090022f2a8f55d1fa7fcf42c3867a2|-"
  "kubectl|v1.37.1|https://dl.k8s.io/release/v1.37.1/bin/linux/$ARCH/kubectl|ff749f4b78d9c4f1ec87307df9b50119ed819e2094aa9810cb9acffc3286c8c7|-"
  "helm|v4.3.0|https://get.helm.sh/helm-v4.3.0-linux-$ARCH.tar.gz|31c5794dd55c66a51e6b7d2e2ac7a114ae8b1de41ff1d9ba51748ac973b06a08|linux-$ARCH/helm"
  "k9s|v0.51.0|$GH/derailed/k9s/releases/download/v0.51.0/k9s_Linux_$ARCH.tar.gz|3ee05c82e5f9198928a4e86133608ba6a2c10a2244d6a7789e820f78319d640c|k9s"
  "kubectx|v0.11.0|$GH/ahmetb/kubectx/releases/download/v0.11.0/kubectx_v0.11.0_linux_$ARCH.tar.gz|1dac2072216689e773325cb7587b858ea7415706ebcf04e23bf80e7e70555340|kubectx"
  "kubens|v0.11.0|$GH/ahmetb/kubectx/releases/download/v0.11.0/kubens_v0.11.0_linux_$ARCH.tar.gz|37058abe82ef20c93b44f3dc8ca2382dbb95d416cec958fbf7b8f79f011be86c|kubens"
  "stern|v1.34.0|$GH/stern/stern/releases/download/v1.34.0/stern_1.34.0_linux_$ARCH.tar.gz|e215cfc5e42d71e93b77d3fac8f0df7d736271f44b2d92a5b417eaa588edff3a|stern"
  "jq|1.8.2|$GH/jqlang/jq/releases/download/jq-1.8.2/jq-linux-$ARCH|8b85c817833814ddca00a144c33705546355afccf0cf39b188f3cdb48b852309|-"
)

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for entry in "${TOOLS[@]}"; do
  IFS="|" read -r name version url sha inner <<<"$entry"
  echo "==> $name $version"
  curl -fsSL --retry 3 -o "$tmp/$name.dl" "$url"
  echo "$sha  $tmp/$name.dl" | sha256sum --check --quiet
  if [[ "$inner" == "-" ]]; then
    src="$tmp/$name.dl"
  else
    mkdir -p "$tmp/$name.x"
    tar -xzf "$tmp/$name.dl" -C "$tmp/$name.x" "$inner"
    src="$tmp/$name.x/$inner"
  fi
  sudo install -m 0755 "$src" "$BIN_DIR/$name"
done
echo "All tools installed to $BIN_DIR"
