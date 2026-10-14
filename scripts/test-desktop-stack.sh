#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
docker_binary="${DOCKER:-docker}"
if ! command -v "${docker_binary}" >/dev/null 2>&1 && [[ -x /usr/local/bin/docker ]]; then
  docker_binary=/usr/local/bin/docker
fi
image="${DESKTOP_STACK_IMAGE:-meerkat-computer-desktop-stack:local}"
container="meerkat-computer-desktop-stack-$(date +%s)-$$"
source_stage=""
cleanup() {
  "${docker_binary}" rm -f "${container}" >/dev/null 2>&1 || true
  if [[ -n "${source_stage}" ]]; then
    rm -rf -- "${source_stage}"
  fi
}
trap cleanup EXIT INT TERM
if [[ "${DESKTOP_STACK_SKIP_BUILD:-0}" != 1 ]]; then
  tar -C "${repo_root}" -cf - Cargo.toml Cargo.lock rust-toolchain.toml README.md LICENSE \
    src ui/preview.html ui/preview.js ui/preview.css \
    ui/credentials.html ui/credentials.js ui/credentials.css tests/stack.rs tests/support \
    scripts/repo-cargo scripts/mcp_smoke.py scripts/preview_smoke.py scripts/credential_smoke.py \
    configs tests/desktop/Dockerfile.stack |
    "${docker_binary}" build --tag "${image}" --file tests/desktop/Dockerfile.stack \
      --build-arg "DESKTOP_TEST_IMAGE=${DESKTOP_TEST_IMAGE:-meerkat-computer-desktop-test:local}" -
fi
"${docker_binary}" create --name "${container}" --init --shm-size=512m \
  --security-opt "seccomp=${repo_root}/tests/desktop/seccomp_profile.json" \
  --security-opt no-new-privileges --cap-drop=ALL "${image}" >/dev/null
# Stage only the public test source. Docker cp preserves private host modes,
# which the unprivileged guest cannot read, so normalize this copy like the image.
# Host source permissions and private state remain untouched.
source_stage="$(mktemp -d "${TMPDIR:-/tmp}/meerkat-computer-stack-source.XXXXXX")"
tar -C "${repo_root}" -cf - python src configs tests/support \
  Cargo.toml Cargo.lock rust-toolchain.toml README.md LICENSE tests/stack.rs \
  scripts/repo-cargo scripts/mcp_smoke.py scripts/preview_smoke.py scripts/credential_smoke.py \
  infra/desktop-session.py \
  ui/preview.html ui/preview.js ui/preview.css ui/credentials.html ui/credentials.js ui/credentials.css |
  tar -C "${source_stage}" -xf -
chmod -R a+rX "${source_stage}"
"${docker_binary}" cp "${source_stage}/." "${container}:/opt/meerkat-computer/"
"${docker_binary}" start --attach "${container}"
