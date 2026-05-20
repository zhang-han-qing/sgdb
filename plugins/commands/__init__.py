"""SGDB command registry."""

from __future__ import annotations

from plugins.commands.rset import RSetCommand
from plugins.commands.rx import RXCommand
from plugins.commands.script import ScriptCommand
from plugins.commands.tp import TPSelectCommand
from plugins.commands.tp_attach import TPAttachCommand
from plugins.commands.sgdb import SGDBCommand
from plugins.commands.tpu_attach import TPUAttachCommand


def register_all_commands() -> list[str]:
    command_names = ["rset", "rx", "script", "tpu-attach", "tp-attach", "tp"]
    RSetCommand()
    RXCommand()
    ScriptCommand()
    TPUAttachCommand()
    TPAttachCommand()
    TPSelectCommand()
    SGDBCommand(["sgdb"] + command_names)
    return ["sgdb"] + command_names

