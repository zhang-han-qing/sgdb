"""Glue between multitui and SGDB: build a per-core gdb command line that
sources the SGDB plugins and attaches to one tp core via the existing
``tp-attach`` command.

Kept intentionally minimal: only the init needed to register/run ``tp-attach``
(no sysroot, no mi-async, etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

SGDB_ROOT = Path(__file__).resolve().parents[1]  # .../sgdb


def build_sgdb_argv(
    gdb: str,
    device_type: str,
    device_id: int,
    core_id: int,
    ip: str,
) -> list[str]:
    plugin_init = SGDB_ROOT / "plugins" / "gdbinit.py"
    return [
        gdb,
        "-q",
        "-iex", "set pagination off",
        "-iex", f"source {plugin_init}",
    ]


def build_attach_command(
    device_type: str,
    device_id: int,
    core_id: int,
    ip: str,
) -> str:
    return f"tp-attach {device_type} {device_id} {core_id} {ip}"


def default_core_num(device_type: str, fallback: int = 2) -> int:
    if str(SGDB_ROOT) not in sys.path:
        sys.path.insert(0, str(SGDB_ROOT))
    try:
        from plugins.devices.registry import get_device

        device = get_device(device_type)
        return device.core_num if device is not None else fallback
    except Exception:  # noqa: BLE001 - registry is optional for launching
        return fallback
