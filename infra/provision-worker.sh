#!/usr/bin/env bash
# Runs only inside the owned, disposable image builder, before publication.
set -euo pipefail
desktop=false
apps=full
while [[ $# -gt 0 ]]; do
    case "$1" in
        --desktop) desktop=true; shift ;;
        --apps) apps="${2:?--apps requires minimal or full}"; shift 2 ;;
        *) echo 'Unsupported provisioning option' >&2; exit 2 ;;
    esac
done
[[ "$apps" == minimal || "$apps" == full ]]
test "$(id -u)" = 0
test -f /root/worker-source.tgz
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv git curl ca-certificates build-essential nodejs npm apparmor sudo
if $desktop; then
    apt-get install -y --no-install-recommends xvfb xauth xdotool x11-utils openbox dbus dbus-x11 at-spi2-core libglib2.0-bin libatk-adaptor libgail-common mousepad thunar evince poppler-utils fonts-dejavu-core
    if [[ "$apps" == full ]]; then
        apt-get install -y --no-install-recommends blender inkscape gimp libreoffice-writer libreoffice-calc libreoffice-impress libreoffice-gtk3 fonts-liberation fonts-noto-core mesa-utils libgl1-mesa-dri
    fi
fi
id meerkat-computer >/dev/null 2>&1 || useradd --create-home --home-dir /home/computer --shell /bin/bash meerkat-computer
# The immutable broker profile, not the image, grants passwordless sudo at bootstrap.
test ! -e /etc/sudoers.d/meerkat-computer
install -d -m 0755 /opt/meerkat-computer
tar -xzf /root/worker-source.tgz -C /opt/meerkat-computer
rm /root/worker-source.tgz
cd /opt/meerkat-computer
python3 -m venv /opt/meerkat-computer-build
/opt/meerkat-computer-build/bin/pip install 'uv==0.10.9'
/opt/meerkat-computer-build/bin/uv sync --frozen --no-dev --no-editable --python /usr/bin/python3
export PLAYWRIGHT_BROWSERS_PATH=/opt/meerkat-computer/browsers
.venv/bin/python -m playwright install --with-deps chromium
python3 infra/chromium-profile.py --apply
# The same launcher is used at image qualification and at worker startup.
check=(.venv/bin/python infra/image-check.py)
if $desktop; then
    check=(.venv/bin/python infra/desktop-session.py -- "${check[@]}" --desktop)
fi
runuser -u meerkat-computer -- env PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_BROWSERS_PATH" "${check[@]}"
install -d -o meerkat-computer -g meerkat-computer -m 0700 /var/lib/meerkat-computer /var/lib/meerkat-computer/state
install -d -o meerkat-computer -g meerkat-computer -m 0750 /workspace
cat >/etc/systemd/system/meerkat-computer-worker.service <<'UNIT'
[Unit]
Description=Private Meerkat Computer worker
After=network-online.target
Wants=network-online.target
ConditionPathExists=/var/lib/meerkat-computer/worker.token
[Service]
User=meerkat-computer
Group=meerkat-computer
WorkingDirectory=/workspace
Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/meerkat-computer/browsers
Environment=MEERKAT_COMPUTER_WORKER_TOKEN_FILE=/var/lib/meerkat-computer/worker.token
EnvironmentFile=/var/lib/meerkat-computer/worker.env
UNIT
if $desktop; then
cat >>/etc/systemd/system/meerkat-computer-worker.service <<'UNIT'
ExecStart=/opt/meerkat-computer/.venv/bin/python /opt/meerkat-computer/infra/desktop-session.py -- /opt/meerkat-computer/.venv/bin/python -m meerkat_computer.worker --desktop --host 0.0.0.0 --port ${MEERKAT_COMPUTER_WORKER_PORT} --state-dir /var/lib/meerkat-computer/state --workspace /workspace
UNIT
else
cat >>/etc/systemd/system/meerkat-computer-worker.service <<'UNIT'
ExecStart=/opt/meerkat-computer/.venv/bin/python -m meerkat_computer.worker --host 0.0.0.0 --port ${MEERKAT_COMPUTER_WORKER_PORT} --state-dir /var/lib/meerkat-computer/state --workspace /workspace
UNIT
fi
cat >>/etc/systemd/system/meerkat-computer-worker.service <<'UNIT'
Restart=on-failure
RestartSec=2
# Allow the supervisor's 12s worker grace plus bounded group cleanup.
TimeoutStopSec=20
# The supervisor must stop the worker before Chromium, X11 and DBus.
KillMode=mixed
UMask=0077
CPUAccounting=true
MemoryAccounting=true
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable meerkat-computer-worker.service
test ! -e /var/lib/meerkat-computer/worker.token
test ! -e /var/lib/meerkat-computer/worker.env
test -z "$(find /var/lib/meerkat-computer/state -mindepth 1 -print -quit)"
test -z "$(find /workspace -mindepth 1 -print -quit)"
# Incus creates unique instance IDs. Regenerate cloned guest OS identity on boot.
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
rm -f /etc/ssh/ssh_host_* /root/.bash_history
apt-get clean
echo 'Worker installed; image contains no computer token, lease, browser state or workspace data.'
