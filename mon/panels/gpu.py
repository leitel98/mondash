"""GPU: NVIDIA via nvidia-smi (util, VRAM, temp, power, clocks, per-process VRAM) and Intel via i915 sysfs."""
from __future__ import annotations

import glob
import os
import subprocess
import time

from rich.console import Group
from rich.text import Text

from . import _nvidia
from ..core import Hist, Panel, braille_graph, fit, kv_line, meter, pad, read_file, read_int, sparkline


class IntelGpu:
    """Busy % from rc6 (idle) residency, plus actual/max frequency, from /sys/class/drm."""

    def __init__(self, card: str) -> None:
        self.card = card
        self.name = "Intel iGPU"
        self.prev: tuple[float, int] | None = None
        self.busy = 0.0
        self.freq = self.max_freq = 0
        gts = glob.glob(f"{card}/gt/gt*/rc6_residency_ms")
        self.rc6_path = gts[0] if gts else None

    @staticmethod
    def find() -> "IntelGpu | None":
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
            if read_file(f"{card}/device/vendor") == "0x8086" and read_file(f"{card}/gt_act_freq_mhz") is not None:
                return IntelGpu(card)
        return None

    def sample(self) -> None:
        self.freq = read_int(f"{self.card}/gt_act_freq_mhz", 0) or 0
        self.max_freq = read_int(f"{self.card}/gt_RP0_freq_mhz", 0) or read_int(f"{self.card}/gt_max_freq_mhz", 0) or 0
        if self.rc6_path:
            now = time.monotonic()
            rc6 = read_int(self.rc6_path, 0) or 0
            if self.prev:
                dt_ms = (now - self.prev[0]) * 1000
                idle = (rc6 - self.prev[1]) / dt_ms if dt_ms > 0 else 1.0
                self.busy = max(0.0, min(100.0, (1.0 - idle) * 100))
            self.prev = (now, rc6)


class AmdGpu:
    """AMD via the amdgpu sysfs interface: busy %, VRAM, temperature, power and clock (no extra tools needed)."""

    def __init__(self, card: str) -> None:
        self.card = card
        self.dev = f"{card}/device"
        hw = glob.glob(f"{self.dev}/hwmon/hwmon*")
        self.hwmon = hw[0] if hw else None
        self.name = self._name()
        self.busy = 0.0
        self.vram_used = self.vram_total = 0
        self.temp: float | None = None
        self.power: float | None = None
        self.freq = 0

    def _name(self) -> str:
        # amdgpu exposes a marketing name via product_name on newer kernels; fall back to the PCI id
        name = read_file(f"{self.dev}/product_name") or ""
        if not name:
            try:
                slot = os.path.basename(os.path.realpath(self.dev)).split(":", 1)[-1]
                for line in subprocess.run(["lspci", "-mm", "-s", slot], capture_output=True, text=True, timeout=3).stdout.splitlines():
                    parts = [p.strip('"') for p in line.split('" "')]
                    if len(parts) >= 4:
                        name = parts[3]
            except Exception:
                name = ""
        return (name or "AMD GPU").replace("Advanced Micro Devices, Inc. [AMD/ATI]", "AMD")

    @staticmethod
    def find() -> "AmdGpu | None":
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
            if read_file(f"{card}/device/vendor") == "0x1002" and read_file(f"{card}/device/gpu_busy_percent") is not None:
                return AmdGpu(card)
        return None

    def sample(self) -> None:
        self.busy = float(read_int(f"{self.dev}/gpu_busy_percent", 0) or 0)
        self.vram_used = read_int(f"{self.dev}/mem_info_vram_used", 0) or 0
        self.vram_total = read_int(f"{self.dev}/mem_info_vram_total", 0) or 0
        if self.hwmon:
            t = read_int(f"{self.hwmon}/temp1_input")
            self.temp = t / 1000 if t is not None else None
            p = read_int(f"{self.hwmon}/power1_average") or read_int(f"{self.hwmon}/power1_input")
            self.power = p / 1e6 if p is not None else None
            f = read_int(f"{self.hwmon}/freq1_input")
            self.freq = f // 1_000_000 if f else 0


class GpuPanel(Panel):
    name = "gpu"
    title = "GPU"
    interval = 1.0
    help = {"p": "toggle process list"}
    options = {"procs": "true/false — per-process VRAM list (default true)", "intel": "true/false — show Intel iGPU (default true)"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.util = Hist()
        self.vram = Hist()
        self.snap = {"gpus": [], "apps": [], "error": None}
        self.intel = IntelGpu.find() if bool(self.cfg.get("intel", True)) else None
        self.intel_hist = Hist()
        self.amd = AmdGpu.find()
        self.amd_hist = Hist()
        self.show_procs = bool(self.cfg.get("procs", True))

    def on_key(self, key: str) -> bool:
        if key == "p":
            self.show_procs = not self.show_procs
            return True
        return False

    def sample(self, dt: float) -> None:
        self.snap = _nvidia.poll(max_age=self.interval * 0.8)
        if self.snap["gpus"]:
            g = self.snap["gpus"][0]
            self.util.push(g.get("utilization.gpu") or 0.0)
            tot = g.get("memory.total") or 0
            self.vram.push((g.get("memory.used") or 0) / tot * 100 if tot else 0)
        if self.intel:
            self.intel.sample()
            self.intel_hist.push(self.intel.busy)
        if self.amd:
            self.amd.sample()
            self.amd_hist.push(self.amd.busy)

    def status(self) -> str:
        g = self.snap["gpus"][0] if self.snap["gpus"] else None
        return (g.get("pstate") or "") if g else ""

    @staticmethod
    def _proc_name(name: str) -> str:
        return name.replace("\\", "/").rsplit("/", 1)[-1]

    def render(self, width: int, height: int):
        th = self.theme
        rows: list = []
        gpus = self.snap["gpus"]
        if not gpus and not self.intel and not self.amd:
            return Text(self.snap.get("error") or "no GPU found", style=th.dim)
        if self.amd:
            a = self.amd
            head = Text.assemble((fit(a.name, max(6, width // 2)), f"bold {th.accent}"), "  ")
            facts = []
            if a.temp is not None:
                facts.append(("", f"{a.temp:.0f}°C", th.temp_style(a.temp)))
            if a.power is not None:
                facts.append(("", f"{a.power:.0f}W", None))
            if a.freq:
                facts.append(("", f"{a.freq}MHz", None))
            head.append_text(kv_line(facts, width - len(head.plain), th))
            rows.append(head)
            rows.append(meter("busy", a.busy / 100, f"{a.busy:.0f}%", width, th, label_w=5, value_w=5))
            if a.vram_total:
                val = f"{a.vram_used / 2**30:.1f}/{a.vram_total / 2**30:.1f}G"
                rows.append(meter("vram", a.vram_used / a.vram_total, val, width, th, label_w=5, value_w=len(val)))
        for g in gpus[:1]:
            util = g.get("utilization.gpu") or 0.0
            used, total = g.get("memory.used") or 0, g.get("memory.total") or 0
            name = (g.get("name") or "GPU").replace("NVIDIA ", "").replace("GeForce ", "")
            head = Text.assemble((fit(name, max(6, width // 2)), f"bold {th.accent}"), "  ")
            facts = []
            t = g.get("temperature.gpu")
            if t is not None:
                facts.append(("", f"{t:.0f}°C", th.temp_style(t, 70, 85)))
            pw = g.get("power.draw")
            if pw is not None:
                lim = g.get("power.limit")
                facts.append(("", f"{pw:.0f}W" + (f"/{lim:.0f}W" if lim else ""), None))
            if g.get("clocks.gr") is not None:
                facts.append(("", f"{g['clocks.gr']:.0f}MHz", None))
            if g.get("clocks.mem") is not None:
                facts.append(("mem", f"{g['clocks.mem']:.0f}MHz", None))
            fan = g.get("fan.speed")
            if fan is not None:
                facts.append(("fan", f"{fan:.0f}%", None))
            head.append_text(kv_line(facts, width - len(head.plain), th))
            rows.append(head)
            rows.append(meter("util", util / 100, f"{util:.0f}%", width, th, label_w=5, value_w=5))
            vram_val = f"{used / 1024:.1f}/{total / 1024:.1f}G" if width >= 36 else f"{used / total * 100 if total else 0:.0f}%"
            rows.append(meter("vram", used / total if total else 0, vram_val, width, th, label_w=5, value_w=len(vram_val)))
        if self.intel:
            head = Text.assemble((self.intel.name, f"bold {th.accent}"), "  ")
            head.append_text(kv_line([("", f"{self.intel.freq}MHz", None), ("max", f"{self.intel.max_freq}MHz", th.dim)],
                                     width - len(head.plain), th))
            rows.append(head)
            rows.append(meter("busy", self.intel.busy / 100, f"{self.intel.busy:.0f}%", width, th, label_w=5, value_w=5))
        apps = self.snap["apps"] if self.show_procs else []
        proc_lines = min(len(apps), max(0, height - len(rows) - 3)) if apps else 0
        graph_h = height - len(rows) - proc_lines - (1 if proc_lines else 0)
        if gpus and graph_h >= 2:
            rows.append(braille_graph(self.util.data, width, graph_h, 100, th))
        elif gpus and graph_h == 1:
            rows.append(sparkline(self.util.data, width, 100, th))
        elif not gpus and self.amd and graph_h >= 1:
            rows.append(braille_graph(self.amd_hist.data, width, graph_h, 100, th) if graph_h >= 2
                        else sparkline(self.amd_hist.data, width, 100, th))
        elif not gpus and self.intel and graph_h >= 1:
            rows.append(braille_graph(self.intel_hist.data, width, graph_h, 100, th) if graph_h >= 2
                        else sparkline(self.intel_hist.data, width, 100, th))
        if proc_lines:
            rows.append(Text(pad("PID", 7, "right") + " " + pad("VRAM", 7, "right") + " PROCESS", style=f"bold {th.dim}"))
            for a in sorted(apps, key=lambda x: -x["mem"])[:proc_lines]:
                rows.append(Text.assemble((pad(a["pid"], 7, "right"), th.dim), " ",
                                          (pad(f"{a['mem']:.0f}M", 7, "right"), th.accent), " ",
                                          fit(self._proc_name(a["name"]), width - 16)))
        return Group(*rows)
