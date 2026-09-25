"""CPU: total load graph, per-core meters with frequency and temperature, load average, pressure."""
from __future__ import annotations

import math
import os
import time

import psutil
from rich.console import Group
from rich.text import Text

from ..core import Hist, Panel, bar, braille_graph, duration, kv_line, pad, read_file, read_psi, sparkline, temperatures
from ..host import CPU_CHIPS, cpu_model


class CpuPanel(Panel):
    name = "cpu"
    title = "CPU"
    interval = 1.0
    help = {"g": "toggle per-core temperature/frequency columns"}
    options = {"show_cores": "true/false — per-core rows (default true)", "graph_min": "minimum graph height (default 3)"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.n = psutil.cpu_count() or 1
        self.total = Hist()
        self.cores = [Hist(240) for _ in range(self.n)]
        self.pct = 0.0
        self.per = [0.0] * self.n
        self.freqs: list[float] = []
        self.freq = 0.0
        self.temps: dict[int, float] = {}
        self.pkg_temp: float | None = None
        self.load = (0.0, 0.0, 0.0)
        self.psi: str | None = None
        self.details = True
        self.core_ids = [self._core_id(i) for i in range(self.n)]
        self.model = cpu_model()
        psutil.cpu_percent(percpu=True)

    @staticmethod
    def _core_id(i: int) -> int:
        txt = read_file(f"/sys/devices/system/cpu/cpu{i}/topology/core_id")
        return int(txt) if txt and txt.isdigit() else i

    def sample(self, dt: float) -> None:
        self.per = psutil.cpu_percent(percpu=True)
        self.pct = sum(self.per) / len(self.per)
        self.total.push(self.pct)
        for h, v in zip(self.cores, self.per):
            h.push(v)
        try:
            fs = psutil.cpu_freq(percpu=True)
            self.freqs = [f.current for f in fs] if fs else []
            self.freq = sum(self.freqs) / len(self.freqs) if self.freqs else 0.0
        except Exception:
            self.freqs = []
        self.temps = {}
        self.pkg_temp = None
        for chip in CPU_CHIPS:
            for entry in temperatures().get(chip, []):
                label = entry.label or ""
                if label.startswith("Core"):
                    try:
                        self.temps[int(label.split()[1])] = entry.current
                    except (IndexError, ValueError):
                        pass
                elif label.startswith(("Package", "Tctl", "Tdie")) or not label:
                    self.pkg_temp = entry.current if self.pkg_temp is None else max(self.pkg_temp, entry.current)
        if self.pkg_temp is None and self.temps:
            self.pkg_temp = max(self.temps.values())
        self.load = os.getloadavg()
        self.psi = read_psi("cpu")

    def on_key(self, key: str) -> bool:
        if key == "g":
            self.details = not self.details
            return True
        return False

    def status(self) -> str:
        return self.model

    def render(self, width: int, height: int):
        th = self.theme
        rows: list = []
        # header: big number + bar + facts
        pct_txt = Text(f"{self.pct:5.1f}%", style=f"bold {th.level(self.pct)}")
        facts = [("", f"{self.freq / 1000:.2f}GHz" if self.freq else "", th.accent)]
        if self.pkg_temp is not None:
            facts.append(("", f"{self.pkg_temp:.0f}°C", th.temp_style(self.pkg_temp)))
        facts.append(("load", f"{self.load[0]:.2f} {self.load[1]:.2f} {self.load[2]:.2f}", None))
        if self.psi:
            facts.append(("psi", self.psi, None))
        facts.append(("up", duration(time.time() - psutil.boot_time()), None))
        facts_txt = kv_line([f for f in facts if f[1]], max(0, width - 8 - 22), th)
        bar_w = width - 7 - len(facts_txt.plain) - 2
        header = Text.assemble(pct_txt, " ")
        if bar_w >= 6:
            header.append_text(bar(self.pct / 100, bar_w, th))
            header.append(" ")
        header.append_text(facts_txt)
        rows.append(header)

        show_cores = bool(self.cfg.get("show_cores", True)) and height >= 3
        graph_min = int(self.cfg.get("graph_min", 3))
        core_rows = 0
        per_row = 1
        if show_cores:
            avail = max(1, height - 1 - (graph_min if height - 1 - self.n < graph_min else 0))
            per_row = max(1, math.ceil(self.n / avail))
            per_row = min(per_row, max(1, width // 14))
            core_rows = math.ceil(self.n / per_row)
        graph_h = height - 1 - core_rows
        if graph_h >= 2:
            rows.append(braille_graph(self.total.data, width, graph_h, 100, th))
        elif graph_h == 1:
            rows.append(sparkline(self.total.data, width, 100, th))
        if show_cores:
            cell_w = width // per_row
            detail = self.details and cell_w - 1 >= 26
            for r in range(core_rows):
                line = Text(no_wrap=True)
                for k in range(per_row):
                    i = r * per_row + k
                    if i >= self.n:
                        break
                    line.append_text(self._core_line(i, cell_w - (1 if k < per_row - 1 else 0), detail))
                    if k < per_row - 1:
                        line.append(" ")
                rows.append(line)
        return Group(*rows)

    def _core_line(self, i: int, width: int, detail: bool) -> Text:
        th = self.theme
        v = self.per[i] if i < len(self.per) else 0.0
        label = pad(f"c{i}", 3)
        extra = Text(no_wrap=True)
        if detail:
            if i < len(self.freqs):
                extra.append(f" {self.freqs[i] / 1000:4.1f}G", th.dim)
            t = self.temps.get(self.core_ids[i])
            if t is not None:
                extra.append(f" {t:3.0f}°", th.temp_style(t))
        val = f"{v:3.0f}%"
        bar_w = width - len(label) - 1 - len(val) - 1 - len(extra.plain)
        out = Text(label, th.dim)
        out.append(" ")
        if bar_w >= 3:
            out.append_text(bar(v / 100, bar_w, th))
            out.append(" ")
        else:
            out.append_text(sparkline(self.cores[i].data, max(1, bar_w + 1), 100, th))
        out.append(val, th.level(v))
        out.append_text(extra)
        return out
