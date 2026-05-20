"""SGDB loader.

Add one line into ~/.gdbinit once (path is your local sgdb path):
    source /path/to/sgdb/plugins/gdbinit.py
"""

from __future__ import annotations

import sys
from pathlib import Path


def _check_path() -> None:
    src_root = Path(__file__).resolve().parent
    project_root = str(src_root.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root) # 保证 plugins 包可以被导入


def _register_all_commands() -> None:
    import gdb
    from plugins.commands import register_all_commands

    if getattr(gdb, "_sgdb_loaded", False):
        return

    register_all_commands()
    gdb._sgdb_loaded = True


def main() -> None:
    _check_path()
    _register_all_commands()


main()
