#!/usr/bin/env bash
set -eu

# Install and start host-side gdb proxy services (systemd).
# Usage:
#   sudo ./setup_pcie_host.sh [1690|1690e]

DEVICE_TYPE="${1:-1690}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INSTALL_SCRIPT="${SCRIPT_DIR}/proxy/packaging/install.sh"

if [ "$(id -u)" -ne 0 ]; then
    echo "[setup_pcie_host] please run as root (sudo)." >&2
    exit 1
fi

if [ ! -x "${INSTALL_SCRIPT}" ] && [ ! -f "${INSTALL_SCRIPT}" ]; then
    echo "[setup_pcie_host] missing installer: ${INSTALL_SCRIPT}" >&2
    exit 1
fi

echo "[setup_pcie_host] install proxy for device=${DEVICE_TYPE}"
sh "${INSTALL_SCRIPT}" "${DEVICE_TYPE}"

echo "[setup_pcie_host] reload systemd/udev"
systemctl daemon-reload
udevadm control --reload-rules || true

if [ -x "/usr/lib/tpu-gdb-proxy/scripts/gdb_proxy_instances.sh" ]; then
    /usr/lib/tpu-gdb-proxy/scripts/gdb_proxy_instances.sh refresh || true
fi

echo
echo "[setup_pcie_host] done."
echo "Check status with:"
echo "  systemctl status 'gdb-proxy@*'"
