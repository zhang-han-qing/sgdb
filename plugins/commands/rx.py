"""rx command: read physical address via monitor rx."""

from __future__ import annotations

import re
import struct

import gdb

_RX_SPEC_RE = re.compile(r"^(?:(\d+))?([xduf])([bhwg])$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

_UNIT_BYTES = {
    "b": 1,
    "h": 2,
    "w": 4,
    "g": 8,
}


def _parse_rx_args(arg: str) -> tuple[int, str, int, int]:
    raw = arg.strip()
    if not raw:
        raise gdb.GdbError("用法: rx [/<n><format><units>] addr，例如: rx /4xw 0x6908010000")

    count = 1
    fmt = "x"
    unit_bytes = 4

    remainder = raw
    if remainder.startswith("/"):
        parts = remainder.split(maxsplit=1)
        spec = parts[0][1:]
        m = _RX_SPEC_RE.fullmatch(spec)
        if m is None:
            raise gdb.GdbError("格式说明符无效，示例: /4xw, /8uw, /2fh")
        if m.group(1):
            count = int(m.group(1))
        fmt = m.group(2)
        unit_bytes = _UNIT_BYTES[m.group(3)]
        if len(parts) != 2:
            raise gdb.GdbError("缺少地址参数")
        remainder = parts[1].strip()

    addr_token = remainder.strip()
    if not addr_token:
        raise gdb.GdbError("缺少地址参数")
    try:
        addr = int(addr_token, 0)
    except ValueError as exc:
        raise gdb.GdbError(f"地址格式无效: {addr_token}") from exc
    if addr < 0:
        raise gdb.GdbError("地址必须为非负整数")

    if count <= 0:
        raise gdb.GdbError("count 必须大于 0")
    return addr, fmt, unit_bytes, count


def _to_signed(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return value - (1 << bits) if (value & sign) else value


def _format_value(value: int, fmt: str, unit_bytes: int) -> str:
    if fmt == "x":
        return f"0x{value:0{unit_bytes * 2}x}"
    if fmt == "u":
        return str(value)
    if fmt == "d":
        return str(_to_signed(value, unit_bytes * 8))
    if fmt == "f":
        if unit_bytes == 4:
            raw = value.to_bytes(4, byteorder="little", signed=False)
            return f"{struct.unpack('<f', raw)[0]:.8g}"
        if unit_bytes == 8:
            raw = value.to_bytes(8, byteorder="little", signed=False)
            return f"{struct.unpack('<d', raw)[0]:.12g}"
        raise gdb.GdbError("format=f 仅支持 unit 为 4 或 8 字节")
    raise gdb.GdbError(f"未知格式: {fmt}")


def _print_x_style(addr: int, values: list[int], fmt: str, unit_bytes: int) -> None:
    per_line = 4
    for line_start in range(0, len(values), per_line):
        line_values = values[line_start : line_start + per_line]
        line_addr = addr + line_start * unit_bytes
        rendered = "\t".join(_format_value(v, fmt, unit_bytes) for v in line_values)
        print(f"0x{line_addr:016x}:\t{rendered}")


class RXCommand(gdb.Command):
    """rx [/<n><format><units>] addr: 读取物理地址并按 x 风格展示。"""

    def __init__(self) -> None:
        super().__init__("rx", gdb.COMMAND_DATA)

    def invoke(self, arg: str, from_tty: bool) -> None:
        addr, fmt, unit_bytes, count = _parse_rx_args(arg)
        total_bytes = count * unit_bytes

        monitor_cmd = f"monitor rx {addr:#x} {total_bytes}"
        output = gdb.execute(monitor_cmd, to_string=True).strip()

        hex_data: str | None = None
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("ERR "):
                raise gdb.GdbError(line)
            if not line:
                continue
            if _HEX_RE.fullmatch(line):
                hex_data = line
                break

        if hex_data is None:
            if output:
                print(output)
                return
            raise gdb.GdbError("[rx] 未接收有效响应")

        if len(hex_data) != total_bytes * 2:
            raise gdb.GdbError("[rx] 数据长度异常")

        raw = bytes.fromhex(hex_data)
        values: list[int] = []
        for i in range(count):
            chunk = raw[i * unit_bytes : (i + 1) * unit_bytes]
            values.append(int.from_bytes(chunk, byteorder="little", signed=False))
        _print_x_style(addr, values, fmt, unit_bytes)
