"""sgdb command: list all SGDB commands."""

from __future__ import annotations

import gdb


class SGDBCommand(gdb.Command):
    """sgdb: 显示 SGDB 已注册命令列表。"""

    def __init__(self, command_names: list[str]) -> None:
        self._command_names = sorted(command_names)
        self.__doc__ = "sgdb: 显示 SGDB 已注册命令列表。\n\n已注册命令:\n  " + "\n  ".join(
            self._command_names
        )
        super().__init__("sgdb", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        print("[sgdb] commands:")
        for name in self._command_names:
            print(f"  - {name}")
