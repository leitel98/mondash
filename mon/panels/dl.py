"""Downloads: wraps the existing dlwatch tracker as a panel (curl, wget, aria2c, flatpak, rsync, git, scp, pip)."""
from __future__ import annotations

import importlib.util
import os
import sys
import time
from importlib.machinery import SourceFileLoader

from rich.console import Group
from rich.text import Text

from ..core import SCRIPTS_DIR, Panel, bar, fit, human, kv_line, rate, tilde
from ..procscan import SCANNER


def _load_dlwatch():
    path = SCRIPTS_DIR / "dlwatch"
    if not path.exists():
        raise FileNotFoundError(f"dlwatch not found at {path}")
    loader = SourceFileLoader("dlwatch", str(path))
    spec = importlib.util.spec_from_loader("dlwatch", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dlwatch"] = mod
    loader.exec_module(mod)
    return mod


def make_fast_tracker(dlw):
    """Subclass dlwatch.Tracker whose scan() finds downloader processes via the shared /proc snapshot."""
    import psutil

    comm_map = {name[:15]: name for name in dlw.DOWNLOADERS}       # /proc comm is truncated to 15 chars

    class FastTracker(dlw.Tracker):
        def scan_processes(self, dt: float) -> None:
            snap = SCANNER.snapshot(0.5)
            seen: set[int] = set()
            for row in snap.rows:
                tool = comm_map.get(row["name"])
                if not tool or row["pid"] == self.me or row["kernel"] or row["st"] == "Z":
                    continue
                pid = row["pid"]
                try:
                    proc = psutil.Process(pid)
                    d = self.active.get(pid)
                    if d is None:
                        args = proc.cmdline()[1:]
                        if not args and tool not in ("flatpak",):
                            continue
                        url_m = dlw.URL_RE.search(" ".join(args))
                        url = url_m.group(0) if url_m else None
                        path = None
                        if tool in dlw.FILE_TOOLS:
                            try:
                                cwd = proc.cwd()
                            except (psutil.AccessDenied, psutil.NoSuchProcess):
                                cwd = None
                            path = dlw.output_from_args(args, tool, cwd)
                        d = dlw.Download(pid, tool, dlw.describe(tool, args, url), url, path, time.monotonic())
                        self.active[pid] = d
                    seen.add(pid)
                    if d.fd is None and d.tool in dlw.FILE_TOOLS and (
                            d.path is None or (not os.path.isfile(d.path) and time.monotonic() - d.first_seen > 3)):
                        found = dlw.output_from_proc(proc)
                        if found:
                            d.path, d.fd = found
                    d.update_size(dt)
                    self.apply_total(d)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            for pid in list(self.active):
                if pid not in seen and pid > 0:
                    d = self.active.pop(pid)
                    if d.path:
                        self.finished.appendleft((time.strftime("%H:%M:%S"), d.display_name, max(d.size, d.total)))

    return FastTracker


class DlPanel(Panel):
    name = "dl"
    title = "Downloads"
    interval = 1.0
    options = {}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        try:
            self.dlw = _load_dlwatch()
            self.tracker = make_fast_tracker(self.dlw)()
            self.net = self.dlw.NetMeter()
            self.err = None
        except Exception as exc:
            self.dlw = None
            self.err = repr(exc)

    def sample(self, dt: float) -> None:
        if not self.dlw:
            return
        self.net.tick(dt)
        self.tracker.scan(dt)

    def status(self) -> str:
        return f"↓ {rate(self.net.rate)} {self.net.iface}" if self.dlw and self.net.iface else ""

    def render(self, width: int, height: int):
        th = self.theme
        if not self.dlw:
            return Text(f"dlwatch unavailable: {self.err}", style=th.bad)
        rows: list = []
        # known-size downloads first (they have bars), then unknown size, then process-only entries; fastest first
        active = sorted(self.tracker.active.values(),
                        key=lambda d: (0 if d.total else 1 if d.path else 2, -d.rate, d.display_name.lower()))
        if not active:
            rows.append(Text("nothing downloading — watching…", style=th.dim))
        n = len(active)
        fin_rows = min(len(self.tracker.finished), 3) + 1 if self.tracker.finished else 0
        avail = max(1, height - (fin_rows if n * 1 + fin_rows <= height else 0))
        per = 3 if n * 3 <= avail else 2 if n * 2 <= avail else 1
        shown = active if per > 1 or n <= avail else active[: max(0, avail - 1)]
        stats_w = max((len(self._stats(d).plain) for d in shown if d.path), default=0)
        for d in shown:
            rows.extend(self._entry(d, width, per, stats_w))
        if len(shown) < n:
            rows.append(Text(f"… +{n - len(shown)} more", style=th.dim))
        if self.tracker.finished and len(rows) < height - 1:
            rows.append(Text("finished:", style=th.dim))
            for when, name, size in list(self.tracker.finished)[: height - len(rows)]:
                rows.append(Text.assemble((f"{when} ", th.dim), ("✔ ", th.good), fit(name, width - 18), (f" {human(size)}", th.dim)))
        return Group(*rows[:height])

    def _stats(self, d) -> Text:
        th = self.theme
        stats = Text(no_wrap=True)
        if d.total:
            stats.append(f"{d.pct:3d}%", "bold")
            stats.append(f" {human(d.size)}/{human(d.total)} ")
            stats.append(rate(d.rate), th.accent)
            stats.append(" ")
            stats.append(f"ETA {self.dlw.eta(d.eta_seconds)}", th.warn)
        else:
            stats.append("size unknown ", th.dim)
            stats.append(f"{human(d.size)} so far ")
            stats.append(rate(d.rate), th.accent)
        return stats

    def _entry(self, d, width: int, per: int, stats_w: int = 0) -> list:
        """1, 2 or 3 lines for one download depending on how crowded the panel is."""
        th = self.theme
        glyph = "♨ " if d.tool == "steam" else "▶ " if d.path else "● "
        note = getattr(d, "note", "")
        name_style = "bold" if d.path else "magenta"
        if not d.path:                                                   # process only (flatpak, git, pip…)
            age = int(time.monotonic() - d.first_seen)
            line = Text.assemble((glyph, "magenta"), (fit(d.label, width - 20), "magenta"), (f"  {d.tool} · {age}s", th.dim))
            return [line] + ([Text("")] if per == 3 else [])
        stats = self._stats(d)
        if per == 1:
            bar_w = max(0, min(20, width // 4))
            name_w = max(8, width - bar_w - max(stats_w, len(stats.plain)) - 4)
            line = Text.assemble((glyph, name_style), (fit(d.display_name, name_w).ljust(name_w), name_style), " ")
            if bar_w >= 4:
                if d.total:
                    line.append_text(bar(d.size / d.total, bar_w, th, palette=[th.good, th.good, th.accent]))
                else:
                    line.append(th.bar_empty * bar_w, th.bar_empty_style)
                line.append(" ")
            line.append_text(stats)
            return [line]
        head = Text.assemble((glyph, name_style), (fit(d.display_name, width - 6 - len(d.tool) - (len(note) + 3 if note else 0)), name_style),
                             ("  " + d.tool, th.dim), ((" · " + note) if note else "", th.dim))
        if per == 2:
            bar_w = max(0, width - len(stats.plain) - 1)
            line = Text(no_wrap=True)
            if d.total and bar_w >= 6:
                line.append_text(bar(d.size / d.total, bar_w, th, palette=[th.good, th.good, th.accent]))
                line.append(" ")
            line.append_text(stats)
            return [head, line]
        out = [head]
        if d.total:
            out.append(bar(d.size / d.total, width, th, palette=[th.good, th.good, th.accent]))
        else:
            out.append(Text(th.bar_empty * width, style=th.bar_empty_style))
        tail = Text(no_wrap=True)
        tail.append_text(stats)
        dest = "  → " + tilde(os.path.dirname(d.path))
        if len(tail.plain) + len(dest) <= width:
            tail.append(dest, th.dim)
        out.append(tail)
        return out
