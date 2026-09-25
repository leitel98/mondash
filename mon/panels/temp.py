"""Temperatures & fans: every hwmon sensor with its high/critical marks, GPU temp, hottest-sensor history."""
from __future__ import annotations

import psutil
from rich.console import Group
from rich.text import Text

from . import _nvidia
from ..core import Hist, Panel, bar, braille_graph, fit, kv_line, pad, temperatures

NICE = {"coretemp": "cpu", "k10temp": "cpu", "zenpower": "cpu", "cpu_thermal": "cpu", "nvme": "nvme", "acpitz": "acpi",
        "amdgpu": "gpu", "radeon": "gpu", "nouveau": "gpu", "i915": "gpu", "xe": "gpu", "thinkpad": "fan", "dell_smm": "fan",
        "asus": "board", "nct6775": "board", "it87": "board", "drivetemp": "disk", "mt7921_phy0": "wifi", "spd5118": "ram"}


def nice_chip(chip: str) -> str:
    """Short label for a hwmon chip name: known ones from NICE, otherwise the name without its index/suffix."""
    if chip in NICE:
        return NICE[chip]
    base = chip.split("_")[0].split("-")[0]
    if base.startswith("pch"):
        return "pch"
    if base.startswith(("iwlwifi", "ath", "mt79", "rtw", "brcm")):
        return "wifi"
    if base.upper().startswith("BAT"):
        return "bat"
    return base[:5]


class TempPanel(Panel):
    name = "temp"
    title = "Temperatures"
    interval = 2.0
    help = {"c": "toggle per-core rows"}
    options = {"cores": "true/false — show individual CPU cores (default false)", "hide": "list of chip names to hide"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.readings: list[tuple[str, str, float, float | None, float | None]] = []
        self.fans: list[tuple[str, str, int]] = []
        self.hist: dict[str, Hist] = {}
        self.cores = bool(self.cfg.get("cores", False))
        self.hide = set(self.cfg.get("hide", []))

    def on_key(self, key: str) -> bool:
        if key == "c":
            self.cores = not self.cores
            return True
        return False

    def sample(self, dt: float) -> None:
        readings = []
        for chip, entries in temperatures().items():
            if chip in self.hide:
                continue
            for n, e in enumerate(entries):
                label = e.label or ("temp" if len(entries) == 1 else f"temp{n + 1}")
                if label.startswith("Core") and not self.cores:
                    continue
                if label.startswith("Package id"):
                    label = "package"
                readings.append((nice_chip(chip), label, e.current, e.high, e.critical))
        snap = _nvidia.poll(max_age=1.5)
        for g in snap["gpus"]:
            if g.get("temperature.gpu") is not None:
                readings.append(("gpu", (g.get("name") or "gpu").replace("NVIDIA GeForce ", ""), g["temperature.gpu"], 85.0, 95.0))
        self.readings = readings
        for chip, label, cur, _, _ in readings:
            self.hist.setdefault(f"{chip}/{label}", Hist()).push(cur)
        self.fans = []
        try:
            for chip, entries in psutil.sensors_fans().items():
                for e in entries:
                    self.fans.append((chip, e.label or chip, e.current))
        except Exception:
            pass

    def _style(self, cur: float, high: float | None, crit: float | None) -> str:
        th = self.theme
        crit = crit or 100.0
        high = high or crit * 0.85
        return th.bad if cur >= high else th.warn if cur >= high - 15 else th.good

    def render(self, width: int, height: int):
        th = self.theme
        rows: list = []
        if not self.readings:
            return Text("no temperature sensors", style=th.dim)
        hottest = max(self.readings, key=lambda r: r[2])
        n_lines = len(self.readings) + (1 if self.fans else 0)
        graph_h = height - n_lines
        if graph_h >= 2:
            key = f"{hottest[0]}/{hottest[1]}"
            head = kv_line([("hottest", f"{hottest[0]} {hottest[1]}", th.accent), ("", f"{hottest[2]:.0f}°C", self._style(*hottest[2:]))], width, th)
            rows.append(head)
            rows.append(braille_graph(self.hist[key].data, width, graph_h - 1, 100, th, palette=[th.good, th.good, th.warn, th.bad]))
        chip_w = 5
        label_w = min(14, max(6, width // 4))
        for chip, label, cur, high, crit in self.readings[:max(0, height - len(rows) - (1 if self.fans else 0))]:
            scale = crit or 100.0
            val = f"{cur:3.0f}°"
            line = Text.assemble((pad(chip, chip_w), th.dim), (pad(fit(label, label_w), label_w), ""), " ")
            bar_w = width - chip_w - label_w - 1 - len(val) - 1
            if bar_w >= 4:
                line.append_text(bar(cur / scale, bar_w, th, palette=[th.good, th.good, th.warn, th.bad]))
                line.append(" ")
            line.append(val, self._style(cur, high, crit))
            rows.append(line)
        if self.fans and len(rows) < height:
            rows.append(kv_line([(f"fan {label}", f"{rpm} rpm", th.accent) for _, label, rpm in self.fans], width, th))
        return Group(*rows)
