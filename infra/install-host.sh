#!/usr/bin/env bash
# Host dependencies only. It does not reconfigure existing Incus resources.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" != "--apply" ]]; then
  echo 'Plan: install Incus, Btrfs tools, QEMU, Python and build tools on Ubuntu 24.04.'
  echo 'Load bridge netfilter and persist it for guest network isolation.'
  echo 'Run this script with --apply as root to install these dependencies.'
  exit 0
fi
test "$(id -u)" = 0 || { echo 'Run as root.' >&2; exit 1; }
source /etc/os-release
test "$ID:$VERSION_ID" = ubuntu:24.04 || { echo 'This installer supports Ubuntu 24.04.' >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y incus btrfs-progs qemu-system-x86 qemu-utils ovmf python3 python3-venv curl kmod git build-essential pkg-config
modprobe br_netfilter
install -m 0644 "$script_dir/meerkat-computer-modules.conf" /etc/modules-load.d/meerkat-computer.conf
echo 'Installed host dependencies. Run infra/host-setup.py to create owned resources.'
