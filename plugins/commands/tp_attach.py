"""tp-attach command: attach the current gdb to a single tp core."""

from __future__ import annotations

import re

import gdb

_BASE_PORT = 40090
_SUPPORTED_DEVICE_TYPES = {"1690", "1690e"}
# 连接 target 时，connect 的 syscall 可能被信号（如聚焦核启动时的 SIGWINCH）
# 打断而抛出 "Interrupted system call"(EINTR)。仅对这种情况重试。
_CONNECT_RETRIES = 2


def build_endpoint(ip: str, device_id: int, core_id: int) -> str:
    return f"{ip}:{_BASE_PORT + device_id * 100 + core_id}"


def _connect_target(endpoint: str) -> None:
    for attempt in range(_CONNECT_RETRIES + 1):
        try:
            gdb.execute(f"target extended-remote {endpoint}", to_string=True)
            return
        except gdb.error as exc:
            if "Interrupted system call" in str(exc) and attempt < _CONNECT_RETRIES:
                continue
            raise


def _pick_unique_tp_daemon_pid(info_text: str) -> int:
    matched_lines = [line for line in info_text.splitlines() if "tp_daemon" in line]
    if not matched_lines:
        raise gdb.GdbError("未找到 COMMAND 包含 tp_daemon 的进程")
    if len(matched_lines) > 1:
        raise gdb.GdbError("找到多个 tp_daemon 进程，无法唯一 attach")

    pid_match = re.search(r"\b(\d+)\b", matched_lines[0])
    if not pid_match:
        raise gdb.GdbError(f"无法从行中解析 pid: {matched_lines[0]}")
    return int(pid_match.group(1))


def attach_core(
    device_type: str,
    device_id: int,
    core_id: int,
    ip: str,
) -> tuple[str, int]:
    if device_type not in _SUPPORTED_DEVICE_TYPES:
        raise gdb.GdbError(
            f"不支持设备类型: {device_type}（当前支持: {sorted(_SUPPORTED_DEVICE_TYPES)}）"
        )
    if device_id < 0 or core_id < 0:
        raise gdb.GdbError("device-id/core-id 必须是非负整数")

    endpoint = build_endpoint(ip=ip, device_id=device_id, core_id=core_id)
    _connect_target(endpoint)
    info = gdb.execute("info os processes", to_string=True)
    pid = _pick_unique_tp_daemon_pid(info)
    gdb.execute(f"attach {pid}", to_string=True)
    return endpoint, pid


class TPAttachCommand(gdb.Command):
    """tp-attach <device-type> <device-id> <core-id> <ip>: attach this gdb to one tp core."""

    def __init__(self) -> None:
        super().__init__("tp-attach", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        parts = arg.strip().split()
        if len(parts) != 4:
            raise gdb.GdbError(
                "用法: tp-attach <device-type> <device-id> <core-id> <ip>"
                "（例如: tp-attach 1690 0 0 172.24.12.100）"
            )
        device_type, device_id_raw, core_id_raw, ip = parts
        try:
            device_id = int(device_id_raw)
            core_id = int(core_id_raw)
        except ValueError as exc:
            raise gdb.GdbError("device-id/core-id 必须是非负整数") from exc

        endpoint, pid = attach_core(
            device_type=device_type,
            device_id=device_id,
            core_id=core_id,
            ip=ip,
        )
        print(f"[tp-attach] {endpoint} attached pid={pid}")
