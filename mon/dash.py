"""mondash — composes panels on a grid described by an ASCII template in dash.toml."""
from __future__ import annotations

import argparse
import math
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.panel import Panel as RichPanel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from .core import CONFIG_FILE, Keys, Panel, Screen, Theme, frame, load_config, make_panel, relaunch_in_window

# ----------------------------------------------------------------------------- grid


@dataclass
class Region:
    name: str
    row0: int
    col0: int
    row1: int   # inclusive
    col1: int   # inclusive


def parse_grid(spec: str) -> tuple[list[Region], int, int]:
    rows = [line.split() for line in spec.strip().splitlines() if line.strip()]
    if not rows:
        raise SystemExit("layout grid is empty")
    ncols = len(rows[0])
    for i, r in enumerate(rows):
        if len(r) != ncols:
            raise SystemExit(f"layout grid row {i + 1} has {len(r)} cells, expected {ncols}")
    regions: dict[str, Region] = {}
    for ri, r in enumerate(rows):
        for ci, name in enumerate(r):
            if name == ".":
                continue
            reg = regions.get(name)
            if reg is None:
                regions[name] = Region(name, ri, ci, ri, ci)
            else:
                reg.row1, reg.col1 = max(reg.row1, ri), max(reg.col1, ci)
                reg.row0, reg.col0 = min(reg.row0, ri), min(reg.col0, ci)
    for reg in regions.values():   # every cell inside the bounding box must be that name
        for ri in range(reg.row0, reg.row1 + 1):
            for ci in range(reg.col0, reg.col1 + 1):
                if rows[ri][ci] != reg.name:
                    raise SystemExit(f"layout: panel '{reg.name}' is not a rectangle in the grid")
    return list(regions.values()), len(rows), ncols


def split_sizes(total: int, count: int, weights: list | None) -> list[int]:
    """Distribute `total` cells: quoted numbers are fixed, bare numbers are weights of the rest."""
    weights = list(weights or [])
    weights += [1] * (count - len(weights))
    weights = weights[:count]
    fixed = {i: int(w) for i, w in enumerate(weights) if isinstance(w, str)}
    flex = {i: float(w) for i, w in enumerate(weights) if not isinstance(w, str)}
    remaining = max(0, total - sum(fixed.values()))
    wsum = sum(flex.values()) or 1.0
    sizes = [0] * count
    for i, v in fixed.items():
        sizes[i] = v
    acc = 0.0
    shares = []
    for i, w in flex.items():
        exact = remaining * w / wsum
        shares.append((i, exact))
        sizes[i] = int(exact)
        acc += int(exact)
    leftover = remaining - int(acc)
    for i, exact in sorted(shares, key=lambda s: -(s[1] - int(s[1])))[:leftover]:
        sizes[i] += 1
    if sum(sizes) > total:  # fixed sizes exceed the terminal: shrink proportionally
        scale = total / sum(sizes)
        sizes = [int(s * scale) for s in sizes]
        sizes[-1] += total - sum(sizes)
    return sizes


class Composite:
    """Renders each region's panel into its rectangle and stitches the rows together.

    A cell is (x, y, w, h, renderable) or (x, y, w, h, renderable, cache_key). Cells with a key are looked up in
    `cache` and only rendered when the key (size, focus, panel version) changed since the last frame.
    """

    def __init__(self, cells: list[tuple], width: int, height: int, cache: dict | None = None) -> None:
        self.cells, self.width, self.height, self.cache = cells, width, height, cache

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        rows: list[list[tuple[int, int, list[Segment]]]] = [[] for _ in range(self.height)]
        fresh: dict = {}
        for cell in self.cells:
            x, y, w, h, renderable = cell[:5]
            key = cell[5] if len(cell) > 5 else None
            lines = self.cache.get(key) if (key and self.cache is not None) else None
            if lines is None:
                lines = console.render_lines(renderable, options.update_dimensions(w, h), pad=True, new_lines=False)
            if key and self.cache is not None:
                fresh[key] = lines
            for i in range(h):
                if 0 <= y + i < self.height:
                    rows[y + i].append((x, w, lines[i] if i < len(lines) else [Segment(" " * w)]))
        if self.cache is not None:
            self.cache.clear()
            self.cache.update(fresh)
        for yi, parts in enumerate(rows):
            parts.sort(key=lambda p: p[0])
            cur = 0
            for x, w, segs in parts:
                if x > cur:
                    yield Segment(" " * (x - cur))
                yield from segs
                cur = x + w
            if cur < self.width:
                yield Segment(" " * (self.width - cur))
            if yi < self.height - 1:
                yield Segment.line()


# ----------------------------------------------------------------------------- dashboard


GLOBAL_KEYS = {"q": "quit", "Tab / Shift-Tab": "focus next / previous panel", "1-9": "focus panel by number",
               "Enter / f": "zoom focused panel (again to restore)", "l / L": "next / previous layout",
               "space": "pause sampling", "+ / -": "faster / slower refresh", "m": "toggle mouse (off = select text)",
               "r": "reload dash.toml", "g": "hide / show the animation panel(s)",
               "H": "hardware inventory, full screen (Esc or H closes)", "?": "this help",
               "mouse": "click a panel to focus it, click its title bar to zoom, wheel to scroll, click column headers to sort"}
HINT_KEYS = [("q", "quit"), ("Tab", "focus"), ("⏎", "zoom"), ("l", "layout"), ("g", "gif"), ("H", "hardware"), ("?", "help")]
OVERLAYS = {"H": "hw"}      # key -> panel type shown full screen on demand, whatever the layout


class Dashboard:
    def __init__(self, config_path: Path | None, layout: str | None) -> None:
        self.config_path = config_path or CONFIG_FILE
        self.panels: dict[str, Panel] = {}
        self.layout_names: list[str] = []
        self.layout_idx = 0
        self.focus = 0
        self.zoom = False
        self.paused = False
        self.help = False
        self.error: str | None = None
        self.mouse = True
        self.render_cache: dict = {}
        self.hidden: set[str] = set()
        self.overlay: str | None = None
        self._seen_hidden_cfg = set()
        self._seen_layouts = set()
        self.last_rects: list[tuple[str, int, int, int, int]] = []
        self.load(layout)
        self.mouse = bool(self.conf.get("mouse", True))
        want = self.conf.get("focus", "proc")
        if want in self.order:
            self.focus = self.order.index(want)
        else:
            for i, name in enumerate(self.order):
                if self.panels[name].help:
                    self.focus = i
                    break

    # config ------------------------------------------------------------------
    def load(self, layout: str | None = None) -> None:
        conf = load_config(self.config_path)
        self.conf = conf
        self.theme = Theme.from_dict(conf.get("theme", {}))
        self.refresh = float(conf.get("refresh", 1.0))
        self.hints = bool(conf.get("hints", True))
        self.layouts = conf.get("layouts", {})
        if not self.layouts:
            raise SystemExit(f"no [layouts.*] in {self.config_path}")
        self.layout_names = list(self.layouts)
        want = layout or conf.get("layout") or self.layout_names[0]
        self.layout_idx = self.layout_names.index(want) if want in self.layout_names else 0
        old = self.panels
        self.panels = {}
        self.apply_layout(reuse=old)
        for p in old.values():
            if p not in self.panels.values():
                p.close()

    _seen_hidden_cfg: set = set()
    _seen_layouts: set = set()

    def overlay_panel(self) -> Panel | None:
        if not self.overlay:
            return None
        p = self.panels.get(self.overlay)
        if p is None:
            p = make_panel(self.overlay, dict(self.conf.get("panels", {}).get(self.overlay, {})), self.theme)
            self.panels[self.overlay] = p
        return p

    def toggle_overlay(self, name: str) -> None:
        self.overlay = None if self.overlay == name else name
        if self.overlay:
            self.overlay_panel()               # create it now so the next tick samples it
        self.render_cache.clear()

    def focused_panel(self) -> Panel | None:
        if self.overlay:
            return self.overlay_panel()
        return self.panels[self.order[self.focus]] if self.order else None

    def visible_order(self) -> list[str]:
        return [n for n in self.order if n not in self.hidden]

    def toggle_hidden(self, name: str) -> None:
        if name in self.hidden:
            self.hidden.discard(name)
        else:
            self.hidden.add(name)
        self.render_cache.clear()
        vis = self.visible_order()
        if vis and self.order[self.focus] in self.hidden:
            self.focus = self.order.index(vis[0])

    def toggle_gifs(self) -> None:
        names = [n for n in self.order if type(self.panels[n]).__name__ == "GifPanel"]
        for n in names:
            self.toggle_hidden(n)

    def apply_layout(self, reuse: dict[str, Panel] | None = None) -> None:
        lay = self.layouts[self.layout_names[self.layout_idx]]
        self.regions, self.nrows, self.ncols = parse_grid(lay.get("grid", ""))
        self.row_w = lay.get("rows")
        self.col_w = lay.get("cols")
        panel_cfgs = self.conf.get("panels", {})
        reuse = reuse or {}
        new: dict[str, Panel] = {}
        for reg in self.regions:
            if reg.name in self.panels:
                new[reg.name] = self.panels[reg.name]
            elif reg.name in reuse:
                new[reg.name] = reuse[reg.name]
            else:
                new[reg.name] = make_panel(reg.name, dict(panel_cfgs.get(reg.name, {})), self.theme)
        # panels dropped by this layout keep their history (they stay in self.panels) so switching back is seamless
        self.panels.update(new)
        for name, p in new.items():
            if p.cfg.get("hidden") and name not in self._seen_hidden_cfg:
                self.hidden.add(name)
                self._seen_hidden_cfg.add(name)
        # a layout can start some of its panels hidden: hidden = ["gif"]; applied the first time the layout is shown
        lname = self.layout_names[self.layout_idx]
        if lname not in self._seen_layouts:
            self._seen_layouts.add(lname)
            for name in lay.get("hidden", []):
                if name in new:
                    self.hidden.add(name)
        self.order = [reg.name for reg in self.regions]
        self.focus = min(self.focus, len(self.order) - 1)
        self.zoom = False

    @property
    def layout_name(self) -> str:
        return self.layout_names[self.layout_idx]

    # rendering ---------------------------------------------------------------
    def rects(self, width: int, height: int) -> list[tuple[str, int, int, int, int]]:
        if self.overlay:
            self.overlay_panel()
            return [(self.overlay, 0, 0, width, height)]
        if self.zoom:
            return [(self.order[self.focus], 0, 0, width, height)]
        if len(self.hidden) >= len(self.order):
            return []
        col_sizes = split_sizes(width, self.ncols, self.col_w)
        row_sizes = split_sizes(height, self.nrows, self.row_w)
        xs = [sum(col_sizes[:i]) for i in range(self.ncols + 1)]
        ys = [sum(row_sizes[:i]) for i in range(self.nrows + 1)]
        boxes = {reg.name: [reg.col0, reg.row0, reg.col1, reg.row1] for reg in self.regions}
        # a hidden panel gives its cells to a neighbour that shares a full edge (below/above first, then side)
        for name in [r.name for r in self.regions if r.name in self.hidden]:
            hb = boxes.pop(name)
            for other, ob in boxes.items():
                if other in self.hidden:
                    continue
                same_cols = ob[0] == hb[0] and ob[2] == hb[2]
                same_rows = ob[1] == hb[1] and ob[3] == hb[3]
                if same_cols and (ob[1] == hb[3] + 1 or ob[3] == hb[1] - 1):
                    ob[1], ob[3] = min(ob[1], hb[1]), max(ob[3], hb[3])
                    break
                if same_rows and (ob[0] == hb[2] + 1 or ob[2] == hb[0] - 1):
                    ob[0], ob[2] = min(ob[0], hb[0]), max(ob[2], hb[2])
                    break
        out = []
        for name, (c0, r0, c1, r1) in boxes.items():
            x, y = xs[c0], ys[r0]
            out.append((name, x, y, xs[c1 + 1] - x, ys[r1 + 1] - y))
        return out

    def render(self, width: int, height: int):
        if self.help:
            return self.render_help(width, height)
        cells = []
        grid_h = height - (1 if self.hints and height > 8 else 0)
        self.last_rects = self.rects(width, grid_h)
        for name, x, y, w, h in self.last_rects:
            if w < 4 or h < 3:
                continue
            p = self.panels[name]
            in_grid = name in self.order
            idx = self.order.index(name) if in_grid else -1
            focused = in_grid and idx == self.focus and len(self.visible_order()) > 1 and not self.overlay
            key = (name, w, h, focused, self.zoom, bool(self.overlay), p.version)
            if key in self.render_cache:
                cells.append((x, y, w, h, None, key))
                continue
            body = p.safe_render(w - 2, h - 2)
            if p._error and not isinstance(body, Text):
                body = Group(Text(f"sample error: {p._error}", style=self.theme.bad), body)
            cells.append((x, y, w, h, frame(p, body, w, h, self.theme, focused=focused,
                                            index=idx + 1 if 0 <= idx < 9 else None,
                                            zoomed=self.zoom or bool(self.overlay)), key))
        if not cells and self.order:
            cells.append((0, 0, width, 1, Text("all panels hidden — press g", style=self.theme.dim)))
        if grid_h < height:
            cells.append((0, grid_h, width, 1, self.render_hints(width)))
        comp = Composite(cells, width, height, self.render_cache)
        if self.paused or self.error:
            note = Text(" PAUSED " if self.paused else f" {self.error} ", style=f"bold reverse {self.theme.warn}")
            cells.append((max(0, width - len(note.plain) - 1), 0, len(note.plain), 1, note))
        return comp

    def render_hints(self, width: int) -> Text:
        """Bottom bar: global keys, then the focused panel's own keys."""
        th = self.theme
        t = Text(no_wrap=True)
        for i, (k, v) in enumerate(HINT_KEYS):
            if i:
                t.append(" ")
            t.append(f" {k} ", f"bold reverse {th.dim}")
            t.append(f" {v}", th.dim)
        if self.paused:
            t.append("   PAUSED", f"bold {th.warn}")
        if not self.mouse:
            t.append("   mouse off", th.dim)
        focused = self.focused_panel()
        if focused and focused.help:
            t.append("   │ ", th.border)
            t.append(f"{focused.title}: ", f"bold {th.focus}")
            for i, (k, v) in enumerate(focused.help.items()):
                if len(t.plain) + len(k) + len(v) + 4 > width:
                    break
                if i:
                    t.append("  ")
                t.append(k, f"bold {th.accent}")
                t.append(f" {v}", th.dim)
        t.truncate(width, overflow="ellipsis")
        return t

    def render_help(self, width: int, height: int):
        th = self.theme
        t = Table.grid(padding=(0, 2))
        t.add_column(style=th.accent, no_wrap=True)
        t.add_column()
        t.add_row(Text("dashboard", style=f"bold {th.title}"), "")
        for k, v in GLOBAL_KEYS.items():
            t.add_row(k, v)
        for name in self.order:
            p = self.panels[name]
            if p.help:
                t.add_row("", "")
                t.add_row(Text(p.title, style=f"bold {th.title}"), Text(f"({name})", style=th.dim))
                for k, v in p.help.items():
                    t.add_row(k, v)
        t.add_row("", "")
        t.add_row(Text("config", style=f"bold {th.title}"), Text(str(self.config_path), style=th.dim))
        t.add_row("layouts", ", ".join(f"[bold]{n}[/]" if n == self.layout_name else n for n in self.layout_names))
        return RichPanel(t, title="help — press any key", box=th.rich_box(), border_style=th.focus, width=width, height=height)

    # keys --------------------------------------------------------------------
    def on_mouse(self, kind: str, x: int, y: int) -> bool:
        """x/y are 0-based screen cells."""
        for name, rx, ry, rw, rh in self.last_rects:
            if rx <= x < rx + rw and ry <= y < ry + rh:
                if kind in ("CLICK", "RCLICK"):
                    if name in self.order:
                        self.focus = self.order.index(name)
                    if y == ry:                    # title bar → zoom toggle (closes an overlay)
                        if self.overlay:
                            self.toggle_overlay(self.overlay)
                        else:
                            self.zoom = not self.zoom
                        return True
                p = self.panels[name]
                if p.on_mouse(kind, x - rx - 1, y - ry - 1):
                    p.version += 1
                if p.wants_hide:
                    p.wants_hide = False
                    if name == self.overlay:
                        self.toggle_overlay(name)
                    elif name in self.order:
                        self.toggle_hidden(name)
                return True
        return False

    def on_key(self, key: str) -> bool:
        if self.help:
            self.help = False
            return True
        if ":" in key:
            kind, x, y = key.split(":")
            return self.on_mouse(kind, int(x) - 1, int(y) - 1)
        if key in OVERLAYS:
            self.toggle_overlay(OVERLAYS[key])
            return True
        if self.overlay:
            p = self.overlay_panel()
            if key in ("ESC", "q") or (key in ("ENTER", "f")):
                self.toggle_overlay(self.overlay)
                return True
            if p and p.on_key(key):
                p.version += 1
                return True
            return False
        focused = self.panels[self.order[self.focus]] if self.order else None
        # a panel in text-entry mode (proc filter) gets everything first
        if focused and (getattr(focused, "typing", False) or getattr(focused, "pending_kill", None)):
            if focused.on_key(key):
                focused.version += 1
                return True
            return False
        if key == "ENTER" and focused and focused.takes_enter and self.order[self.focus] not in self.hidden:
            if focused.on_key(key):
                focused.version += 1
                return True
        if key in ("TAB", "BTAB"):
            vis = self.visible_order() or self.order
            cur = self.order[self.focus]
            i = vis.index(cur) if cur in vis else -1
            self.focus = self.order.index(vis[(i + (1 if key == "TAB" else -1)) % len(vis)])
        elif key == "g":
            self.toggle_gifs()
        elif key.isdigit() and key != "0" and int(key) <= len(self.order):
            self.focus = int(key) - 1
        elif key in ("ENTER", "f"):
            self.zoom = not self.zoom
        elif key == "l":
            self.layout_idx = (self.layout_idx + 1) % len(self.layout_names)
            self.apply_layout()
        elif key == "L":
            self.layout_idx = (self.layout_idx - 1) % len(self.layout_names)
            self.apply_layout()
        elif key == " ":
            self.paused = not self.paused
        elif key == "+":
            self.refresh = max(0.2, self.refresh - 0.2)
        elif key == "-":
            self.refresh = min(10.0, self.refresh + 0.2)
        elif key == "r":
            try:
                self.load(self.layout_name)
                self.render_cache.clear()
                self.error = None
            except SystemExit as exc:
                self.error = str(exc)
        elif key == "?":
            self.help = True
        elif key == "m":
            self.mouse = not self.mouse
            self._keys.set_mouse(self.mouse)
        elif key == "ESC" and self.zoom:
            self.zoom = False
        elif focused:
            handled = focused.on_key(key)
            if handled:
                focused.version += 1
            if focused.wants_hide:
                focused.wants_hide = False
                self.toggle_hidden(self.order[self.focus])
                return True
            return handled
        else:
            return False
        return True

    # loop --------------------------------------------------------------------
    def run(self, console: Console) -> None:
        with Keys(mouse=self.mouse) as keys, Screen(console) as screen:
            self._keys = keys
            dirty = True
            size = console.size
            next_tick = 0.0
            while True:
                now = time.monotonic()
                if not self.paused and now >= next_tick:
                    names = list(self.order) + ([self.overlay] if self.overlay and self.overlay not in self.order else [])
                    for name in names:
                        p = self.panels[name]
                        if p.due(now):
                            v = p.version
                            p.tick(now)
                            if p.version != v:
                                dirty = True
                    next_tick = now + min(self.refresh, min((p.interval for p in self.panels.values()), default=1.0))
                if console.size != size:
                    size = console.size
                    dirty = True
                if dirty:
                    screen.draw(self.render(*size), *size)
                    dirty = False
                key = keys.poll(0.05)
                if key == "q" and not self.overlay and not (self.order and getattr(self.panels[self.order[self.focus]], "typing", False)):
                    return
                if key and self.on_key(key):
                    dirty = True

    def close(self) -> None:
        for p in self.panels.values():
            p.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="mondash", description="lego terminal dashboard built from mon panels")
    ap.add_argument("-l", "--layout", help="layout name from dash.toml")
    ap.add_argument("-c", "--config", type=Path, help=f"config file (default {CONFIG_FILE})")
    ap.add_argument("-w", "--window", action="store_true", help="open in a new terminal window")
    ap.add_argument("--no-mouse", action="store_true", help="leave the mouse to the terminal")
    ap.add_argument("--print-config", action="store_true", help="print the default config and exit")
    ap.add_argument("--panels", action="store_true", help="list panel types with their options and exit")
    a = ap.parse_args(argv)
    if a.print_config:
        from .core import DEFAULT_CONFIG
        print(DEFAULT_CONFIG)
        return
    if a.panels:
        from .core import panel_types
        for name, cls in sorted(panel_types().items()):
            print(f"{name:<7} {cls.title}")
            for k, v in cls.options.items():
                print(f"          {k:<12} {v}")
        return
    if a.window:
        relaunch_in_window([sys.argv[0]] + [x for x in sys.argv[1:] if x not in ("-w", "--window")], "mondash")
        return
    dash = Dashboard(a.config, a.layout)
    if a.no_mouse:
        dash.mouse = False
    console = Console()
    try:
        dash.run(console)
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    finally:
        dash.close()
