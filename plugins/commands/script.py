"""script command: run host-side scripts via monitor."""

from __future__ import annotations

import gdb

_SUPPORTED_SCRIPTS_CACHE: set[str] | None = None


def _query_supported_scripts() -> set[str]:
    output = gdb.execute("monitor script", to_string=True)
    for line in output.splitlines():
        text = line.strip()
        if text.startswith("SUPPORTED:"):
            payload = text[len("SUPPORTED:") :].strip()
            if not payload:
                return set()
            return set(payload.split())
    raise gdb.GdbError("无法从 gdb_proxy 获取脚本能力列表，请检查 monitor script 返回")


def _get_supported_scripts() -> set[str]:
    global _SUPPORTED_SCRIPTS_CACHE
    if _SUPPORTED_SCRIPTS_CACHE is None:
        _SUPPORTED_SCRIPTS_CACHE = _query_supported_scripts()
    return _SUPPORTED_SCRIPTS_CACHE


class ScriptCommand(gdb.Command):
    """script <name> [args...]: 在目标机 host 侧执行 monitor script。"""

    def __init__(self) -> None:
        super().__init__("script", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        raw = arg.strip()
        supported = _get_supported_scripts()
        if not raw:
            supported_text = ", ".join(sorted(supported)) or "(empty)"
            raise gdb.GdbError(
                f"用法: script <name> [args...]，例如: script tpu_hang_info 0；当前支持: {supported_text}"
            )

        parts = raw.split(maxsplit=1)
        name = parts[0]
        if name not in supported:
            supported_text = ", ".join(sorted(supported)) or "(empty)"
            raise gdb.GdbError(f"不支持的脚本: {name}，当前支持: {supported_text}")

        monitor_cmd = f"monitor script {raw}"

        gdb.execute(monitor_cmd, to_string=False)
