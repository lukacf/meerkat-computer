#!/usr/bin/env bash
# Usage: install-broker.sh /path/to/rkat-computer /path/to/config.toml [--apply]
set -euo pipefail
binary=${1:?path to Linux rkat-computer binary}
config=${2:?path to configured TOML}
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
test -f "$binary"
test -f "$config"
if [[ "${3:-}" != "--apply" ]]; then
  echo 'Plan: install broker binary, configuration, private operator credential and systemd service.'
  echo 'Append --apply as root to install. Existing operator credentials are preserved.'
  exit 0
fi
test "$(id -u)" = 0 || { echo 'Run as root.' >&2; exit 1; }
"$binary" validate --config "$config"
getent group incus-admin >/dev/null
id meerkat-computer-broker >/dev/null 2>&1 || useradd --system --user-group --create-home --home-dir /var/lib/meerkat-computer-broker --shell /usr/sbin/nologin meerkat-computer-broker
install -d -o meerkat-computer-broker -g meerkat-computer-broker -m 0700 /var/lib/meerkat-computer-broker
install -d -m 0700 /etc/meerkat-computer
install -m 0755 "$binary" /usr/local/bin/rkat-computer
# Config must remain readable by the service; operator credentials remain root-only.
install -m 0755 -d /etc/meerkat-computer
install -o meerkat-computer-broker -g meerkat-computer-broker -m 0600 "$config" /etc/meerkat-computer/config.toml
python3 - "$config" <<'PY'
import os, pathlib, re, secrets, sys, tomllib
config = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())
name = config['server']['operator_token_env']
if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
    raise ValueError('operator token environment variable name is invalid')
target = pathlib.Path('/etc/meerkat-computer/operator.env')
if not target.exists():
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(name + '=' + secrets.token_hex(32) + '\n')
else:
    values = dict(line.split('=', 1) for line in target.read_text().splitlines() if '=' in line)
    if name not in values or len(values[name]) < 32:
        raise ValueError('existing operator.env does not provide the configured credential')
    os.chmod(target, 0o600)
PY
install -m 0644 "$script_dir/meerkat-computer.service" /etc/systemd/system/meerkat-computer.service
systemctl daemon-reload
systemctl enable meerkat-computer.service
systemctl restart meerkat-computer.service
python3 "$script_dir/broker-readiness.py" "$config"
echo 'Broker installed. Operator credential is in /etc/meerkat-computer/operator.env (mode 0600).'
