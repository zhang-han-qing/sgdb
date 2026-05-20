#!/bin/sh
# SC11 双卡 gdb-proxy 卸载：删除 install_sc11.sh 部署的文件。
# 用法：sudo sh packaging/uninstall_sc11.sh
#       sudo env DESTROOT=/tmp/stage sh packaging/uninstall_sc11.sh
#       sudo env PREFIX=/opt/foo/gdb_proxy sh packaging/uninstall_sc11.sh
set -eu

PREFIX="${PREFIX:-/usr/lib/tpu-gdb-proxy}"
DESTROOT="${DESTROOT:-}"

if [ -z "${DESTROOT}" ] && [ "$(id -u)" -ne 0 ]; then
    echo "uninstall: need root on target system (or set DESTROOT=)" >&2
    exit 1
fi

if [ -z "${DESTROOT}" ]; then
    systemctl list-units --all --full 'gdb-proxy@*.service' --no-legend 2>/dev/null \
        | awk '{print $1}' \
        | while IFS= read -r unit; do
            [ -n "$unit" ] || continue
            systemctl stop "$unit" 2>/dev/null || true
            systemctl disable "$unit" 2>/dev/null || true
        done
    systemctl stop gdb-proxy-refresh.service 2>/dev/null || true
    systemctl disable gdb-proxy-refresh.service 2>/dev/null || true
    systemctl stop gdb-proxy.service 2>/dev/null || true
    systemctl disable gdb-proxy.service 2>/dev/null || true
    modprobe -r sgcard 2>/dev/null || true
    pkill -f "${PREFIX}/proxy_fast.py" 2>/dev/null || true
    pkill -f "${PREFIX}/proxy.py" 2>/dev/null || true
fi

rm -rf "${DESTROOT}${PREFIX}"
rm -f "${DESTROOT}/etc/systemd/system/gdb-proxy@.service"
rm -f "${DESTROOT}/etc/systemd/system/gdb-proxy-refresh.service"
rm -f "${DESTROOT}/etc/systemd/system/gdb-proxy.service"
rm -f "${DESTROOT}/etc/default/gdb-proxy-0"
rm -f "${DESTROOT}/etc/default/gdb-proxy-1"
rm -f "${DESTROOT}/etc/default/gdb-proxy"
rm -f "${DESTROOT}/etc/modprobe.d/sgcard-gdb-proxy.conf"
rm -f "${DESTROOT}/etc/udev/rules.d/99-gdb-proxy-sgcard.rules"

if [ -z "${DESTROOT}" ]; then
    systemctl daemon-reload
    systemctl reset-failed 'gdb-proxy@*' 2>/dev/null || true
    udevadm control --reload-rules 2>/dev/null || true
fi

cat <<EOF
[uninstall] done. PREFIX=${PREFIX} DESTROOT=${DESTROOT:-/}
EOF
