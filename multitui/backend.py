"""Backend for the multi-core TUI: one PTY + pyte screen per child process.

This module has no Textual dependency so it can be unit-tested or reused.
Each :class:`CoreSession` spawns a child (e.g. ``gdb`` or ``bash``) in its own
pseudo-terminal and feeds the raw output into a pyte screen.  The screen is the
single source of truth for both the full render (focused pane) and the
scrolling line feed (unfocused panes).
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import shlex
import signal
import struct
import termios
from collections.abc import Callable

import pyte

_PROMPT_RE = re.compile(r"\(gdb\)\s*$")
_MOUSE_MODE_RE = re.compile(rb"\x1b\[\?(1000|1002|1003|1006|1)(h|l)")


def set_winsize(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


class LineEmittingScreen(pyte.Screen):
    """A pyte screen that emits a finalized line whenever one scrolls by.

    ``on_line`` is invoked with the text of the line the cursor is leaving on
    every line feed.  This gives a robust, terminal-aware line feed (handling
    ``\\r`` and control codes) without the naive byte splitting that plagued
    the old hub.
    """

    def __init__(self, columns: int, lines: int) -> None:
        super().__init__(columns, lines)
        self.on_line: Callable[[str], None] | None = None

    def linefeed(self) -> None:
        if self.on_line is not None:
            row = self.buffer[self.cursor.y]
            text = "".join(row[x].data for x in range(self.columns)).rstrip()
            if text:
                try:
                    self.on_line(text)
                except Exception:  # noqa: BLE001 - never let UI break the stream
                    pass
        super().linefeed()


class CoreSession:
    """One child process attached to a PTY and a pyte screen."""

    def __init__(
        self,
        core_id: int,
        argv: list[str],
        cols: int = 80,
        rows: int = 24,
        cwd: str | None = None,
        term: str = "xterm-256color",
    ) -> None:
        self.core_id = core_id
        self.argv = argv
        self.cols = cols
        self.rows = rows
        self.cwd = cwd
        self.term = term
        self.pid: int | None = None
        self.master_fd: int | None = None
        self.alive = False
        self.screen = LineEmittingScreen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        # Whether the child enabled xterm mouse reporting (so we know whether
        # forwarding wheel events as mouse escape sequences makes sense).
        self.mouse_on = False
        self.mouse_sgr = False
        # Application cursor-key mode (DECCKM): gdb's TUI enables it, which means
        # arrow keys must be sent as SS3 (ESC O x) instead of CSI (ESC [ x).
        self.app_cursor = False

    def start(self) -> None:
        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                if self.cwd:
                    os.chdir(self.cwd)
                env = os.environ.copy()
                env["TERM"] = self.term
                # Do NOT pin COLUMNS/LINES: ncurses (gdb TUI) prefers them over
                # the live winsize, which would freeze the TUI at the initial
                # width and leave blank space on the right after a resize.
                os.execvpe(self.argv[0], self.argv, env)
            except Exception:  # noqa: BLE001
                os._exit(127)
        self.pid = pid
        self.master_fd = master_fd
        self.alive = True
        set_winsize(master_fd, self.rows, self.cols)

    def feed(self, data: bytes) -> None:
        self._track_mouse_mode(data)
        self.stream.feed(data)

    def _track_mouse_mode(self, data: bytes) -> None:
        # gdb's TUI (ncurses) toggles mouse reporting via DEC private modes.
        # Track 1000/1002/1003 (any motion mode) for on/off and 1006 for SGR.
        if b"\x1b[?" not in data:
            return
        for match in _MOUSE_MODE_RE.finditer(data):
            mode, action = match.group(1), match.group(2)
            on = action == b"h"
            if mode == b"1":
                self.app_cursor = on
            elif mode in (b"1000", b"1002", b"1003"):
                self.mouse_on = on
            elif mode == b"1006":
                self.mouse_sgr = on

    def write(self, data: bytes) -> None:
        if self.master_fd is None:
            return
        try:
            os.write(self.master_fd, data)
        except OSError:
            self.alive = False

    def write_line(self, line: str) -> None:
        self.write(line.encode() + b"\r")

    def interrupt(self) -> None:
        self.write(b"\x03")

    def wheel(self, up: bool, col: int, row: int) -> bool:
        """Forward a mouse-wheel notch as a terminal mouse event.

        Returns False (and writes nothing) if the child has not enabled mouse
        reporting, so we never inject garbage into a plain CLI prompt.
        ``col``/``row`` are 1-based cell coordinates inside the terminal.
        """
        if not self.mouse_on:
            return False
        col = max(1, col)
        row = max(1, row)
        button = 64 if up else 65  # xterm wheel up / down
        if self.mouse_sgr:
            seq = f"\x1b[<{button};{col};{row}M".encode()
        else:
            seq = b"\x1b[M" + bytes(
                (min(button + 32, 255), min(col + 32, 255), min(row + 32, 255))
            )
        self.write(seq)
        return True

    def resize(self, rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        if rows != self.rows or cols != self.cols:
            self.rows, self.cols = rows, cols
            self.screen.resize(rows, cols)
        # Always (re)assert winsize + SIGWINCH, even when dimensions are
        # unchanged: the initially-focused core often gets its first resize
        # before gdb is ready and would otherwise never learn the real size.
        if self.master_fd is not None:
            set_winsize(self.master_fd, self.rows, self.cols)
            if self.pid is not None:
                try:
                    os.kill(self.pid, signal.SIGWINCH)
                except ProcessLookupError:
                    self.alive = False

    def last_line(self) -> str:
        for line in reversed(self.screen.display):
            if line.strip():
                return line.rstrip()
        return ""

    def status(self) -> str:
        if not self.alive:
            return "DEAD"
        return "STOP" if _PROMPT_RE.search(self.last_line()) else "RUNNING"

    def close(self) -> None:
        self.alive = False
        if self.pid is not None:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


class SessionManager:
    """Owns N core sessions and provides focus / broadcast helpers."""

    def __init__(
        self,
        argv_by_core: dict[int, list[str]],
        core_ids: list[int],
        cols: int,
        rows: int,
        cwd: str | None = None,
    ) -> None:
        self.sessions: dict[int, CoreSession] = {
            cid: CoreSession(cid, list(argv_by_core[cid]), cols=cols, rows=rows, cwd=cwd)
            for cid in core_ids
        }
        self.core_ids = core_ids
        self.focused = core_ids[0]

    @classmethod
    def from_command(
        cls,
        command: str,
        core_ids: list[int],
        cols: int,
        rows: int,
        cwd: str | None = None,
    ) -> "SessionManager":
        argv = shlex.split(command)
        return cls({cid: argv for cid in core_ids}, core_ids, cols, rows, cwd)

    def start_all(self) -> None:
        for session in self.sessions.values():
            session.start()

    def focus(self, core_id: int) -> None:
        if core_id in self.sessions:
            self.focused = core_id

    @property
    def focused_session(self) -> CoreSession:
        return self.sessions[self.focused]

    def broadcast(self, line: str) -> None:
        for session in self.sessions.values():
            session.write_line(line)

    def interrupt_all(self) -> None:
        for session in self.sessions.values():
            session.interrupt()

    def close_all(self) -> None:
        for session in self.sessions.values():
            session.close()
