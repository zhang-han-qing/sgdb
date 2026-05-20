"""Device definitions for SGDB."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceSpec:
    name: str
    core_num: int
    ip: str
    port_start: int
    port_end: int
    tpu_register_xml: str | None = None  # 在 host 端放 XML 文件

    @staticmethod
    def parse_tcp_range(tcp_range: str) -> tuple[str, int, int]:
        m = re.fullmatch(r"\s*([A-Za-z0-9.\-]+):(\d+)-(\d+)\s*", tcp_range)
        if not m:
            raise ValueError(
                f"invalid tcp_range: {tcp_range}. expected <ip>:<start>-<end>"
            )
        ip, start_s, end_s = m.groups()
        port_start = int(start_s)
        port_end = int(end_s)
        if port_start > port_end:
            raise ValueError("tcp_range: start port must be <= end port")
        if port_start < 1 or port_end > 65535:
            raise ValueError("tcp_range: ports must be in 1..65535")
        return ip, port_start, port_end

    @property
    def ip_range_str(self) -> str:
        return f"{self.ip}:{self.port_start}-{self.port_end}"


class Device2260EVB(DeviceSpec):
    def __init__(
        self,
        name: str,
        tcp_range: str,
        core_num: int = 8,
        *,
        tpu_register_xml: str | None = None,
    ) -> None:
        try:
            ip, port_start, port_end = DeviceSpec.parse_tcp_range(tcp_range)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc

        super().__init__(
            name=name,
            core_num=core_num,
            ip=ip,
            port_start=port_start,
            port_end=port_end,
            tpu_register_xml=tpu_register_xml,
        )
