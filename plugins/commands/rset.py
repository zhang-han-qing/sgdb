"""rset command: write physical address via monitor rset."""

from __future__ import annotations

import re
import struct

import gdb

_RSET_RE = re.compile(
    r"^\s*\*\s*\(\s*(int|float)\s*\*\s*\)\s*\+?(0x[0-9a-fA-F]+|\d+)\s*=\s*(.+?)\s*$"
)
def _parse_rset_arg(arg: str) -> tuple[int, int]:
    m = _RSET_RE.fullmatch(arg.strip())
    if m is None:
        raise gdb.GdbError("用法: rset *(int*)0xADDR = <value> 或 rset *(float*)0xADDR = <value>")

    type_name = m.group(1)
    addr_token = m.group(2)
    value_token = m.group(3).strip()

    try:
        addr = int(addr_token, 0)
    except ValueError as exc:
        raise gdb.GdbError(f"地址格式无效: {addr_token}") from exc
    if addr < 0:
        raise gdb.GdbError("地址必须为非负整数")

    if type_name == "int":
        try:
            raw = int(value_token, 0)
        except ValueError as exc:
            raise gdb.GdbError(f"int 值格式无效: {value_token}") from exc
        value = raw & 0xFFFFFFFF
        return addr, value

    try:
        fval = float(value_token)
    except ValueError as exc:
        raise gdb.GdbError(f"float 值格式无效: {value_token}") from exc
    value = int.from_bytes(struct.pack("<f", fval), byteorder="little", signed=False)
    return addr, value


class RSetCommand(gdb.Command):
    """rset *(int|float*)addr = value: 写物理地址（4字节）。"""

    def __init__(self) -> None:
        super().__init__("rset", gdb.COMMAND_DATA)

    def invoke(self, arg: str, from_tty: bool) -> None:
        addr, value = _parse_rset_arg(arg)

        monitor_cmd = f"monitor rset {addr:#x} 4 {value:#x}"
        output = gdb.execute(monitor_cmd, to_string=True).strip()

        for line in output.splitlines():
            line = line.strip()
            if line.startswith("ERR "):
                raise gdb.GdbError(line)
            if line == "OK":
                print("[rset] OK")
                return

        if output:
            print(output)
            return
        raise gdb.GdbError("[rset] 未收到 OK 回应")
