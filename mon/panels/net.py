"""Network: per-interface download/upload graphs, rates, totals, link info."""
from __future__ import annotations

import socket

import psutil
from rich.console import Group
from rich.text import Text

from ..core import Hist, Panel, braille_graph, human, kv_line, nice_max, rate, read_file, sparkline


def default_iface() -> str | None:
    try:
        for line in open("/proc/net/route").read().splitlines()[1:]:
            parts = line.split()
            if parts[1] == "00000000":
                return parts[0]
    except OSError:
        pass
    return None


class NetPanel(Panel):
    name = "net"
    title = "Network"
    interval = 1.0
    help = {"n / p": "next / previous interface", "a": "toggle auto-scale vs fixed scale"}
    options = {"iface": "interface to show (default: the one with the default route)",
               "scale": "fixed graph scale in bytes/s, e.g. 10485760 (default auto)"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.ifaces = [i for i in psutil.net_io_counters(pernic=True) if i != "lo"] or ["lo"]
        want = self.cfg.get("iface") or default_iface()
        self.idx = self.ifaces.index(want) if want in self.ifaces else 0
        self.rx, self.tx = Hist(), Hist()
        self.prev = None
        self.rx_rate = self.tx_rate = 0.0
        self.totals = (0, 0)
        self.auto = "scale" not in self.cfg
        self.fixed = float(self.cfg.get("scale", 0) or 0)

    @property
    def iface(self) -> str:
        return self.ifaces[self.idx]

    def _switch(self, step: int) -> None:
        self.ifaces = [i for i in psutil.net_io_counters(pernic=True) if i != "lo"] or ["lo"]
        self.idx = (self.idx + step) % len(self.ifaces)
        self.rx, self.tx = Hist(), Hist()
        self.prev = None

    def on_key(self, key: str) -> bool:
        if key == "n":
            self._switch(1)
        elif key == "p":
            self._switch(-1)
        elif key == "a":
            self.auto = not self.auto
        else:
            return False
        return True

    def sample(self, dt: float) -> None:
        counters = psutil.net_io_counters(pernic=True)
        c = counters.get(self.iface)
        if c is None:
            return
        cur = (c.bytes_recv, c.bytes_sent)
        if self.prev is not None:
            self.rx_rate = max(0.0, (cur[0] - self.prev[0]) / dt)
            self.tx_rate = max(0.0, (cur[1] - self.prev[1]) / dt)
            self.rx.push(self.rx_rate)
            self.tx.push(self.tx_rate)
        self.prev = cur
        self.totals = cur

    def status(self) -> str:
        return f"{self.idx + 1}/{len(self.ifaces)}"

    def _link(self) -> list[tuple[str, str, str | None]]:
        th = self.theme
        out: list[tuple[str, str, str | None]] = []
        st = psutil.net_if_stats().get(self.iface)
        if st:
            out.append(("", "up" if st.isup else "down", th.good if st.isup else th.bad))
            if st.speed:
                out.append(("", f"{st.speed}Mb", None))
        for a in psutil.net_if_addrs().get(self.iface, []):
            if a.family == socket.AF_INET:
                out.append(("", a.address, th.accent))
                break
        wifi = read_file(f"/sys/class/net/{self.iface}/wireless") is not None or (read_file(f"/sys/class/net/{self.iface}/uevent") or "").find("wlan") >= 0
        if wifi:
            out.append(("", "wifi", None))
        return out

    def render(self, width: int, height: int):
        th = self.theme
        rows: list = []
        head = Text.assemble((self.iface, f"bold {th.accent}"), "  ")
        head.append_text(kv_line([("↓", rate(self.rx_rate), th.good), ("↑", rate(self.tx_rate), th.warn)] + self._link(),
                                 width - len(self.iface) - 2, th))
        rows.append(head)
        window = max(2, 2 * width)
        if self.auto:
            scale = nice_max(max(self.rx.peak(window), self.tx.peak(window)), 10 * 1024)
        else:
            scale = self.fixed or nice_max(max(self.rx.peak(window), self.tx.peak(window)), 10 * 1024)
        footer_ok = height >= 5
        graph_h = height - 1 - (1 if footer_ok else 0)
        if graph_h >= 2:
            top = graph_h // 2 + graph_h % 2
            bottom = graph_h - top
            rows.append(braille_graph(self.rx.data, width, top, scale, th, palette=[th.good, th.good, th.accent]))
            if bottom >= 1:
                rows.append(braille_graph(self.tx.data, width, bottom, scale, th, palette=[th.warn, th.warn, th.bad], invert=True))
        elif graph_h == 1:
            rows.append(sparkline(self.rx.data, width, scale, th))
        if footer_ok:
            rows.append(kv_line([("total ↓", human(self.totals[0]), None), ("↑", human(self.totals[1]), None),
                                 ("peak", rate(max(self.rx.peak(), self.tx.peak())), None),
                                 ("scale", rate(scale) + ("" if self.auto else " fixed"), th.dim)], width, th))
        return Group(*rows)
