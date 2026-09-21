"""Memory: RAM breakdown meters, swap, zram compression, history graph."""
from __future__ import annotations

import psutil
from rich.console import Group
from rich.text import Text

from ..core import Hist, Panel, braille_graph, human, kv_line, meter, read_file, read_psi, sparkline


class MemPanel(Panel):
    name = "mem"
    title = "Memory"
    interval = 1.0
    options = {"graph": "true/false — history graph (default true)"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.hist = Hist()
        self.vm = psutil.virtual_memory()
        self.sw = psutil.swap_memory()
        self.zram: tuple[int, int] | None = None
        self.psi: str | None = None

    def sample(self, dt: float) -> None:
        self.vm = psutil.virtual_memory()
        self.sw = psutil.swap_memory()
        self.hist.push(self.vm.percent)
        self.psi = read_psi("memory")
        self.zram = None
        stat = read_file("/sys/block/zram0/mm_stat")
        if stat:
            parts = stat.split()
            if len(parts) >= 2:
                self.zram = (int(parts[0]), int(parts[1]))   # original bytes, compressed bytes

    def render(self, width: int, height: int):
        th, vm, sw = self.theme, self.vm, self.sw
        used = vm.total - vm.available
        rows: list = []
        head = Text.assemble((f"{vm.percent:5.1f}%", f"bold {th.level(vm.percent)}"), " ")
        head.append_text(kv_line([("used", human(used), th.accent), ("of", human(vm.total), None),
                                  ("psi", self.psi or "", None) if self.psi else ("", "", None)],
                                 width - 7, th))
        rows.append(head)
        items = [("used", used), ("avail", vm.available), ("cached", getattr(vm, "cached", 0)),
                 ("buffers", getattr(vm, "buffers", 0)), ("shared", getattr(vm, "shared", 0)), ("free", vm.free)]
        want = 1 + len(items) + 1 + (1 if self.zram and self.zram[0] >= 1 << 20 else 0)
        graph_h = height - want if bool(self.cfg.get("graph", True)) else 0
        if graph_h < 2 and height >= 6:
            # shrink the breakdown to make room for a graph
            items = items[:max(2, height - 4 - (1 if self.zram else 0))]
            graph_h = height - (1 + len(items) + 1 + (1 if self.zram else 0))
        if graph_h >= 2:
            rows.append(braille_graph(self.hist.data, width, graph_h, 100, th))
        elif graph_h == 1:
            rows.append(sparkline(self.hist.data, width, 100, th))
        for label, val in items[:max(0, height - len(rows) - 1)]:
            rows.append(meter(label, val / vm.total if vm.total else 0, human(val), width, th, label_w=7, value_w=6))
        if len(rows) < height:
            if sw.total:
                rows.append(meter("swap", sw.used / sw.total, f"{human(sw.used)}/{human(sw.total)}", width, th, label_w=7, value_w=13))
            else:
                rows.append(Text("swap    none", style=th.dim))
        if self.zram and self.zram[0] >= 1 << 20 and len(rows) < height:
            orig, comp = self.zram
            ratio = orig / comp if comp else 0
            rows.append(kv_line([("zram", f"{human(orig)} → {human(comp)}", th.accent),
                                 ("ratio", f"{ratio:.1f}x" if ratio else "-", None)], width, th))
        return Group(*rows)
