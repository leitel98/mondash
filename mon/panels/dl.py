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
                    if d.path and d.url and not d.total:
                        d.total = self.probe.get(d.url)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            for pid in list(self.active):
                if pid not in seen and pid > 0:
                    d = self.active.pop(pid)
                    if d.path:
                        self.finished.appendleft((time.strftime("%H:%M:%S"), d.name, max(d.size, d.total)))

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
        active = sorted(self.tracker.active.values(), key=lambda d: d.name)
        if not active:
            rows.append(Text("nothing downloading — watching…", style=th.dim))
        for d in active:
            if len(rows) + 2 > height:
                rows.append(Text(f"… +{len(active) - len(rows) // 2} more", style=th.dim))
                break
            if d.path:
                glyph = "♨ " if d.tool == "steam" else "▶ "
                note = getattr(d, "note", "")
                rows.append(Text.assemble((glyph, "bold"), (fit(d.name, width - 14 - len(note)), "bold"), ("  " + d.tool, th.dim),
                                          ((" · " + note) if note else "", th.dim)))
                if d.total:
                    pct = min(100, d.size * 100 // d.total)
                    stats = Text.assemble((f"{pct:3d}%", "bold"), f" {human(d.size)}/{human(d.total)} ",
                                          (rate(d.rate), th.accent), " ", (f"ETA {self.dlw.eta(d.eta_seconds)}", th.warn))
                    if width >= 20 and height - len(rows) > 3:       # room for a bar line above the stats
                        rows.append(bar(d.size / d.total, width, th, palette=[th.good, th.good, th.accent]))
                    rows.append(stats)
                else:
                    rows.append(Text.assemble(("size unknown", th.dim), f"  {human(d.size)} so far  ", (rate(d.rate), th.accent)))
                if height - len(rows) > len(active) + 1:
                    rows.append(Text("  → " + fit(tilde(os.path.dirname(d.path)), width - 4, tail=True), style=th.dim))
            else:
                age = int(time.monotonic() - d.first_seen)
                rows.append(Text.assemble(("● ", "magenta"), (fit(d.label, width - 20), "magenta"), (f"  {d.tool} · {age}s", th.dim)))
        if self.tracker.finished and len(rows) < height - 1:
            rows.append(Text("finished:", style=th.dim))
            for when, name, size in list(self.tracker.finished)[: height - len(rows)]:
                rows.append(Text.assemble((f"{when} ", th.dim), ("✔ ", th.good), fit(name, width - 18), (f" {human(size)}", th.dim)))
        return Group(*rows)
