"""Power: battery state & health, AC, power draw history, CPU governor/EPP, platform profile, backlight."""
from __future__ import annotations

import glob
import time

from rich.console import Group
from rich.text import Text

from ..core import Hist, Panel, braille_graph, duration, kv_line, meter, nice_max, read_file, read_int, sparkline


class PowerPanel(Panel):
    name = "power"
    title = "Power"
    interval = 2.0
    options = {"battery": "battery name under /sys/class/power_supply (default: first BAT*)"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
        want = self.cfg.get("battery")
        self.bat = f"/sys/class/power_supply/{want}" if want else (bats[0] if bats else None)
        self.acs = [p for p in glob.glob("/sys/class/power_supply/*") if (read_file(f"{p}/type") or "") == "Mains"]
        self.draw = Hist()
        self.info: dict = {}
        self.rapl = sorted(glob.glob("/sys/class/powercap/intel-rapl:*/energy_uj"))
        self.rapl_prev: tuple[float, int] | None = None
        self.rapl_w: float | None = None
        self.rapl_hist = Hist()
        bl = sorted(glob.glob("/sys/class/backlight/*"))
        self.backlight = bl[0] if bl else None

    def sample(self, dt: float) -> None:
        info: dict = {}
        if self.bat:
            b = self.bat
            info["status"] = read_file(f"{b}/status", "?")
            info["capacity"] = read_int(f"{b}/capacity", 0)
            v = read_int(f"{b}/voltage_now")
            i = read_int(f"{b}/current_now")
            p = read_int(f"{b}/power_now")
            watts = (p / 1e6) if p is not None else ((v or 0) * (i or 0) / 1e12 if v is not None and i is not None else None)
            info["watts"] = watts
            if watts is not None:
                self.draw.push(watts)
            full = read_int(f"{b}/charge_full") or read_int(f"{b}/energy_full")
            design = read_int(f"{b}/charge_full_design") or read_int(f"{b}/energy_full_design")
            now = read_int(f"{b}/charge_now") or read_int(f"{b}/energy_now")
            info["health"] = (full / design * 100) if full and design else None
            info["cycles"] = read_int(f"{b}/cycle_count")
            info["model"] = f"{read_file(f'{b}/manufacturer', '')} {read_file(f'{b}/model_name', '')}".strip()
            info["tech"] = read_file(f"{b}/technology", "")
            # time left from current rate
            if watts and now and full and info["status"] in ("Discharging", "Charging"):
                unit_rate = (i / 1e6) if (i and read_int(f"{b}/charge_now") is not None) else (watts if watts else 0)
                remaining = now / 1e6
                if info["status"] == "Discharging" and unit_rate:
                    info["left"] = remaining / unit_rate * 3600
                elif info["status"] == "Charging" and unit_rate:
                    info["left"] = (full / 1e6 - remaining) / unit_rate * 3600
        info["ac"] = any((read_int(f"{p}/online", 0) or 0) == 1 for p in self.acs) if self.acs else None
        if self.rapl:
            uj = read_int(self.rapl[0])
            now = time.monotonic()
            if uj is not None:
                if self.rapl_prev and uj >= self.rapl_prev[1]:
                    self.rapl_w = (uj - self.rapl_prev[1]) / 1e6 / (now - self.rapl_prev[0])
                    self.rapl_hist.push(self.rapl_w)
                self.rapl_prev = (now, uj)
        info["governor"] = read_file("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        info["epp"] = read_file("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference")
        info["profile"] = read_file("/sys/firmware/acpi/platform_profile")
        if self.backlight:
            cur, mx = read_int(f"{self.backlight}/brightness"), read_int(f"{self.backlight}/max_brightness")
            info["backlight"] = (cur / mx * 100) if cur is not None and mx else None
        self.info = info

    def render(self, width: int, height: int):
        th, info = self.theme, self.info
        rows: list = []
        if self.bat and info:
            cap = info.get("capacity") or 0
            status = info.get("status", "?")
            st_style = {"Charging": th.good, "Full": th.good, "Discharging": th.warn, "Not charging": th.dim}.get(status, th.dim)
            palette = [th.bad, th.warn, th.good]
            rows.append(meter("batt", cap / 100, f"{cap}%", width, th, label_w=5, value_w=5, palette=palette))
            facts = [("", status.lower(), st_style)]
            if info.get("watts") is not None:
                facts.append(("", f"{info['watts']:.1f}W", th.accent))
            if info.get("left"):
                facts.append(("left", duration(info["left"]), None))
            if info.get("ac") is not None:
                facts.append(("ac", "on" if info["ac"] else "off", th.good if info["ac"] else th.dim))
            rows.append(kv_line(facts, width, th))
            facts2 = []
            if info.get("health") is not None:
                facts2.append(("health", f"{info['health']:.0f}%", th.level(100 - info["health"]) ))
            if info.get("cycles"):
                facts2.append(("cycles", str(info["cycles"]), None))
            if info.get("model"):
                facts2.append(("", info["model"], th.dim))
            if facts2:
                rows.append(kv_line(facts2, width, th))
        elif info.get("ac") is not None:
            rows.append(kv_line([("ac", "on" if info["ac"] else "off", th.good if info["ac"] else th.dim)], width, th))
        sys_facts = []
        if self.rapl_w is not None:
            sys_facts.append(("cpu pkg", f"{self.rapl_w:.1f}W", th.accent))
        if info.get("governor"):
            sys_facts.append(("gov", info["governor"], None))
        if info.get("epp"):
            sys_facts.append(("epp", info["epp"], None))
        if info.get("profile"):
            sys_facts.append(("profile", info["profile"], None))
        if info.get("backlight") is not None:
            sys_facts.append(("screen", f"{info['backlight']:.0f}%", None))
        if sys_facts:
            rows.append(kv_line(sys_facts, width, th))
        graph_h = height - len(rows)
        series = self.draw if len(self.draw) else self.rapl_hist
        if graph_h >= 2 and len(series):
            rows.append(braille_graph(series.data, width, graph_h, nice_max(series.peak(), 5), th, palette=[th.good, th.warn, th.bad]))
        elif graph_h == 1 and len(series):
            rows.append(sparkline(series.data, width, nice_max(series.peak(), 5), th))
        if not rows:
            rows.append(Text("no battery or power info", style=th.dim))
        return Group(*rows)
