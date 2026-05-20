"""GdbTerminal: a Textual widget that renders a CoreSession's pyte screen and
forwards keystrokes to its PTY.  This is what makes the right pane show gdb's
native display (CLI or TUI) and stay interactive.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual import events
from textual.widget import Widget

from .backend import CoreSession

_NAMED_COLORS = {"black", "red", "green", "blue", "magenta", "cyan", "white"}

# Named keys -> bytes to write to the PTY. Printable chars fall back to
# event.character (covers letters, digits, symbols and ctrl combos).
_KEY_BYTES = {
    "enter": b"\r",
    "tab": b"\t",
    "backspace": b"\x7f",
    "escape": b"\x1b",
    "space": b" ",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "delete": b"\x1b[3~",
    "insert": b"\x1b[2~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
}

# In application cursor-key mode (DECCKM, set by ncurses' smkx when gdb enters
# TUI), arrows/home/end must use SS3 (ESC O x) instead of CSI (ESC [ x).
_KEY_BYTES_APP = {
    "up": b"\x1bOA",
    "down": b"\x1bOB",
    "right": b"\x1bOC",
    "left": b"\x1bOD",
    "home": b"\x1bOH",
    "end": b"\x1bOF",
}


def _color(value: str) -> str | None:
    if value == "default":
        return None
    if value == "brown":
        return "yellow"
    if value in _NAMED_COLORS:
        return value
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return "#" + value
    return None


# Cache Style objects by their attribute tuple. Building Style is relatively
# expensive and a terminal reuses very few distinct styles, so this avoids
# thousands of allocations per repaint.
_STYLE_CACHE: dict[tuple, Style] = {}


def _style(key: tuple) -> Style:
    style = _STYLE_CACHE.get(key)
    if style is None:
        fg, bg, bold, italics, underscore, reverse = key
        style = Style(
            color=_color(fg),
            bgcolor=_color(bg),
            bold=bool(bold),
            italic=bool(italics),
            underline=bool(underscore),
            reverse=bool(reverse),
        )
        _STYLE_CACHE[key] = style
    return style


def screen_to_text(screen) -> Text:
    """Convert a pyte screen buffer into a Rich Text block.

    Consecutive cells sharing a style are coalesced into a single span, which
    cuts the number of ``Text.append`` calls and Style allocations by an order
    of magnitude versus per-cell appends.
    """
    text = Text(no_wrap=True, overflow="crop")
    cursor_x, cursor_y = screen.cursor.x, screen.cursor.y
    show_cursor = not screen.cursor.hidden
    lines, columns = screen.lines, screen.columns
    buffer = screen.buffer
    append = text.append
    for y in range(lines):
        row = buffer[y]
        run: list[str] = []
        run_key: tuple | None = None
        for x in range(columns):
            char = row[x]
            reverse = char.reverse
            if show_cursor and x == cursor_x and y == cursor_y:
                reverse = not reverse
            key = (char.fg, char.bg, char.bold, char.italics, char.underscore, reverse)
            if run_key is not None and key != run_key:
                append("".join(run), _style(run_key))
                run = []
            run_key = key
            run.append(char.data or " ")
        if run_key is not None:
            append("".join(run), _style(run_key))
        if y != lines - 1:
            append("\n")
    return text


class GdbTerminal(Widget, can_focus=True):
    """Renders the focused session and forwards keys to it."""

    DEFAULT_CSS = """
    GdbTerminal {
        background: $surface;
        color: $text;
    }
    """

    def __init__(self, session: CoreSession, **kwargs) -> None:
        super().__init__(**kwargs)
        self._session = session

    @property
    def session(self) -> CoreSession:
        return self._session

    def attach(self, session: CoreSession) -> None:
        self._session = session
        self._sync_size()
        self.refresh()

    def render(self) -> Text:
        return screen_to_text(self._session.screen)

    def _sync_size(self) -> None:
        size = self.size
        if size.width > 0 and size.height > 0:
            self._session.resize(size.height, size.width)

    def on_resize(self, event: events.Resize) -> None:
        self._sync_size()
        self.refresh()

    def on_key(self, event: events.Key) -> None:
        data = None
        if self._session.app_cursor:
            data = _KEY_BYTES_APP.get(event.key)
        if data is None:
            data = _KEY_BYTES.get(event.key)
        if data is None and event.character is not None:
            data = event.character.encode()
        if data is not None:
            self._session.write(data)
        event.stop()
        event.prevent_default()

    def _wheel(self, event: events.MouseEvent, up: bool) -> None:
        # Forward the wheel notch a few cells at the pointer so gdb's TUI
        # scrolls the source window under the cursor.
        for _ in range(3):
            self._session.wheel(up, event.x + 1, event.y + 1)
        event.stop()
        event.prevent_default()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(event, up=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(event, up=False)
