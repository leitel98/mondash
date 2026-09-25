"""Animation: browse a folder of GIFs (plus built-in effects), play one, with back / pause / hide buttons.

GIFs are decoded with Pillow and drawn as half-block true-colour cells. Click a list entry to play it,
click the picture to pause, use the buttons at the bottom or the keys: a pause, b back, x hide.
"""
from __future__ import annotations

import glob
import math
import os
import random

from rich.color import Color, ColorType
from rich.color_triplet import ColorTriplet
from rich.console import Group
from rich.style import Style
from rich.text import Text

from ..core import BUNDLED_GIFS, USER_GIFS, Panel, fit, tilde

try:
    from PIL import Image, ImageSequence
except Exception:                  # Pillow missing: built-in effects still work
    Image = None

BUILTINS = ["plasma", "rain", "donut", "wave"]
_PLASMA_STYLES: dict = {}
DEFAULT_FOLDER = USER_GIFS                      # the user's own gifs; the ones shipped with mon are listed after them


class GifPanel(Panel):
    name = "gif"
    title = "Animation"
    interval = 0.1
    takes_enter = True
    help = {"↑ ↓ ⏎": "choose / play", "a": "pause", "b": "back to list", "x": "hide panel (g shows)", "click": "select · pause · buttons"}
    options = {"folder": f"folder with .gif files (default {DEFAULT_FOLDER}; the bundled gifs are always listed too)",
               "file": "gif (or built-in effect name) to start playing; default: show the list",
               "fps": "frame cap (default 8; GIF frame delays are respected below this)",
               "hidden": "true/false — start hidden, g shows it"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.folder = os.path.expanduser(str(self.cfg.get("folder", "") or DEFAULT_FOLDER))
        self.fps = max(1.0, min(30.0, float(self.cfg.get("fps", 8))))
        self.play_interval = 1.0 / self.fps
        self.mode = "browse"
        self.items: list[tuple[str, str]] = []        # (label, path-or-effect)
        self.sel = 0
        self.top = 0
        self.playing = True
        self.clock = 0.0
        self.current: str | None = None                 # path or effect name
        self.frames: list = []
        self.durations: list[float] = []
        self.err = ""
        self.cache: dict = {}
        self.rain_cols: dict[int, list] = {}
        self._buttons: list[tuple[int, int, str]] = []
        self._styles: dict = {}          # (top rgb, bottom rgb) -> Style, shared across frames
        self._list_rows = 0
        self.rescan()
        start = str(self.cfg.get("file", "") or "")
        if start:
            self.play(os.path.expanduser(start))
        self._set_interval()

    # ------------------------------------------------------------------ browsing / loading
    @staticmethod
    def _gifs(folder: str) -> list[str]:
        return sorted(glob.glob(os.path.join(folder, "*.gif")) + glob.glob(os.path.join(folder, "*.GIF")),
                      key=lambda p: os.path.basename(p).lower())

    def rescan(self) -> None:
        try:
            os.makedirs(self.folder, exist_ok=True)
        except OSError:
            pass
        own = self._gifs(self.folder)
        bundled = [] if os.path.realpath(self.folder) == os.path.realpath(str(BUNDLED_GIFS)) else self._gifs(str(BUNDLED_GIFS))
        names = {os.path.basename(p) for p in own}
        self.items = ([(os.path.basename(p), p) for p in own]
                      + [(f"· {os.path.basename(p)}", p) for p in bundled if os.path.basename(p) not in names]
                      + [(f"✦ {e}", e) for e in BUILTINS])
        self.sel = max(0, min(self.sel, len(self.items) - 1))

    def _set_interval(self) -> None:
        self.interval = self.play_interval if self.mode == "play" else 1.0

    def play(self, target: str) -> None:
        self.frames, self.durations, self.cache, self.err = [], [], {}, ""
        self.current = target
        self.clock = 0.0
        self.playing = True
        if target not in BUILTINS:
            if Image is None:
                self.err = "Pillow not installed (pip install --user pillow)"
            else:
                try:
                    im = Image.open(target)
                    for frame in ImageSequence.Iterator(im):
                        self.frames.append(frame.convert("RGB"))
                        self.durations.append(max(0.02, (frame.info.get("duration") or 100) / 1000))
                        if len(self.frames) >= 400:
                            break
                except Exception as exc:
                    self.err = f"cannot load: {exc}"
                if not self.frames and not self.err:
                    self.err = "no frames in file"
        self.mode = "play"
        self._set_interval()
        self.title = self.cfg.get("title") or (os.path.basename(target) if target not in BUILTINS else target)
        self._last = 0.0

    def back(self) -> None:
        self.mode = "browse"
        self.title = self.cfg.get("title") or "Animation"
        self.rescan()
        self._set_interval()

    # ------------------------------------------------------------------ input
    def on_key(self, key: str) -> bool:
        if key == "x":
            self.wants_hide = True
            return True
        if self.mode == "browse":
            if key == "UP":
                self.sel = max(0, self.sel - 1)
            elif key == "DOWN":
                self.sel = min(len(self.items) - 1, self.sel + 1)
            elif key in ("ENTER", "a", "p") and self.items:
                self.play(self.items[self.sel][1])
            elif key == "R":
                self.rescan()
            else:
                return False
            return True
        if key == "a":
            self.playing = not self.playing
        elif key in ("b", "ESC", "BACKSPACE"):
            self.back()
        elif key == "n" and self.current in BUILTINS:
            self.play(BUILTINS[(BUILTINS.index(self.current) + 1) % len(BUILTINS)])
        elif key == "R" and self.current:
            self.play(self.current)
        else:
            return False
        return True

    def on_mouse(self, kind: str, x: int, y: int) -> bool:
        if kind in ("WHEELUP", "WHEELDOWN") and self.mode == "browse":
            self.sel = max(0, min(len(self.items) - 1, self.sel + (-1 if kind == "WHEELUP" else 1)))
            return True
        if kind != "CLICK":
            return False
        for x0, x1, action in self._buttons:
            if self._button_row == y and x0 <= x < x1:
                actions = {"back": self.back, "pause": lambda: setattr(self, "playing", not self.playing),
                           "play": lambda: self.play(self.items[self.sel][1]) if self.items else None,
                           "hide": lambda: setattr(self, "wants_hide", True), "rescan": self.rescan}
                fn = actions.get(action)
                if fn:
                    fn()
                return True
        if self.mode == "browse":
            n = self.top + y - self._list_top
            if 0 <= y - self._list_top < self._list_rows and 0 <= n < len(self.items):
                if n == self.sel:
                    self.play(self.items[n][1])
                else:
                    self.sel = n
                return True
            return False
        self.playing = not self.playing
        return True

    # ------------------------------------------------------------------ sampling
    def sample(self, dt: float):
        if self.mode != "play" or not self.playing:
            return False
        self.clock += dt
        return True

    def status(self) -> str:
        if self.mode == "browse":
            return f"{len(self.items)} items"
        return "paused" if not self.playing else ""

    # ------------------------------------------------------------------ rendering
    def _bar(self, width: int, buttons: list[tuple[str, str]]) -> Text:
        """Clickable button bar; remembers x ranges for on_mouse."""
        th = self.theme
        t = Text(no_wrap=True)
        self._buttons = []
        for i, (label, action) in enumerate(buttons):
            if len(t.plain) + len(label) + 3 > width:
                break
            if i:
                t.append(" ")
            x0 = len(t.plain)
            t.append(f" {label} ", f"bold reverse {th.accent}" if action != "hide" else f"bold reverse {th.dim}")
            self._buttons.append((x0, len(t.plain), action))
        return t

    def render(self, width: int, height: int):
        self._button_row = height - 1
        if self.mode == "browse":
            return self._render_browse(width, height)
        body_h = max(1, height - 1)
        if self.err and not self.frames:
            body = Text(self.err, style=self.theme.bad)
        elif self.frames:
            body = self._render_gif(width, body_h)
        else:
            fn = {"plasma": self._plasma, "rain": self._rain, "donut": self._donut, "wave": self._wave}.get(self.current, self._plasma)
            body = fn(width, body_h)
        bar = self._bar(width, [("‹ back", "back"), ("▶ play" if not self.playing else "⏸ pause", "pause"), ("✕ hide", "hide")])
        return Group(body, bar) if height > 1 else body

    def _render_browse(self, width: int, height: int) -> Group:
        th = self.theme
        rows: list[Text] = []
        rows.append(Text.assemble(("gifs in ", th.dim), (fit(tilde(self.folder), width - 9, tail=True), th.accent)))
        self._list_top = 1
        self._list_rows = max(1, height - 2)
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + self._list_rows:
            self.top = self.sel - self._list_rows + 1
        if not self.items:
            rows.append(Text("drop .gif files in that folder", style=th.dim))
        for n in range(self.top, min(len(self.items), self.top + self._list_rows)):
            label, target = self.items[n]
            selected = n == self.sel
            line = Text(no_wrap=True, style="reverse" if selected else "")
            line.append("▶ " if selected else "  ", th.accent if not selected else "")
            line.append(fit(label, width - 4), "" if target in BUILTINS or selected else "")
            rows.append(line)
        while len(rows) < height - 1:
            rows.append(Text(""))
        rows.append(self._bar(width, [("⏎ play", "pause"), ("↻ rescan", "rescan"), ("✕ hide", "hide")]))
        # in browse mode the "pause" button plays the selection
        self._buttons = [(a, b, "play" if act == "pause" else act) for a, b, act in self._buttons]
        return Group(*rows[:height])

    def _frame_index(self) -> int:
        total = sum(self.durations)
        if total <= 0:
            return 0
        t = self.clock % total
        acc = 0.0
        for i, d in enumerate(self.durations):
            acc += d
            if t < acc:
                return i
        return len(self.frames) - 1

    def _render_gif(self, width: int, height: int) -> Text:
        idx = self._frame_index()
        key = (idx, width, height)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        im = self.frames[idx]
        px_w, px_h = width, height * 2
        scale = min(px_w / im.width, px_h / im.height)
        w = max(1, int(im.width * scale))
        h = max(2, int(im.height * scale) // 2 * 2)
        small = im.resize((w, h), Image.BILINEAR)
        px = small.load()
        left = (width - w) // 2
        top = (height - h // 2) // 2
        styles: dict = self._styles
        out = Text(no_wrap=True)
        for row in range(height):
            if row:
                out.append("\n")
            y = (row - top) * 2
            if y < 0 or y >= h:
                continue
            out.append(" " * left)
            run, run_key, run_style = 0, None, None
            for x in range(w):
                key2 = (px[x, y], px[x, y + 1])
                if key2 != run_key:
                    if run:
                        out.append("▀" * run, run_style)
                        run = 0
                    run_key = key2
                    run_style = styles.get(key2)
                    if run_style is None:
                        run_style = styles[key2] = Style(color=_rgb(*key2[0]), bgcolor=_rgb(*key2[1]))
                run += 1
            if run:
                out.append("▀" * run, run_style)
        if len(self.cache) > 600:
            self.cache.clear()
        self.cache[key] = out
        return out

    # ------------------------------------------------------------------ built-in effects
    def _plasma(self, width: int, height: int) -> Text:
        t = self.clock
        out = Text(no_wrap=True)
        for row in range(height):
            if row:
                out.append("\n")
            run, run_style = 0, None
            y = row * 2
            for x in range(width):
                v = (math.sin(x * 0.12 + t) + math.sin(y * 0.2 - t * 0.7) + math.sin((x + y) * 0.08 + t * 0.5)
                     + math.sin(math.hypot(x - width / 2, (y - height) * 0.5) * 0.15 - t)) / 4
                hue = round((v + 1) / 2 * 40) / 40
                style = _PLASMA_STYLES.get(hue)
                if style is None:
                    style = _PLASMA_STYLES[hue] = Style(color=Color.from_rgb(*_hsv(hue, 0.85, 0.9)))
                if style is not run_style and run:
                    out.append("█" * run, run_style)
                    run = 0
                run_style = style
                run += 1
            out.append("█" * run, run_style)
        return out

    def _rain(self, width: int, height: int) -> Text:
        glyphs = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉ0123456789"
        grid = [[None] * width for _ in range(height)]
        for x in range(width):
            col = self.rain_cols.get(x)
            if col is None or col[0] > height + col[1]:
                if random.random() < 0.08:
                    self.rain_cols[x] = [random.randint(-height, 0), random.randint(4, max(5, height)), random.uniform(0.6, 1.4)]
                continue
            head, length, speed = col
            col[0] += speed
            for i in range(length):
                y = int(head) - i
                if 0 <= y < height:
                    grid[y][x] = (glyphs[(x * 7 + y * 3 + int(self.clock * 6)) % len(glyphs)], i, length)
        out = Text(no_wrap=True)
        for row in range(height):
            if row:
                out.append("\n")
            for x in range(width):
                cell = grid[row][x]
                if cell is None:
                    out.append(" ")
                else:
                    ch, i, length = cell
                    if i == 0:
                        out.append(ch, "bold white")
                    else:
                        level = max(40, int(255 * (1 - i / length)))
                        out.append(ch, Style(color=Color.from_rgb(0, level, int(level * 0.4))))
        return out

    def _donut(self, width: int, height: int) -> Text:
        a, b = self.clock * 1.0, self.clock * 0.5
        chars = ".,-~:;=!*#$@"
        zbuf = [0.0] * (width * height)
        buf = [" "] * (width * height)
        r1, r2, k2 = 1.0, 2.0, 5.0
        k1 = min(width, height * 2) * k2 * 3 / (8 * (r1 + r2))
        ca, sa, cb, sb = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
        theta = 0.0
        while theta < 6.28:
            ct, st = math.cos(theta), math.sin(theta)
            phi = 0.0
            while phi < 6.28:
                cp, sp = math.cos(phi), math.sin(phi)
                cx, cy = r2 + r1 * ct, r1 * st
                x = cx * (cb * cp + sa * sb * sp) - cy * ca * sb
                y = cx * (sb * cp - sa * cb * sp) + cy * ca * cb
                z = k2 + ca * cx * sp + cy * sa
                ooz = 1 / z
                xp = int(width / 2 + k1 * ooz * x)
                yp = int(height / 2 - k1 * ooz * y / 2)
                lum = cp * ct * sb - ca * ct * sp - sa * st + cb * (ca * st - ct * sa * sp)
                if 0 <= xp < width and 0 <= yp < height:
                    idx = xp + yp * width
                    if ooz > zbuf[idx]:
                        zbuf[idx] = ooz
                        buf[idx] = chars[max(0, min(11, int(lum * 8)))]
                phi += 0.07
            theta += 0.2
        out = Text(no_wrap=True)
        for row in range(height):
            if row:
                out.append("\n")
            out.append("".join(buf[row * width:(row + 1) * width]), self.theme.accent)
        return out

    def _wave(self, width: int, height: int) -> Text:
        from ..core import braille_graph
        vals = [1 + math.sin(x * 0.15 + self.clock * 2) * math.cos(x * 0.05 - self.clock) for x in range(width * 2)]
        return braille_graph(vals, width, height, 2.0, self.theme)


def _rgb(r: int, g: int, b: int) -> Color:
    """Truecolor Color without going through rich's hex parser (10x cheaper for thousands of cells)."""
    return Color(f"#{r:02x}{g:02x}{b:02x}", ColorType.TRUECOLOR, triplet=ColorTriplet(r, g, b))


def _hsv(h: float, s: float, v: float) -> tuple[int, int, int]:
    i = int(h * 6) % 6
    f = h * 6 - int(h * 6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r, g, b = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]
    return int(r * 255), int(g * 255), int(b * 255)
