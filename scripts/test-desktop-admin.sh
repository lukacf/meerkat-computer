#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
docker_binary="${DOCKER:-docker}"
if ! command -v "${docker_binary}" >/dev/null 2>&1 && [[ -x /usr/local/bin/docker ]]; then
  docker_binary=/usr/local/bin/docker
fi
image="${DESKTOP_ADMIN_IMAGE:-meerkat-computer-desktop-admin-test:local}"
container="meerkat-computer-desktop-admin-$(date +%s)-$$"
cleanup() {
  "${docker_binary}" rm -f "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
tar -C "${repo_root}" -cf - tests/desktop/Dockerfile.admin tests/desktop/sudo_smoke.py src/backend.rs |
  "${docker_binary}" build --tag "${image}" --file tests/desktop/Dockerfile.admin \
    --build-arg "DESKTOP_TEST_IMAGE=${DESKTOP_TEST_IMAGE:-meerkat-computer-desktop-test:local}" -
# This separate guest-admin test needs normal setuid semantics. It has only
# Docker's default capabilities, no privileged mode, mounts or network access.
"${docker_binary}" run --rm --name "${container}" --init --network none "${image}"
