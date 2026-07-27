"""Central device registry.

This module is intentionally reusable by other SGDB features.
"""

from __future__ import annotations

from plugins.devices.base import Device2260EVB
from plugins.devices.base import DeviceSpec

DEVICE_REGISTRY: dict[str, DeviceSpec] = {
    "1690": Device2260EVB(name="1690", tcp_range="172.24.12.100:40090-40097"),
    "1690e": Device2260EVB(name="1690e", tcp_range="172.24.12.100:40090-40093", core_num=4),
    "cv84x6": DeviceSpec(
        name="cv84x6",
        core_num=4,
        ip="172.24.12.100",
        port_start=40090,
        port_end=40093,
        target_process="bmcpu",
        remote_rootfs_cpio=None,
    ),
}


def get_device(name: str) -> DeviceSpec | None:
    return DEVICE_REGISTRY.get(name)


def list_supported_device_names() -> list[str]:
    return sorted(DEVICE_REGISTRY.keys())
