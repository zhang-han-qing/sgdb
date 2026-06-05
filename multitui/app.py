"""Textual app wrapping N gdb (or any) sessions.

Layout:
  - left:   one panel per *unfocused* core (status + live feed + per-core input);
            double-click a panel to make that core the focused one.
  - right:  the focused core's native terminal (CLI/TUI), interactive, plus a
            highlighted (global) input at the bottom that broadcasts to all cores.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from functools import partial

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Input, Label, RichLog

from .backend import CoreSession, SessionManager
from .terminal import GdbTerminal

_STATUS_COLOR = {"RUNNING": "yellow", "STOP": "green", "DEAD": "red"}
_REFRESH_HZ = 30


class CorePanel(Vertical):
    """One core's status, scrolling feed and a per-core command input."""

    DEFAULT_CSS = """
    CorePanel {
        height: 16;
        border: round $primary;
        margin: 0 0 1 0;
    }
    CorePanel > .panel-title {
        height: 1;
        background: $boost;
        text-style: bold;
    }
    CorePanel > .panel-log {
        height: 1fr;
    }
    CorePanel > .panel-input {
        height: 3;
        border: round $accent;
        background: $surface;
    }
    """

    class Switch(Message):
        def __init__(self, core_id: int) -> None:
            self.core_id = core_id
            super().__init__()

    def __init__(self, core_id: int) -> None:
        super().__init__(id=f"panel-{core_id}")
        self.core_id = core_id

    def compose(self) -> ComposeResult:
        self._label = Label(self._title("RUNNING"), classes="panel-title")
        self._log = RichLog(
            max_lines=400, wrap=True, markup=False, highlight=False, classes="panel-log"
        )
        self._input = Input(
            placeholder="type command + Enter", id=f"in-{self.core_id}", classes="panel-input"
        )
        self._input.border_title = f"core {self.core_id} >"
        yield self._label
        yield self._log
        yield self._input

    def _title(self, status: str) -> str:
        color = _STATUS_COLOR.get(status, "white")
        return f"core {self.core_id}  [{color}]{status}[/]  [dim](double-click to focus)[/]"

    def add_line(self, text: str) -> None:
        self._log.write(text)

    def set_status(self, status: str) -> None:
        self._label.update(self._title(status))

    def on_click(self, event) -> None:
        if getattr(event, "chain", 1) >= 2:
            self.post_message(self.Switch(self.core_id))


class MultiTuiApp(App):
    CSS = """
    #body { height: 1fr; }
    #left { width: 34%; padding: 0 1; }
    #right { width: 1fr; }
    #terminal_title {
        height: 1;
        background: $accent;
        color: $text;
        text-style: bold;
        padding: 0 1;
    }
    GdbTerminal { height: 1fr; }
    #global_bar {
        dock: bottom;
        height: 3;
        border: heavy $warning;
        background: $surface;
    }
    #global_label {
        width: auto;
        padding: 0 1;
        color: $warning;
        text-style: bold;
    }
    #global_input { width: 1fr; border: none; }
    """

    BINDINGS = [
        Binding(
            "ctrl+q",
            "quit",
            "[Quit]",
            priority=True,
            key_display="ctrl+q",
            show=True,
        ),
        Binding("ctrl+c", "interrupt", "Interrupt focused core", show=True),
    ]

    def __init__(
        self,
        manager: SessionManager,
        attach_by_core: dict[int, str] | None = None,
    ) -> None:
        super().__init__()
        self.manager = manager
        self.panels: dict[int, CorePanel] = {}
        self._terminal: GdbTerminal | None = None
        self._dirty_term = False
        self._dirty_status: set[int] = set()
        # One-shot: re-sync the initially-focused core's size once gdb is up.
        self._focused_resized = False
        # Per-core command to run once the core reaches the (gdb) prompt. The
        # focused core additionally waits for its first resize/SIGWINCH so its
        # attach sequence is never interrupted (EINTR) by a late SIGWINCH.
        self._attach_by_core: dict[int, str] = attach_by_core or {}
        self._attached: set[int] = set()

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with VerticalScroll(id="left"):
                for cid in self.manager.core_ids:
                    panel = CorePanel(cid)
                    self.panels[cid] = panel
                    yield panel
            with Vertical(id="right"):
                yield Label(self._terminal_title(self.manager.focused), id="terminal_title")
                yield GdbTerminal(self.manager.focused_session, id="terminal")
                with Horizontal(id="global_bar"):
                    yield Label("(global)", id="global_label")
                    yield Input(
                        placeholder="broadcast to ALL cores | stop = Ctrl-C all | core N",
                        id="global_input",
                    )
        yield Footer()

    def on_mount(self) -> None:
        self._terminal = self.query_one("#terminal", GdbTerminal)
        for cid, session in self.manager.sessions.items():
            session.screen.on_line = partial(self._feed_line, cid)
        self.manager.start_all()
        loop = asyncio.get_running_loop()
        for session in self.manager.sessions.values():
            if session.master_fd is not None:
                loop.add_reader(session.master_fd, self._on_readable, session)
        self._apply_focus(self.manager.focused)
        self._terminal.focus()
        self.set_interval(1 / _REFRESH_HZ, self._flush)
        # core 0 is attached before the layout has a real size, so its initial
        # _sync_size() is a no-op. Re-sync once the geometry is known, giving it
        # the same treatment switched-to cores get via attach().
        self.call_after_refresh(self._sync_focused_size)

    def _sync_focused_size(self) -> None:
        if self._terminal is not None:
            self._terminal._sync_size()
            self._terminal.refresh()

    # ---- data flow -------------------------------------------------------

    def _feed_line(self, core_id: int, text: str) -> None:
        # Always accumulate scrollback per core, even while focused. The panel
        # is merely hidden while focused, so switching back shows full history.
        panel = self.panels.get(core_id)
        if panel is not None:
            panel.add_line(text)

    def _on_readable(self, session: CoreSession) -> None:
        if session.master_fd is None:
            return
        try:
            data = os.read(session.master_fd, 65536)
        except OSError:
            data = b""
        if not data:
            self._mark_dead(session)
            return
        session.feed(data)
        self._dirty_status.add(session.core_id)
        if session.core_id == self.manager.focused:
            self._dirty_term = True
            # Once the focused core has produced output (gdb is ready) and the
            # widget has a real size, re-assert its winsize so the initial core
            # (which missed the early SIGWINCH) picks up the correct geometry.
            if not self._focused_resized and self._terminal is not None:
                size = self._terminal.size
                if size.width > 0 and size.height > 0:
                    self._terminal._sync_size()
                    self._focused_resized = True
        self._maybe_send_attach(session)

    def _maybe_send_attach(self, session: CoreSession) -> None:
        cid = session.core_id
        if cid in self._attached:
            return
        command = self._attach_by_core.get(cid)
        if not command:
            return
        # Focused core: wait until its initial resize/SIGWINCH has fired, so the
        # attach syscalls (target/info/attach) won't be interrupted by EINTR.
        if cid == self.manager.focused and not self._focused_resized:
            return
        # Ready == gdb is idle at the (gdb) prompt (plugins sourced).
        if session.status() != "STOP":
            return
        session.write_line(command)
        self._attached.add(cid)

    def _mark_dead(self, session: CoreSession) -> None:
        loop = asyncio.get_running_loop()
        if session.master_fd is not None:
            loop.remove_reader(session.master_fd)
        if session.pid is not None:
            try:
                os.waitpid(session.pid, os.WNOHANG)
            except ChildProcessError:
                pass
        session.alive = False
        self._dirty_status.add(session.core_id)

    def _flush(self) -> None:
        if self._dirty_term and self._terminal is not None:
            self._terminal.refresh()
            self._dirty_term = False
        if self._dirty_status:
            for cid in self._dirty_status:
                self.panels[cid].set_status(self.manager.sessions[cid].status())
            self._dirty_status.clear()

    # ---- focus / input ---------------------------------------------------

    def _terminal_title(self, core_id: int) -> str:
        return (
            f" >>> core {core_id}  (focused, keys go here | ctrl-x ctrl-a: gdb TUI)"
        )

    def switch_focus(self, core_id: int) -> None:
        if core_id == self.manager.focused or core_id not in self.manager.sessions:
            return
        self.manager.focus(core_id)
        # Constraint: whenever a core is brought into the focused pane (double
        # click or `core N`), send Ctrl-C first so it pauses immediately.
        self.manager.focused_session.interrupt()
        self._apply_focus(core_id)

    def _apply_focus(self, core_id: int) -> None:
        for cid, panel in self.panels.items():
            panel.display = cid != core_id
        self.query_one("#terminal_title", Label).update(self._terminal_title(core_id))
        if self._terminal is not None:
            self._terminal.attach(self.manager.focused_session)
            self._terminal.focus()

    def on_core_panel_switch(self, message: CorePanel.Switch) -> None:
        self.switch_focus(message.core_id)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        input_id = event.input.id or ""
        if input_id.startswith("in-"):
            cid = int(input_id[3:])
            if cid in self.manager.sessions:
                if text == "stop":
                    self.manager.sessions[cid].interrupt()
                elif text:
                    self.manager.sessions[cid].write_line(text)
            return
        # global bar
        if not text:
            return
        if text == "stop":
            self.manager.interrupt_all()
            return
        if text.startswith("core "):
            try:
                cid = int(text.split(maxsplit=1)[1])
            except ValueError:
                return
            self.switch_focus(cid)
            return
        if text.startswith("global "):
            text = text[len("global "):].strip()
        if text:
            self.manager.broadcast(text)

    def action_interrupt(self) -> None:
        self.manager.focused_session.interrupt()

    async def action_quit(self) -> None:
        # Before quitting, Ctrl-C every core and wait until none is RUNNING, so
        # we never leave a core executing on the device. The PTY readers keep
        # running during the await, so status() converges as prompts come back.
        self.manager.interrupt_all()
        for _ in range(100):  # up to ~5s (100 * 50ms)
            if all(
                s.status() != "RUNNING" for s in self.manager.sessions.values()
            ):
                break
            await asyncio.sleep(0.05)
        self.exit()

    def on_unmount(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for session in self.manager.sessions.values():
                if session.master_fd is not None:
                    loop.remove_reader(session.master_fd)
        except RuntimeError:
            pass
        self.manager.close_all()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wrap N terminal sessions (gdb/bash/...) in one Textual UI."
    )
    parser.add_argument("--cores", type=int, default=None, help="number of cores/sessions")
    parser.add_argument(
        "--cmd",
        default="bash",
        help="generic mode: command to launch per core (e.g. 'gdb', 'bash')",
    )
    parser.add_argument("--cols", type=int, default=100, help="initial columns")
    parser.add_argument("--rows", type=int, default=30, help="initial rows")
    # sgdb mode: enabled when --ip is given; each core attaches to its tp_daemon
    parser.add_argument("--ip", default=None, help="enable SGDB mode: target IP for tp-attach")
    parser.add_argument("--device", default="1690", help="SGDB device type (1690/1690e)")
    parser.add_argument("--device-id", type=int, default=0, help="SGDB device id")
    parser.add_argument("--gdb", default="gdb-multiarch", help="gdb executable for SGDB mode")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    attach_by_core: dict[int, str] = {}
    if args.ip:
        from .sgdb_launch import (
            SGDB_ROOT,
            build_attach_command,
            build_sgdb_argv,
            default_core_num,
        )

        cores = args.cores or default_core_num(args.device)
        core_ids = list(range(cores))
        argv_by_core = {
            cid: build_sgdb_argv(args.gdb, args.device, args.device_id, cid, args.ip)
            for cid in core_ids
        }
        attach_by_core = {
            cid: build_attach_command(args.device, args.device_id, cid, args.ip)
            for cid in core_ids
        }
        manager = SessionManager(
            argv_by_core, core_ids, cols=args.cols, rows=args.rows, cwd=str(SGDB_ROOT)
        )
    else:
        core_ids = list(range(args.cores or 4))
        manager = SessionManager.from_command(
            args.cmd, core_ids, cols=args.cols, rows=args.rows
        )
    MultiTuiApp(manager, attach_by_core).run()


if __name__ == "__main__":
    main()
