# Native Linux qualification

Run `make smoke-desktop` with a local Docker daemon. The explicit lane builds an
unprivileged Linux test image, creates one private Xvfb/Openbox desktop and AT-SPI
bus, and removes only its uniquely named test container. No host desktop, host
mounts, cloud account or Incus daemon is used. Evidence is copied to a temporary
host directory, or `DESKTOP_TEST_EVIDENCE` when specified.

The GTK3 fixture has editable text, buttons, a menu and a second dialog window.
The smoke checks native semantic actions, screen coordinates, pixels, stale
observations, browser/native surface switching and grant fencing. It also
installs and launches Blender, Inkscape, GIMP and LibreOffice. Blender's accessible
tree may be sparse; the report records observed elements and verifies rendered
screenshots and keyboard window closure separately. A launch whose post-launch
observation fails is recorded as uncertain and is never replayed; the smoke
continues by observing the existing native window.

Blender work uses its built-in Python API as the primary control path. Through
`workspace_write` the smoke writes a scene script, then uses ordinary
`computer_act` inputs to focus the 3D viewport, open the Python console with
`Shift+F4`, and run the file with a short `exec(compile(...))` command. The `bpy`
script renames, scales and rotates the default cube and saves
`blender-python.blend`. The smoke verifies a receipt written by
Blender's interpreter, returns to the viewport with `Shift+F5`, and captures
`blender-python.png` to verify the visible scene. `blender.png` remains the before
image, and the existing keyboard closure check still runs. This uses the same
desktop tools; no Blender service or additional model tool is introduced.

The production MCP adapter is exercised through a test-only binding transport
bridge to the real worker. That bridge is not a substitute for broker authority
tests; `tests/stack.rs` covers the Rust broker and its identity/grant lifecycle.

After building the desktop image, run `scripts/test-desktop-stack.sh` for the
actual Rust broker, production MCP adapter and real native desktop together. It
builds a separate Linux Rust dependency layer and uses a 1280x800 desktop. A
focused source rerun can use `DESKTOP_STACK_SKIP_BUILD=1`; current package and
test sources are copied into the temporary container before the test starts.
Rebuild when dependency manifests or the base image change.

`scripts/test-desktop-admin.sh` is a separate Linux sudo qualification. It extracts
the exact guest bootstrap from `src/backend.rs`, uses a synthetic private worker
credential and verifies non-root execution, opt-in sudo, sudo revocation and
credential preservation. Its disposable guest uses Docker's default capabilities
and normal setuid semantics, without network, mounts or privileged mode. This is
guest sudo evidence, not Incus isolation evidence. The UI lanes retain their
stricter no-new-privileges and dropped-capability settings.

`seccomp_profile.json` starts from the Microsoft Playwright v1.63.0 Docker
profile, from
<https://github.com/microsoft/playwright/blob/v1.63.0/utils/docker/seccomp_profile.json>.
The upstream license is retained in `PLAYWRIGHT-LICENSE`.
It retains the syscall allowlist and enables user namespace creation for the
Chromium sandbox. The test profile also allows `chroot` for the sandbox's private
user namespace because dropping all container capabilities removes Docker's
capability-conditioned `chroot` rule. Kernel capability checks still apply.
The container runs without added capabilities or privileged
mode, with private IPC and a 512 MiB shared-memory allocation. See
<https://playwright.dev/python/docs/docker> for the upstream Docker guidance.
