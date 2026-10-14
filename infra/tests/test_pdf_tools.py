"""Exercise desktop PDF package selection without root, apt or network access."""

import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PDF_PACKAGES = {"evince", "poppler-utils"}


def selected_packages(tmp_path: Path, *arguments: str) -> set[str]:
    # Execute the real option parsing and package stage only. Every command in
    # this stage is intercepted; the rest of provisioning never runs on the host.
    source = (ROOT / "infra/provision-worker.sh").read_text()
    package_stage, boundary, _ = source.partition("\nid meerkat-computer ")
    assert boundary, "Provisioning package-stage boundary changed"
    trace = tmp_path / "apt-arguments.txt"
    stubs = """
id() { [[ "$*" == '-u' ]] && printf '0\\n'; }
test() {
    if [[ "$*" == '-f /root/worker-source.tgz' ]]; then return 0; fi
    builtin test "$@"
}
apt-get() { printf '%s\\n' "$*" >> "$PDF_PACKAGE_TRACE"; }
"""
    subprocess.run(
        ["/bin/bash", "-c", stubs + package_stage, "pdf-package-test", *arguments],
        check=True,
        env={"PATH": str(tmp_path / "no-external-commands"), "PDF_PACKAGE_TRACE": str(trace)},
        capture_output=True,
        text=True,
    )
    calls = [shlex.split(line) for line in trace.read_text().splitlines()]
    assert calls[0] == ["update"]
    return {argument for call in calls if call[0] == "install" for argument in call[1:]}


@pytest.mark.parametrize("bundle", ["minimal", "full"])
def test_each_desktop_bundle_installs_native_pdf_viewer_and_utilities(tmp_path, bundle):
    packages = selected_packages(tmp_path, "--desktop", "--apps", bundle)
    assert PDF_PACKAGES <= packages
    assert {"xvfb", "openbox", "at-spi2-core"} <= packages
    assert ("blender" in packages) == (bundle == "full")


@pytest.mark.parametrize("bundle", ["minimal", "full"])
def test_headless_image_does_not_install_pdf_desktop_packages(tmp_path, bundle):
    packages = selected_packages(tmp_path, "--apps", bundle)
    assert packages.isdisjoint(PDF_PACKAGES | {"xvfb", "openbox"})


def test_real_desktop_qualification_image_installs_the_same_pdf_packages():
    source = (ROOT / "tests/desktop/Dockerfile").read_text().replace("\\\n", " ")
    package_stage = source.split("RUN apt-get update && apt-get install", 1)[1]
    packages = set(shlex.split(package_stage.split("&&", 1)[0]))
    assert PDF_PACKAGES <= packages
