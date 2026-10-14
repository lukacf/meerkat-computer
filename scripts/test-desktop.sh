#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
case "${DESKTOP_TEST_SUITE:-native}" in
  native) suite=(python scripts/desktop_smoke.py --evidence /tmp/desktop-evidence) ;;
  pdf) suite=(python tests/desktop/pdf_smoke.py --evidence /tmp/desktop-evidence/pdf) ;;
  *) printf 'DESKTOP_TEST_SUITE must be native or pdf\n' >&2; exit 2 ;;
esac
docker_binary="${DOCKER:-docker}"
if ! command -v "${docker_binary}" >/dev/null 2>&1 && [[ -x /usr/local/bin/docker ]]; then
  docker_binary=/usr/local/bin/docker
fi
"${docker_binary}" info >/dev/null
image="${DESKTOP_TEST_IMAGE:-meerkat-computer-desktop-test:local}"
container="meerkat-computer-desktop-smoke-$(date +%s)-$$"
evidence="${DESKTOP_TEST_EVIDENCE:-$(mktemp -d "${TMPDIR:-/tmp}/meerkat-desktop-evidence.XXXXXX")}"
mkdir -p "${evidence}"
cleanup() {
  "${docker_binary}" rm -f "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# Explicit build context excludes state directories, credentials and neighboring repos.
tar -C "${repo_root}" -cf - pyproject.toml uv.lock README.md LICENSE python tests/desktop \
  scripts/desktop_smoke.py infra/desktop-session.py |
  "${docker_binary}" build --tag "${image}" --file tests/desktop/Dockerfile -
"${docker_binary}" image inspect "${image}" --format '{{.Id}}' > "${evidence}/image-id.txt"

# Private IPC and shared memory, non-root UID, no host mounts or additional capabilities.
"${docker_binary}" create --name "${container}" --init --shm-size=512m \
  --security-opt "seccomp=${repo_root}/tests/desktop/seccomp_profile.json" \
  --security-opt no-new-privileges --cap-drop=ALL "${image}" "${suite[@]}" >/dev/null
set +e
"${docker_binary}" start --attach "${container}"
status=$?
set -e
"${docker_binary}" cp "${container}:/tmp/desktop-evidence/." "${evidence}" || true
printf 'Desktop smoke evidence: %s\n' "${evidence}"
exit "${status}"
