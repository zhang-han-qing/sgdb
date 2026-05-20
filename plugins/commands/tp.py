"""tp command: switch inferior by tp index."""

from __future__ import annotations

import gdb

from plugins.state.tpu_threads import get_tpu_thread_binding


def _normalize_tp_name(raw: str) -> str:
    token = raw.strip().lower()
    if not token:
        raise gdb.GdbError("用法: tp <idx>，例如 tp 0 或 tp 3")
    if token.startswith("tp"):
        token = token[2:]
    if not token.isdigit():
        raise gdb.GdbError("参数必须是数字索引，例如 tp 0")
    return f"tp{int(token)}"


class TPSelectCommand(gdb.Command):
    """tp <idx>: 切换到对应的 inferior。"""

    def __init__(self) -> None:
        super().__init__("tp", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        tp_name = _normalize_tp_name(arg)
        binding = get_tpu_thread_binding(tp_name)
        if binding is None:
            raise gdb.GdbError(f"{tp_name} 不存在，请先执行 tpu-attach")

        alive_nums = {inf.num for inf in gdb.inferiors()}
        if binding.inferior_num not in alive_nums:
            raise gdb.GdbError(
                f"{tp_name} 映射的 inferior {binding.inferior_num} 已不存在，请重新执行 tpu-attach"
            )

        gdb.execute(f"inferior {binding.inferior_num}", to_string=True)
        print(f"[tp] switched to {tp_name} -> inferior {binding.inferior_num}")
