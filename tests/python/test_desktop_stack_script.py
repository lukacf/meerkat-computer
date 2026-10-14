"""The Linux test source overlay must be readable by its unprivileged guest."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def test_stack_overlay_normalizes_only_staged_public_source(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "scripts/test-desktop-stack.sh"
    smoke_scripts = sorted(
        set(
            re.findall(
                r'root.join\("(scripts/[^"]+\.py)"\)',
                (source.parents[1] / "tests/stack.rs").read_text(),
            )
        )
    )
    script = repo / "scripts/test-desktop-stack.sh"
    shutil.copyfile(source, script)
    paths = [
        "python/meerkat_computer/worker.py",
        "src/domain.rs",
        "configs/local.toml",
        "tests/support/local_worker.rs",
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "README.md",
        "LICENSE",
        "tests/stack.rs",
        "scripts/repo-cargo",
        *smoke_scripts,
        "infra/desktop-session.py",
        "ui/preview.html",
        "ui/preview.js",
        "ui/preview.css",
        "ui/credentials.html",
        "ui/credentials.js",
        "ui/credentials.css",
    ]
    for name in paths:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
        path.chmod(0o700 if name in ("scripts/repo-cargo", "infra/desktop-session.py") else 0o600)
    private = repo / ".state/worker.token"
    private.parent.mkdir()
    private.write_text("synthetic-private-token")
    private.chmod(0o600)
    (repo / "python/meerkat_computer").chmod(0o700)
    dockerfile = repo / "tests/desktop/Dockerfile.stack"
    dockerfile.parent.mkdir(parents=True)
    shutil.copyfile(source.parents[1] / "tests/desktop/Dockerfile.stack", dockerfile)
    original_modes = {name: stat.S_IMODE((repo / name).stat().st_mode) for name in paths}
    record = tmp_path / "docker.jsonl"
    docker = tmp_path / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import io,json,os,stat,sys,tarfile\n"
        "from pathlib import Path\n"
        "args=sys.argv[1:]\n"
        "entry={'args':args}\n"
        "if args[0]=='build':\n"
        " with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())) as archive:\n"
        "  expected=json.loads(os.environ['EXPECTED_SMOKE_SCRIPTS'])\n"
        "  assert set(expected)<=set(archive.getnames()), 'smoke dependency missing from build context'\n"
        "  dockerfile=archive.extractfile('tests/desktop/Dockerfile.stack').read().decode()\n"
        "  copies=[line.split()[1:-1] for line in dockerfile.splitlines() if line.startswith('COPY ')]\n"
        "  assert all(any(p in parts for parts in copies) for p in expected), 'smoke dependency missing from image COPY'\n"
        "if args[0]=='cp':\n"
        " root=Path(args[1]); items=[root,*root.rglob('*')] if root.is_dir() else [root]\n"
        " modes={str(p):stat.S_IMODE(p.stat().st_mode) for p in items}\n"
        " entry['modes']=modes\n"
        " assert all(mode & 0o004 for mode in modes.values()), 'guest cannot read source'\n"
        " assert all(modes[str(p)] & 0o001 for p in items if p.is_dir()), 'guest cannot traverse source'\n"
        " assert not any('.state' in p.parts for p in items), 'private state copied'\n"
        "with open(os.environ['DOCKER_RECORD'],'a') as f:f.write(json.dumps(entry)+'\\n')\n"
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "DOCKER": str(docker),
            "DOCKER_RECORD": str(record),
            "DESKTOP_STACK_SKIP_BUILD": "0",
            "EXPECTED_SMOKE_SCRIPTS": json.dumps(smoke_scripts),
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in record.read_text().splitlines()]
    copied = [call for call in calls if call["args"][0] == "cp"]
    assert copied
    staged_paths = {path for call in copied for path in call["modes"]}
    assert any(path.endswith("infra/desktop-session.py") for path in staged_paths)
    assert any(path.endswith("python/meerkat_computer/worker.py") for path in staged_paths)
    assert all(any(path.endswith(name) for path in staged_paths) for name in smoke_scripts)
    assert all(
        mode & 0o001
        for call in copied
        for path, mode in call["modes"].items()
        if path.endswith(("scripts/repo-cargo", "infra/desktop-session.py"))
    )
    assert all(not Path(path).is_relative_to(repo) for path in staged_paths)
    assert all(not Path(path).exists() for path in staged_paths), "temporary source staging leaked"
    assert [call["args"][0] for call in calls].count("start") == 1
    assert calls[-1]["args"][0:2] == ["rm", "-f"]
    assert {name: stat.S_IMODE((repo / name).stat().st_mode) for name in paths} == original_modes
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
