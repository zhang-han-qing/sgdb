#!/bin/sh
set -eu

ACTION="${1:-start}"

list_device_indexes() {
    for node in /dev/sg-host-drv-*; do
        [ -e "$node" ] || continue
        idx="${node##*-}"
        case "$idx" in
            ''|*[!0-9]*) continue ;;
        esac
        echo "$idx"
    done
}

start_instances() {
    list_device_indexes | while IFS= read -r idx; do
        [ -n "$idx" ] || continue
        systemctl start "gdb-proxy@${idx}.service"
    done
}

stop_instances() {
    systemctl list-units --all --full 'gdb-proxy@*.service' --no-legend 2>/dev/null \
        | awk '{print $1}' \
        | while IFS= read -r unit; do
            [ -n "$unit" ] || continue
            systemctl stop "$unit"
        done
}

case "$ACTION" in
    start|refresh)
        start_instances
        ;;
    stop)
        stop_instances
        ;;
    restart)
        stop_instances
        start_instances
        ;;
    *)
        echo "usage: $0 {start|stop|refresh|restart}" >&2
        exit 2
        ;;
esac
