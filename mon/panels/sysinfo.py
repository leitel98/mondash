"""System strip: host, OS, kernel, uptime, load, tasks, pressure and the clock. Packs into 1–N lines."""
from __future__ import annotations

import os
import platform
import time

import psutil
from rich.console import Group
from rich.text import Text

from ..core import Panel, duration, human, read_psi
from ..host import cpu_temperature, os_name


class SysPanel(Panel):
    name = "sys"
    title = "System"
    interval = 1.0
    options = {"items": "ordered list from: host os kernel uptime load tasks temp disk bat psi clock date",
               "disk": "mount point for the disk item (default /var/home, /home or /)"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.host = platform.node()
        self.os = os_name()
        self.kernel = platform.release()
        self.items = self.cfg.get("items", ["host", "os", "kernel", "uptime", "load", "tasks", "temp", "disk", "bat", "psi", "clock"])
        self.load = (0.0, 0.0, 0.0)
        self.tasks = 0
        self.psi = ""
        self.temp: float | None = None
        self.disk: tuple[str, float, int] | None = None
        self.bat: tuple[int, bool] | None = None
        self.disk_mount = self.cfg.get("disk") or next((m for m in ("/var/home", "/home", "/") if os.path.ismount(m)), "/")

    def sample(self, dt: float) -> None:
        self.load = os.getloadavg()
        self.tasks = len(psutil.pids())
        parts = [f"{k} {v}" for k, v in (("cpu", read_psi("cpu")), ("mem", read_psi("memory")), ("io", read_psi("io"))) if v]
        self.psi = " ".join(parts)
        self.temp = cpu_temperature()
        try:
            u = psutil.disk_usage(self.disk_mount)
            self.disk = (self.disk_mount, u.percent, u.free)
        except OSError:
            self.disk = None
        b = psutil.sensors_battery()
        self.bat = (int(b.percent), bool(b.power_plugged)) if b else None

    DROP_ORDER = ["psi", "kernel", "os", "date", "tasks", "bat", "load", "uptime", "disk", "temp", "host", "clock"]

    def _chunks(self, items: list[str] | None = None) -> list[Text]:
        th = self.theme
        data = {
            "host": Text(self.host, style=f"bold {th.accent}"),
            "os": Text(self.os),
            "kernel": Text(self.kernel, style=th.dim),
            "uptime": Text.assemble(("up ", th.dim), duration(time.time() - psutil.boot_time())),
            "load": Text.assemble(("load ", th.dim), " ".join(f"{x:.2f}" for x in self.load)),
            "tasks": Text.assemble(("tasks ", th.dim), str(self.tasks)),
            "psi": Text.assemble(("psi ", th.dim), self.psi) if self.psi else None,
            "temp": Text.assemble(("cpu ", th.dim), (f"{self.temp:.0f}°C", th.temp_style(self.temp))) if self.temp is not None else None,
            "disk": Text.assemble((f"{self.disk[0]} ", th.dim), (f"{self.disk[1]:.0f}%", th.level(self.disk[1])), (f" · {human(self.disk[2])} free", th.dim)) if self.disk else None,
            "bat": Text.assemble(("bat ", th.dim), (f"{self.bat[0]}%", th.good if self.bat[0] > 30 else th.bad), (" ⚡" if self.bat[1] else "", th.good)) if self.bat else None,
            "clock": Text(time.strftime("%H:%M:%S"), style="bold"),
            "date": Text(time.strftime("%a %d %b"), style=th.dim),
        }
        return [data[k] for k in (items or self.items) if data.get(k) is not None]

    def render(self, width: int, height: int):
        chunks = self._chunks()
        sep = Text("  │  ", style=self.theme.border)
        rows: list[Text] = []
        cur = Text(no_wrap=True)
        for ch in chunks:
            extra = len(ch.plain) + (len(sep.plain) if cur.plain else 0)
            if cur.plain and len(cur.plain) + extra > width:
                rows.append(cur)
                cur = Text(no_wrap=True)
            if cur.plain:
                cur.append_text(sep)
            cur.append_text(ch)
        if cur.plain:
            rows.append(cur)
        if height == 1 and len(rows) > 1:
            # single line: short separators, then drop the least important items until it fits
            items = list(self.items)
            while True:
                one = Text(no_wrap=True)
                for i, ch in enumerate(self._chunks(items)):
                    if i:
                        one.append(" · ", self.theme.border)
                    one.append_text(ch)
                if len(one.plain) <= width or len(items) <= 1:
                    one.truncate(width, overflow="ellipsis")
                    return one
                for k in self.DROP_ORDER:
                    if k in items:
                        items.remove(k)
                        break
                else:
                    items.pop()
        return Group(*rows[:height])
