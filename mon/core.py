"""Shared building blocks: theme, key reader, graphs, bars, panel base class, standalone runner."""
from __future__ import annotations

import math
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from rich import box as rich_box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel as RichPanel
from rich.text import Text

from .host import (HOME, cached, cpu_temperature, default_iface, read_file, read_int, read_psi,  # noqa: F401 (re-exported)
                   temperatures, tilde)

PACKAGE_DIR = Path(__file__).resolve().parent
BUNDLED_GIFS = PACKAGE_DIR / "gifs"                 # shipped with the package (read-only once installed)
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "mon"
CONFIG_FILE = CONFIG_DIR / "dash.toml"
USER_GIFS = CONFIG_DIR / "gifs"                     # the user's own animations

# ----------------------------------------------------------------------------- formatting


def human(n: float, suffix: str = "") -> str:
    n = float(n or 0)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            if unit == "B":
                return f"{n:.0f}{unit}{suffix}"
            return (f"{n:.1f}" if n < 100 else f"{n:.0f}") + f"{unit}{suffix}"
        n /= 1024
    return f"{n:.1f}T{suffix}"


def rate(n: float) -> str:
    return human(n, "/s")


def duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--"
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def fit(text: str, width: int, tail: bool = False) -> str:
    """Clip text to width with an ellipsis (keeps the end if tail=True)."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return ("…" + text[-(width - 1):]) if tail else (text[: width - 1] + "…")


def pad(text: str, width: int, align: str = "left") -> str:
    text = fit(text, width)
    if align == "right":
        return text.rjust(width)
    if align == "center":
        return text.center(width)
    return text.ljust(width)


def nice_max(v: float, floor: float = 1.0) -> float:
    """Round v up to 1/2/5 × 10^k so graph scales don't jitter."""
    v = max(v, floor)
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


# ----------------------------------------------------------------------------- theme


BOXES = {"rounded": rich_box.ROUNDED, "square": rich_box.SQUARE, "heavy": rich_box.HEAVY,
         "double": rich_box.DOUBLE, "minimal": rich_box.MINIMAL, "simple": rich_box.SIMPLE, "ascii": rich_box.ASCII}


@dataclass
class Theme:
    accent: str = "cyan"
    border: str = "grey42"
    focus: str = "bright_cyan"
    title: str = "bold"
    dim: str = "grey58"
    good: str = "green"
    warn: str = "yellow"
    bad: str = "red"
    graph: list[str] = field(default_factory=lambda: ["green", "yellow", "red"])
    graph_color: str = "value"          # "value" (colour follows the sample) or "height" (btop-style bands)
    bar: list[str] = field(default_factory=lambda: ["green", "yellow", "red"])
    bar_fill: str = "█"
    bar_empty: str = "░"
    bar_empty_style: str = "grey23"
    thresholds: list[float] = field(default_factory=lambda: [60.0, 85.0])
    box: str = "rounded"
    title_index: bool = True             # show [n] hotkey in panel titles inside the dashboard

    @classmethod
    def from_dict(cls, d: dict) -> "Theme":
        t = cls()
        for k, v in (d or {}).items():
            if hasattr(t, k):
                setattr(t, k, v)
        return t

    def rich_box(self):
        return BOXES.get(self.box, rich_box.ROUNDED)

    def palette_at(self, palette: list[str], frac: float) -> str:
        if not palette:
            return ""
        idx = int(max(0.0, min(0.999, frac)) * len(palette))
        return palette[idx]

    def level(self, pct: float) -> str:
        lo, hi = (self.thresholds + [60, 85])[:2]
        return self.bad if pct >= hi else self.warn if pct >= lo else self.good

    def temp_style(self, celsius: float, warn: float = 75.0, bad: float = 90.0) -> str:
        """Colour for a temperature; CPUs use the defaults, GPUs pass their own marks."""
        return self.bad if celsius >= bad else self.warn if celsius >= warn else self.good


# ----------------------------------------------------------------------------- history


class Hist:
    """Ring buffer of floats with a rolling max helper."""

    def __init__(self, maxlen: int = 900) -> None:
        self.data: deque[float] = deque(maxlen=maxlen)

    def push(self, v: float) -> None:
        self.data.append(float(v))

    def last(self, default: float = 0.0) -> float:
        return self.data[-1] if self.data else default

    def peak(self, n: int | None = None) -> float:
        if not self.data:
            return 0.0
        seq = list(self.data)[-n:] if n else self.data
        return max(seq)

    def __len__(self) -> int:
        return len(self.data)


# ----------------------------------------------------------------------------- drawing

# braille dot bits, bottom → top, for the left and right column of a cell
_DOTS = ((0x40, 0x04, 0x02, 0x01), (0x80, 0x20, 0x10, 0x08))
SPARK = "▁▂▃▄▅▆▇█"


def braille_graph(values: Iterable[float], width: int, height: int, vmax: float, theme: Theme,
                  palette: list[str] | None = None, invert: bool = False) -> Text:
    """Filled area graph, 2 samples per column, 4 dots per row. Newest sample on the right."""
    palette = palette or theme.graph
    width, height = max(1, width), max(1, height)
    pts = list(values)[-2 * width:]
    fracs: list[float | None] = [None] * (2 * width - len(pts))
    fracs += [max(0.0, min(1.0, v / vmax)) if vmax > 0 else 0.0 for v in pts]
    dot_h = height * 4
    dots = [None if f is None else (0 if f == 0 else max(1, round(f * dot_h))) for f in fracs]
    out = Text(no_wrap=True)
    row_order = range(height - 1, -1, -1) if invert else range(height)
    for n, r in enumerate(row_order):
        base = (height - 1 - r) * 4
        run, run_style = [], None
        for c in range(width):
            ch = 0
            colour_frac = 0.0
            for k in (0, 1):
                d = dots[2 * c + k]
                if d is None:
                    continue
                fill = max(0, min(4, d - base))
                for i in range(fill):
                    ch |= _DOTS[k][i]
                f = fracs[2 * c + k]
                colour_frac = max(colour_frac, f if f is not None else 0.0)
            if ch and invert:
                ch = _flip(ch)
            style = ""
            if ch:
                if theme.graph_color == "height":
                    style = theme.palette_at(palette, (height - r - 0.5) / height)
                else:
                    style = theme.palette_at(palette, colour_frac)
            glyph = chr(0x2800 + ch) if ch else " "
            if style != run_style and run:
                out.append("".join(run), run_style or "")
                run = []
            run_style = style
            run.append(glyph)
        if run:
            out.append("".join(run), run_style or "")
        if n < height - 1:
            out.append("\n")
    return out


def _flip(ch: int) -> int:
    """Mirror a braille cell vertically (for upside-down graphs)."""
    out = 0
    for col in (0, 1):
        for i, bit in enumerate(_DOTS[col]):
            if ch & bit:
                out |= _DOTS[col][3 - i]
    return out


def sparkline(values: Iterable[float], width: int, vmax: float, theme: Theme, palette: list[str] | None = None) -> Text:
    palette = palette or theme.graph
    pts = list(values)[-width:]
    out = Text(no_wrap=True)
    out.append(" " * (width - len(pts)))
    for v in pts:
        f = max(0.0, min(1.0, v / vmax)) if vmax > 0 else 0.0
        out.append(SPARK[min(7, int(f * 8))] if f > 0 else " ", theme.palette_at(palette, f))
    return out


def bar(frac: float, width: int, theme: Theme, palette: list[str] | None = None) -> Text:
    """Horizontal meter; each filled cell is coloured by its position (btop-style gradient)."""
    palette = palette or theme.bar
    width = max(1, width)
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    out = Text(no_wrap=True)
    run, run_style = [], None
    for i in range(filled):
        style = theme.palette_at(palette, (i + 0.5) / width)
        if style != run_style and run:
            out.append("".join(run), run_style)
            run = []
        run_style = style
        run.append(theme.bar_fill)
    if run:
        out.append("".join(run), run_style)
    if filled < width:
        out.append(theme.bar_empty * (width - filled), theme.bar_empty_style)
    return out


def meter(label: str, frac: float, value: str, width: int, theme: Theme, label_w: int = 6, value_w: int = 6,
          palette: list[str] | None = None) -> Text:
    """`label ▐████░░░░▌ value` on one line, sized to width."""
    bar_w = width - label_w - value_w - 2
    if bar_w < 3:
        return Text.assemble((pad(label, label_w), theme.dim), " ", (pad(value, width - label_w - 1, "right"), theme.level(frac * 100)))
    return Text.assemble((pad(label, label_w), theme.dim), " ", bar(frac, bar_w, theme, palette), " ",
                         (pad(value, value_w, "right"), theme.level(frac * 100)))


def kv_line(pairs: list[tuple[str, str, str | None]], width: int, theme: Theme, sep: str = "  ") -> Text:
    """Pack `label value` pairs on one line, dropping trailing pairs that don't fit."""
    out = Text(no_wrap=True)
    used = 0
    for i, (label, value, style) in enumerate(pairs):
        chunk = len(label) + (1 if label else 0) + len(value) + (len(sep) if i else 0)
        if used + chunk > width:
            break
        if i:
            out.append(sep)
        if label:
            out.append(label + " ", theme.dim)
        out.append(value, style or "")
        used += chunk
    return out


def lines(*items: RenderableType) -> Group:
    return Group(*items)


# ----------------------------------------------------------------------------- stacked entries (downloads, jobs)


def density(n: int, height: int, extra: int = 0) -> tuple[int, int]:
    """How many lines each of n entries may take (3, 2 or 1) so they fit in `height` with `extra` lines of
    sections below them. Returns (per_entry, extra_kept): the sections are dropped when even 1 line per
    entry would not leave room for them."""
    per = 3 if n * 3 + extra <= height else 2 if n * 2 + extra <= height else 1
    if per == 1 and n + extra > height:
        extra = 0
    return per, extra


def entry_rows(per: int, glyph: str, name: str, name_style: str, head_note: str, stats: Text, width: int,
               theme: Theme, bar_fn=None, stats_w: int = 0, tail_note: str = "") -> list[Text]:
    """1, 2 or 3 lines for one entry, the same shape the downloads and jobs panels use:

        per=1   ◆ name……………  ████░░  stats
        per=2   ◆ name                       head_note
                ████████████████░░░░░  stats
        per=3   head, a full-width bar, then stats + tail_note

    bar_fn(width) draws the bar for a given width (None: no bar). stats_w aligns the stats column across
    entries in the one-line layout."""
    if per == 1:
        bar_w = max(0, min(20, width // 4)) if bar_fn else 0
        name_w = max(8, width - bar_w - max(stats_w, len(stats.plain)) - 4)
        line = Text.assemble((glyph, name_style), (fit(name, name_w).ljust(name_w), name_style), " ")
        if bar_fn and bar_w >= 4:
            line.append_text(bar_fn(bar_w))
            line.append(" ")
        line.append_text(stats)
        line.no_wrap = True
        return [line]
    head = Text.assemble((glyph, name_style), (fit(name, max(6, width - len(head_note) - 2)), name_style), (head_note, theme.dim))
    head.no_wrap = True
    if per == 2:
        bar_w = max(0, width - len(stats.plain) - 1)
        line = Text(no_wrap=True)
        if bar_fn and bar_w >= 6:
            line.append_text(bar_fn(bar_w))
            line.append(" ")
        line.append_text(stats)
        return [head, line]
    out = [head, bar_fn(width) if bar_fn else Text(theme.bar_empty * width, style=theme.bar_empty_style)]
    tail = Text(no_wrap=True)
    tail.append_text(stats)
    if tail_note and len(tail.plain) + len(tail_note) <= width:
        tail.append(tail_note, theme.dim)
    out.append(tail)
    return out


def finished_rows(entries, width: int, theme: Theme, limit: int) -> list[Text]:
    """`finished:` section shared by the downloads and jobs panels: (when, name, note) triples."""
    if limit <= 0 or not entries:
        return []
    rows = [Text("finished:", style=theme.dim)]
    for when, name, note in list(entries)[: limit - 1]:
        line = Text.assemble((f"{when} ", theme.dim), ("✔ ", theme.good), fit(name, max(4, width - 12 - len(note))), (f" {note}", theme.dim))
        line.no_wrap = True
        rows.append(line)
    return rows


# ----------------------------------------------------------------------------- keys


_KEYMAP = {
    "\x1b[A": "UP", "\x1b[B": "DOWN", "\x1b[C": "RIGHT", "\x1b[D": "LEFT",
    "\x1b[Z": "BTAB", "\t": "TAB", "\r": "ENTER", "\n": "ENTER", "\x1b": "ESC",
    "\x7f": "BACKSPACE", "\x08": "BACKSPACE", "\x1b[5~": "PGUP", "\x1b[6~": "PGDN",
    "\x1b[H": "HOME", "\x1b[1~": "HOME", "\x1b[F": "END", "\x1b[4~": "END", "\x1b[3~": "DEL",
    "\x1bOA": "UP", "\x1bOB": "DOWN", "\x1bOC": "RIGHT", "\x1bOD": "LEFT",
}


_MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"


class Keys:
    """Non-blocking key reader with arrow/escape-sequence and mouse decoding. Inert when stdin is not a tty.

    Mouse events come back as key names: "CLICK:x:y", "RCLICK:x:y", "WHEELUP:x:y", "WHEELDOWN:x:y" (1-based cells).
    """

    def __init__(self, mouse: bool = False) -> None:
        self.fd = sys.stdin.fileno() if sys.stdin.isatty() else None
        self.saved = termios.tcgetattr(self.fd) if self.fd is not None else None
        self.queue: deque[str] = deque()
        self.mouse = mouse and self.fd is not None
        self._mouse_active = False

    def set_mouse(self, on: bool) -> None:
        if self.fd is None:
            return
        out = sys.__stdout__.fileno()
        if on and not self._mouse_active:
            os.write(out, MOUSE_ON.encode())
        elif not on and self._mouse_active:
            os.write(out, MOUSE_OFF.encode())
        self._mouse_active = on

    def __enter__(self) -> "Keys":
        if self.fd is not None:
            tty.setcbreak(self.fd)
            if self.mouse:
                self.set_mouse(True)
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            self.set_mouse(False)
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def _fill(self, timeout: float) -> None:
        if self.fd is None:
            time.sleep(timeout)
            return
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return
        data = os.read(self.fd, 512).decode(errors="ignore")

        def mouse(m: re.Match) -> str:
            b, x, y, kind = int(m.group(1)), m.group(2), m.group(3), m.group(4)
            if b & 64:
                self.queue.append(("WHEELUP" if b & 1 == 0 else "WHEELDOWN") + f":{x}:{y}")
            elif kind == "M" and not b & 32:            # press, not drag
                btn = b & 3
                self.queue.append(("CLICK" if btn == 0 else "MCLICK" if btn == 1 else "RCLICK") + f":{x}:{y}")
            return ""

        data = _MOUSE_RE.sub(mouse, data)
        i = 0
        while i < len(data):
            matched = False
            for n in (4, 3, 2):
                seq = data[i:i + n]
                if len(seq) == n and seq in _KEYMAP:
                    self.queue.append(_KEYMAP[seq])
                    i += n
                    matched = True
                    break
            if not matched:
                ch = data[i]
                self.queue.append(_KEYMAP.get(ch, ch))
                i += 1

    def poll(self, timeout: float = 0.0) -> str | None:
        """Return the next key name, waiting at most `timeout` seconds."""
        if not self.queue:
            self._fill(timeout)
        return self.queue.popleft() if self.queue else None


# ----------------------------------------------------------------------------- screen


class Screen:
    """Alternate-screen renderer that rewrites only the rows that changed since the last frame.

    rich's Live repaints the whole screen every refresh; for a 1 Hz dashboard that is fine, for an
    animation it is most of the CPU. Here each frame is rendered to per-row ANSI strings and only
    rows that differ are written, with a cursor move in front of each.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self.fd = sys.__stdout__.fileno()
        self.prev: list[str] = []
        self.size: tuple[int, int] | None = None
        from rich.console import ColorSystem
        self.cs = console._color_system or ColorSystem.TRUECOLOR

    def _write(self, s: str) -> None:
        data = s.encode()
        while data:
            n = os.write(self.fd, data)
            data = data[n:]

    def __enter__(self) -> "Screen":
        self._write("\x1b[?1049h\x1b[?25l\x1b[H\x1b[2J")
        return self

    def __exit__(self, *_: object) -> None:
        self._write("\x1b[0m\x1b[?25h\x1b[?1049l")

    def _ansi(self, segments) -> str:
        parts = []
        for seg in segments:
            if seg.control:
                continue
            parts.append(seg.style.render(seg.text, color_system=self.cs) if seg.style else seg.text)
        return "".join(parts)

    def draw(self, renderable: RenderableType, width: int, height: int, force: bool = False) -> int:
        """Render and flush; returns the number of rows written."""
        opts = self.console.options.update_dimensions(width, height)
        lines = self.console.render_lines(renderable, opts, pad=True, new_lines=False)
        rows = [self._ansi(line) for line in lines[:height]]
        out = []
        if force or self.size != (width, height):
            out.append("\x1b[H\x1b[2J")
            self.prev = []
            self.size = (width, height)
        written = 0
        for i, row in enumerate(rows):
            if i >= len(self.prev) or self.prev[i] != row:
                out.append(f"\x1b[{i + 1};1H{row}\x1b[0m")
                written += 1
        self.prev = rows
        if out:
            self._write("".join(out))
        return written


# ----------------------------------------------------------------------------- panel base


class Panel:
    """A monitor. Subclass: set name/title, implement sample() and render(); optionally on_key()/help."""

    name = "panel"
    title = "Panel"
    interval = 1.0
    help: dict[str, str] = {}          # key -> description, shown in the dashboard help
    options: dict[str, str] = {}       # option -> description, for --set and dash.toml
    takes_enter = False                # True: Enter goes to the panel before the dashboard's zoom
    wants_hide = False                 # set by a panel to ask the dashboard to hide it (standalone: quit)

    def __init__(self, cfg: dict, theme: Theme) -> None:
        self.cfg = cfg or {}
        self.theme = theme
        self.title = self.cfg.get("title", self.title)
        self.interval = float(self.cfg.get("interval", self.interval))
        self._last = 0.0
        self.version = 0            # bumped whenever the panel's picture may have changed (render cache key)

    # lifecycle ---------------------------------------------------------------
    def due(self, now: float) -> bool:
        return now - self._last >= self.interval - 1e-3

    def tick(self, now: float) -> None:
        dt = now - self._last if self._last else self.interval
        self._last = now
        try:
            changed = self.sample(max(0.05, dt))
            if self._error:                   # a transient failure (a device that went away for a tick) clears itself
                self._error = None
                changed = True
        except Exception as exc:  # a broken sensor must never take the dashboard down
            self._error = repr(exc)
            changed = True
        if changed is not False:          # sample() may return False to say "nothing new, don't repaint"
            self.version += 1

    _error: str | None = None

    def sample(self, dt: float):
        """Collect data. Return False when nothing changed (skips the repaint); anything else repaints."""

    def render(self, width: int, height: int) -> RenderableType:
        return Text("")

    def safe_render(self, width: int, height: int) -> RenderableType:
        try:
            return self.render(width, height)
        except Exception as exc:
            return Text(f"render error: {exc!r}", style=self.theme.bad)

    def on_key(self, key: str) -> bool:
        """Handle a key. Return True if consumed (forces a redraw)."""
        return False

    def on_mouse(self, kind: str, x: int, y: int) -> bool:
        """Mouse event inside the panel body; x/y are 0-based body coordinates. Return True to redraw."""
        return False

    def status(self) -> str:
        """Short text for the frame's bottom-right corner (sort mode, interface, …)."""
        return ""

    def close(self) -> None: ...


def frame(panel: Panel, body: RenderableType, width: int, height: int, theme: Theme,
          focused: bool = False, index: int | None = None, zoomed: bool = False) -> RichPanel:
    title = Text(no_wrap=True)
    if index is not None and theme.title_index:
        title.append(f"{index}", theme.accent if not focused else theme.focus)
        title.append(" ")
    title.append(panel.title, f"{theme.title} {theme.focus}" if focused else theme.title)
    if zoomed:
        title.append("  zoomed · Esc", theme.dim)
    status = panel.status()
    return RichPanel(body, title=title, title_align="left", subtitle=Text(status, style=theme.dim) if status else None,
                     subtitle_align="right", box=theme.rich_box(),
                     border_style=theme.focus if focused else theme.border, padding=0, width=width, height=height)


# ----------------------------------------------------------------------------- config


DEFAULT_CONFIG = '''# mon dashboard config — edit freely, `r` inside mondash reloads it.
refresh = 1.0            # seconds between dashboard ticks (each panel also has its own interval)
layout = "default"       # layout to start with; `l` cycles through them
hints = true             # one-line key bar at the bottom (global keys + the focused panel's keys)
mouse = true             # click to focus / sort / select, wheel to scroll; `m` toggles (off = terminal text selection)
focus = "proc"           # panel that starts focused

[theme]
accent = "cyan"
border = "grey42"
focus  = "bright_cyan"
dim    = "grey58"
graph  = ["green", "yellow", "red"]     # low → high
graph_color = "value"                   # or "height" for btop-style horizontal bands
bar    = ["green", "yellow", "red"]
bar_fill = "█"
bar_empty = "░"
bar_empty_style = "grey23"
thresholds = [60, 85]                   # % where colours turn warn / bad
box = "rounded"                         # rounded square heavy double minimal ascii
title_index = true

# Layouts are ASCII grids, like CSS grid-template-areas. Same name in adjacent
# cells = one panel spanning them. "." = empty. Rows must all have the same
# number of cells. `rows` / `cols` are optional weights; a quoted number is a
# fixed size in lines / columns ("3"), a bare number is a share of the rest.
# `hidden` lists panels that start hidden in that layout (a neighbour takes
# their space); `g` toggles the animation panel, the ✕ button hides any panel.

[layouts.default]
rows = ["3", 3, 3, 3, 3, 3, 3]
cols = [2, 2, 2]
hidden = ["gif"]
grid = """
sys  sys  sys
cpu  cpu  gpu
mem  net  temp
proc proc disk
proc proc dl
proc proc gif
jobs jobs jobs
"""

[layouts.wide]
rows = ["3", 2, 2, 3]
cols = [3, 2, 2, 2]
grid = """
sys  sys  sys   sys
cpu  cpu  gpu   temp
mem  net  disk  power
proc proc proc  dl
"""

[layouts.gaming]
rows = [3, 3, 4]
cols = [3, 2]
grid = """
gpu  temp
cpu  net
proc mem
"""

[layouts.downloads]
rows = ["3", 3, 5, 5]
grid = """
sys  sys
net  disk
dl   dl
jobs jobs
"""

[layouts.minimal]
grid = """
cpu mem
net proc
"""

[layouts.info]
rows = ["3", 3, 2]
cols = [3, 2]
grid = """
sys  sys
hw   gif
hw   temp
"""

# Per-panel options. A grid name with no [panels.NAME] entry uses the panel type of the same name.
[panels.cpu]
interval = 1.0

[panels.mem]

[panels.net]
# iface = "eth0"            # default: the interface that holds the default route

[panels.disk]
interval = 2.0

[panels.proc]
interval = 2.0
sort = "cpu"                # cpu mem pid name user
kernel = false              # show kernel threads (toggle with h)
rows_cmd = 90               # show the command column when the panel is at least this wide

[panels.gpu]
interval = 1.0

[panels.temp]
interval = 2.0

[panels.power]
interval = 2.0

[panels.sys]
# items = ["host", "os", "kernel", "uptime", "load", "tasks", "temp", "disk", "bat", "psi", "clock"]

[panels.dl]
interval = 1.0
# tools = ["axel", "lftp"]  # extra programs that are downloads on this machine (process names; `mondash --doctor` lists candidates)
# ignore = ["rsync"]        # never list these

[panels.jobs]
interval = 1.0
busy = true                 # also list processes burning CPU that are not a recognised job (`b` toggles)
# busy_cpu = 40             # CPU % a process must hold, for busy_after seconds, to be listed there
# busy_after = 8
# busy_ignore = ["blender"] # names never listed as busy (browsers, compositors, players and VMs already are)
# tools = { myencoder = "media", buildthing = "build" }   # extra programs that are jobs: process name → kind
#                           # kinds: compile link build test lint container media archive copy backup system python vcs ml db vm iac script
# ignore = ["cat"]          # never a job, even if built in
finished = 4                # finished jobs kept on screen (`c` clears)

[panels.hw]
# sections = ["system", "cpu", "memory", "gpu", "storage", "network", "audio", "display", "battery", "os"]

[panels.gif]
# folder = "~/Pictures/gifs"           # default: ~/.config/mon/gifs; the gifs shipped with mondash and the built-in effects are listed too
# file = "plasma"                      # start playing this gif path or effect instead of showing the list
fps = 8
hidden = false                         # start hidden; `g` or the panel's ✕ button toggles it

# Any shell command becomes a panel:
# [panels.journal]
# type = "cmd"
# title = "journal"
# command = "journalctl -n 30 --no-pager -o cat"
# interval = 5
'''


def default_config() -> dict:
    import tomllib
    return tomllib.loads(DEFAULT_CONFIG)


def load_config(path: Path | None = None) -> dict:
    import tomllib
    path = path or CONFIG_FILE
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_CONFIG)
        except OSError:
            return default_config()          # read-only home (sandbox, CI): run with the defaults
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"mon: cannot parse {path}: {exc}")


# ----------------------------------------------------------------------------- registry


def panel_types() -> dict[str, type[Panel]]:
    from . import panels
    return panels.TYPES


def make_panel(name: str, cfg: dict, theme: Theme) -> Panel:
    types = panel_types()
    kind = cfg.get("type", name)
    if kind not in types:
        raise SystemExit(f"mon: unknown panel type '{kind}' for '{name}'. Available: {', '.join(sorted(types))}")
    p = types[kind](cfg, theme)
    p.name = name
    return p


# ----------------------------------------------------------------------------- standalone runner


def parse_sets(items: list[str] | None) -> dict:
    out: dict = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--set expects key=value, got '{item}'")
        k, v = item.split("=", 1)
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


TERMINALS = (("konsole", ["konsole", "--title", "{title}", "-e"]), ("ptyxis", ["ptyxis", "--"]),
             ("gnome-terminal", ["gnome-terminal", "--"]), ("kitty", ["kitty", "--title", "{title}"]),
             ("foot", ["foot", "-T", "{title}"]), ("wezterm", ["wezterm", "start", "--"]),
             ("alacritty", ["alacritty", "-T", "{title}", "-e"]), ("xfce4-terminal", ["xfce4-terminal", "-T", "{title}", "-x"]),
             ("tilix", ["tilix", "-t", "{title}", "-e"]), ("xterm", ["xterm", "-T", "{title}", "-e"]))


def find_terminal(title: str) -> list[str] | None:
    """Command prefix that opens a new terminal window running what follows it; $TERMINAL first."""
    want = os.environ.get("TERMINAL")
    order = ([(want, [want, "-e"])] if want and shutil.which(want) else []) + list(TERMINALS)
    for term, pre in order:
        if shutil.which(term):
            return [p.replace("{title}", title) for p in pre]
    return None


def relaunch_in_window(argv: list[str], title: str) -> None:
    pre = find_terminal(title)
    if pre is None:
        sys.exit("mon: no terminal emulator found (set $TERMINAL)")
    subprocess.Popen(pre + argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def self_argv(drop: tuple[str, ...] = ("-w", "--window")) -> list[str]:
    """This process's command line minus the given flags, for re-running ourselves in a new window."""
    return [sys.argv[0]] + [x for x in sys.argv[1:] if x not in drop]


def run_standalone(kind: str, argv: list[str] | None = None) -> None:
    """Entry point used by the per-panel scripts (cpumon, netmon, …)."""
    import argparse

    types = panel_types()
    cls = types[kind]
    ap = argparse.ArgumentParser(prog=f"{kind}mon", description=f"{cls.title} monitor (mon panel '{kind}')")
    ap.add_argument("-i", "--interval", type=float, help="seconds between samples")
    ap.add_argument("-w", "--window", action="store_true", help="open in a new terminal window")
    ap.add_argument("-b", "--bare", action="store_true", help="no frame around the panel")
    ap.add_argument("--no-mouse", action="store_true", help="leave the mouse to the terminal (text selection)")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="panel option (see --options)" if cls.options else "panel option")
    ap.add_argument("--options", action="store_true", help="list the panel's options and keys")
    ap.add_argument("-c", "--config", type=Path, help=f"dash.toml to read theme/panel options from (default {CONFIG_FILE})")
    a = ap.parse_args(argv)

    if a.options:
        print(f"{cls.title} — options:")
        for k, v in ({"interval": "seconds between samples", "title": "frame title"} | cls.options).items():
            print(f"  {k:<14} {v}")
        if cls.help:
            print("keys:")
            for k, v in cls.help.items():
                print(f"  {k:<14} {v}")
        return
    if a.window:
        relaunch_in_window(self_argv(), cls.title)
        return

    conf = load_config(a.config)
    theme = Theme.from_dict(conf.get("theme", {}))
    mouse = bool(conf.get("mouse", True)) and not a.no_mouse
    cfg = dict(conf.get("panels", {}).get(kind, {}))
    cfg.update(parse_sets(a.set))
    if a.interval:
        cfg["interval"] = a.interval
    panel = make_panel(kind, cfg, theme)
    console = Console()
    try:
        run_panel(panel, console, theme, framed=not a.bare, mouse=mouse)
    except KeyboardInterrupt:
        pass
    finally:
        panel.close()


def run_panel(panel: Panel, console: Console, theme: Theme, framed: bool = True, mouse: bool = True) -> None:
    with Keys(mouse=mouse) as keys, Screen(console) as screen:
        dirty = True
        size = console.size
        while True:
            now = time.monotonic()
            if panel.due(now):
                v = panel.version
                panel.tick(now)
                dirty = dirty or panel.version != v
            if console.size != size:
                size = console.size
                dirty = True
            if dirty:
                w, h = size
                if framed:
                    screen.draw(frame(panel, panel.safe_render(w - 2, h - 2), w, h, theme), w, h)
                else:
                    screen.draw(panel.safe_render(w, h), w, h)
                dirty = False
            if panel.wants_hide:
                return
            key = keys.poll(0.05)
            if key == "q" and not getattr(panel, "typing", False):
                return
            if key and ":" in key:
                kind, x, y = key.split(":")
                off = 1 if framed else 0
                if panel.on_mouse(kind, int(x) - 1 - off, int(y) - 1 - off):
                    dirty = True
            elif key and panel.on_key(key):
                dirty = True
