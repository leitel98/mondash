"""Hardware inventory: machine, CPU, memory modules (type/speed), GPUs, storage, network chips, audio, display, battery.

Everything comes from sysfs, udev's DMI export and lspci, so no root is needed. Sampled once, refreshed rarely.
"""
from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import subprocess

from rich.console import Group
from rich.text import Text

from . import _nvidia
from ..core import Panel, fit, human, read_file, read_int

JEDEC = {"802C": "Micron", "80AD": "SK hynix", "80CE": "Samsung", "0198": "Kingston", "029E": "Corsair", "04CB": "ADATA",
         "059B": "Crucial", "04CD": "G.Skill", "01F7": "Team Group", "0B0B": "Lexar", "8A76": "Leven"}
PART_PREFIX = {"LD4": "Leven", "LD5": "Leven", "KF": "Kingston Fury", "KVR": "Kingston", "CT": "Crucial", "BL": "Crucial Ballistix",
               "CM": "Corsair", "M378": "Samsung", "M471": "Samsung", "HMA": "SK hynix", "HMT": "SK hynix", "MTA": "Micron",
               "F4-": "G.Skill", "F5-": "G.Skill", "TF": "Team Group", "TL": "Team Group", "AX": "ADATA", "AD": "ADATA"}
CHASSIS = {"3": "desktop", "4": "low-profile desktop", "8": "portable", "9": "laptop", "10": "notebook", "13": "all-in-one",
           "14": "sub-notebook", "30": "tablet", "31": "convertible", "32": "detachable", "35": "mini PC"}


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _lspci() -> list[dict]:
    if not shutil.which("lspci"):
        return []
    out = []
    for line in _run(["lspci", "-mm"]).splitlines():
        fields = re.findall(r'"([^"]*)"|(\S+)', line)
        flat = [a or b for a, b in fields]
        if len(flat) < 4:
            continue
        slot, cls, vendor, device = flat[0], flat[1], flat[2], flat[3]
        out.append({"slot": slot, "class": cls, "vendor": vendor, "device": device})
    return out


def _clean_vendor(v: str) -> str:
    for junk in (" Corporation", " Semiconductor Co., Ltd.", " Incorporated", ", Inc.", " Inc.", " Ltd.", " Co."):
        v = v.replace(junk, "")
    return v.strip()


def _clean_device(d: str) -> str:
    return re.sub(r"\s+", " ", d).strip()


class HwPanel(Panel):
    name = "hw"
    title = "Hardware"
    interval = 300.0
    help = {"↑ ↓": "scroll", "r": "re-scan"}
    options = {"sections": "ordered list from: system cpu memory gpu storage network audio display battery os"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.sections: list[tuple[str, list[tuple[str, str]]]] = []
        self.scroll = 0
        self.order = self.cfg.get("sections", ["system", "cpu", "memory", "gpu", "storage", "network", "audio", "display", "battery", "os"])

    # ------------------------------------------------------------------ collectors
    def _system(self) -> list[tuple[str, str]]:
        dmi = lambda k: read_file(f"/sys/class/dmi/id/{k}", "") or ""
        rows = []
        model = " ".join(x for x in (dmi("sys_vendor"), dmi("product_name")) if x)
        if model:
            rows.append(("model", model + (f"  ({dmi('product_family')})" if dmi("product_family") and dmi("product_family") not in model else "")))
        kind = CHASSIS.get(dmi("chassis_type"))
        if kind:
            rows.append(("type", kind))
        board = " ".join(x for x in (dmi("board_vendor"), dmi("board_name")) if x)
        if board:
            rows.append(("board", board))
        bios = " ".join(x for x in (dmi("bios_vendor"), dmi("bios_version"), dmi("bios_date")) if x)
        if bios:
            rows.append(("bios", bios))
        return rows

    def _cpu(self) -> list[tuple[str, str]]:
        info = read_file("/proc/cpuinfo") or ""
        model = ""
        for line in info.splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
        model = re.sub(r"\((R|TM)\)", "", model).replace(" CPU", "")
        model = " ".join(model.split())
        cores = len({l.split(":")[1].strip() for l in info.splitlines() if l.startswith("core id")}) or 0
        threads = info.count("processor\t:")
        rows = [("model", model)]
        rows.append(("cores", f"{cores} cores / {threads} threads" if cores else f"{threads} threads"))
        mx = read_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
        mn = read_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq")
        if mx:
            rows.append(("clock", f"{(mn or 0) / 1e6:.1f} – {mx / 1e6:.1f} GHz"))
        caches = []
        for idx in sorted(glob.glob("/sys/devices/system/cpu/cpu0/cache/index*")):
            lvl, typ, size = read_file(f"{idx}/level"), read_file(f"{idx}/type"), read_file(f"{idx}/size")
            if lvl and size and typ != "Instruction":
                if size.endswith("K") and size[:-1].isdigit() and int(size[:-1]) >= 1024:
                    size = f"{int(size[:-1]) // 1024}M"
                caches.append(f"L{lvl}{'d' if typ == 'Data' else ''} {size}")
        if caches:
            rows.append(("cache", "  ".join(caches)))
        gov = read_file("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver")
        if gov:
            rows.append(("driver", gov))
        return rows

    def _memory(self) -> list[tuple[str, str]]:
        rows = []
        total = 0
        for line in (read_file("/proc/meminfo") or "").splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
        props: dict[str, str] = {}
        for line in _run(["udevadm", "info", "/sys/devices/virtual/dmi/id"]).splitlines():
            if line.startswith("E: MEMORY_"):
                k, _, v = line[3:].partition("=")
                props[k] = v
        slots = sorted({int(m.group(1)) for k in props for m in [re.match(r"MEMORY_DEVICE_(\d+)_", k)] if m})
        modules = []
        for i in slots:
            g = lambda k: props.get(f"MEMORY_DEVICE_{i}_{k}", "")
            size = int(g("SIZE") or 0)
            if not size:
                continue
            vendor = JEDEC.get(g("MANUFACTURER").upper(), "")
            part = g("PART_NUMBER").strip()
            if not vendor:
                for pre, name in PART_PREFIX.items():
                    if part.upper().startswith(pre):
                        vendor = name
                        break
            if not vendor and g("MANUFACTURER") and not re.fullmatch(r"[0-9A-Fa-f]{4}", g("MANUFACTURER")):
                vendor = g("MANUFACTURER")
            speed = g("CONFIGURED_SPEED_MTS") or g("SPEED_MTS")
            desc = " ".join(x for x in (human(size), g("TYPE"), f"{speed} MT/s" if speed else "", g("FORM_FACTOR")) if x)
            modules.append((f"slot {i}", desc + ((f"  {vendor}" if vendor else "") + (f" {part}" if part else "")).rstrip()))
        kind = props.get("MEMORY_DEVICE_0_TYPE", "")
        summary = f"{human(total)}" + (f" {kind}" if kind else "")
        if modules:
            summary += f"  ·  {len(modules)} module{'s' if len(modules) != 1 else ''}"
        cap = int(props.get("MEMORY_ARRAY_MAX_CAPACITY", "0") or 0)
        if cap:
            summary += f"  ·  max {human(cap)}"
        rows.append(("total", summary))
        rows.extend(modules)
        if not modules and not kind:
            rows.append(("note", "module details need DMI data (udev did not export it)"))
        return rows

    def _gpu(self, pci: list[dict]) -> list[tuple[str, str]]:
        rows = []
        nv = {g["name"]: g for g in _nvidia.poll(max_age=5).get("gpus", [])}
        for dev in pci:
            if "VGA" in dev["class"] or "3D" in dev["class"] or "Display" in dev["class"]:
                name = _clean_device(dev["device"])
                m = re.search(r"\[(.+?)\]", name)
                pretty = m.group(1) if m else name
                extra = ""
                for gname, g in nv.items():
                    if any(tok in gname for tok in pretty.split()[-2:]) and g.get("memory.total"):
                        extra = f"  ·  {g['memory.total'] / 1024:.0f} GB VRAM"
                rows.append((_clean_vendor(dev["vendor"]), pretty + extra))
        drivers = sorted({os.path.basename(os.path.realpath(c)) for c in glob.glob("/sys/class/drm/card[0-9]/device/driver")})
        if drivers:
            rows.append(("drivers", ", ".join(drivers)))
        return rows

    def _storage(self) -> list[tuple[str, str]]:
        rows = []
        for blk in sorted(glob.glob("/sys/block/*")):
            name = os.path.basename(blk)
            if name.startswith(("loop", "ram", "zram", "dm-", "sr")):
                continue
            size = (read_int(f"{blk}/size", 0) or 0) * 512
            if not size:
                continue
            model = " ".join((read_file(f"{blk}/device/model", "") or "").split())
            rot = read_int(f"{blk}/queue/rotational", 0)
            kind = "NVMe" if name.startswith("nvme") else "HDD" if rot else "SSD"
            fw = read_file(f"{blk}/device/firmware_rev") or ""
            rows.append((name, " ".join(x for x in (f"{size / 1e9:.0f} GB", kind, model, fw.strip()) if x)))
        return rows

    def _network(self, pci: list[dict]) -> list[tuple[str, str]]:
        rows = []
        by_slot = {d["slot"]: d for d in pci}
        for iface in sorted(os.listdir("/sys/class/net")):
            if iface == "lo":
                continue
            dev = f"/sys/class/net/{iface}/device"
            if not os.path.exists(dev):
                continue
            slot = os.path.basename(os.path.realpath(dev)).split(":", 1)[-1]
            d = by_slot.get(slot)
            drv = os.path.basename(os.path.realpath(f"{dev}/driver")) if os.path.exists(f"{dev}/driver") else ""
            wifi = os.path.isdir(f"/sys/class/net/{iface}/wireless") or os.path.isdir(f"/sys/class/net/{iface}/phy80211")
            desc = f"{_clean_vendor(d['vendor'])} {_clean_device(d['device'])}" if d else (drv or "?")
            rows.append((f"{iface} ({'wifi' if wifi else 'eth'})", desc + (f"  ·  {drv}" if drv and d else "")))
        return rows

    def _audio(self, pci: list[dict]) -> list[tuple[str, str]]:
        return [(_clean_vendor(d["vendor"]), _clean_device(d["device"])) for d in pci if "Audio" in d["class"]]

    def _display(self) -> list[tuple[str, str]]:
        rows = []
        for conn in sorted(glob.glob("/sys/class/drm/card*-*")):
            if read_file(f"{conn}/status") != "connected":
                continue
            name = os.path.basename(conn).split("-", 1)[1]
            mode = (read_file(f"{conn}/modes") or "").splitlines()
            desc = mode[0] if mode else ""
            try:
                edid = open(f"{conn}/edid", "rb").read()
            except OSError:
                edid = b""
            if len(edid) >= 128:
                mfg = ((edid[8] << 8) | edid[9])
                letters = "".join(chr(64 + ((mfg >> s) & 31)) for s in (10, 5, 0))
                w_cm, h_cm = edid[21], edid[22]
                import math
                inch = math.hypot(w_cm, h_cm) / 2.54 if w_cm and h_cm else 0
                desc += f"  {letters}" + (f"  {inch:.1f}\"" if inch else "")
                for i in range(54, 126, 18):
                    if edid[i:i + 3] == b"\0\0\0" and edid[i + 3] == 0xFC:
                        desc += "  " + edid[i + 5:i + 18].decode(errors="ignore").strip()
            rows.append((name, desc.strip()))
        return rows

    def _battery(self) -> list[tuple[str, str]]:
        rows = []
        for bat in sorted(glob.glob("/sys/class/power_supply/BAT*")):
            model = " ".join(x for x in (read_file(f"{bat}/manufacturer", ""), read_file(f"{bat}/model_name", ""), read_file(f"{bat}/technology", "")) if x)
            design = read_int(f"{bat}/energy_full_design") or 0
            full = read_int(f"{bat}/energy_full") or 0
            if not design:
                v = read_int(f"{bat}/voltage_min_design") or 0
                design = (read_int(f"{bat}/charge_full_design") or 0) * v // 1_000_000
                full = (read_int(f"{bat}/charge_full") or 0) * v // 1_000_000
            desc = model
            if design:
                desc += f"  ·  {design / 1e6:.0f} Wh design"
                if full:
                    desc += f", {full / 1e6:.0f} Wh now ({full * 100 // design}% health)"
            cyc = read_int(f"{bat}/cycle_count")
            if cyc:
                desc += f"  ·  {cyc} cycles"
            rows.append((os.path.basename(bat), desc))
        return rows

    def _os(self) -> list[tuple[str, str]]:
        pretty = ""
        for line in (read_file("/etc/os-release") or "").splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty = line.split("=", 1)[1].strip('"')
        rows = [("distro", pretty or platform.system()), ("kernel", platform.release())]
        de = os.environ.get("XDG_CURRENT_DESKTOP") or os.environ.get("DESKTOP_SESSION")
        if de:
            rows.append(("desktop", f"{de}  ·  {os.environ.get('XDG_SESSION_TYPE', '')}".strip(" ·")))
        return rows

    # ------------------------------------------------------------------ panel
    def sample(self, dt: float) -> None:
        pci = _lspci()
        collectors = {"system": self._system, "cpu": self._cpu, "memory": self._memory, "gpu": lambda: self._gpu(pci),
                      "storage": self._storage, "network": lambda: self._network(pci), "audio": lambda: self._audio(pci),
                      "display": self._display, "battery": self._battery, "os": self._os}
        sections = []
        for key in self.order:
            fn = collectors.get(key)
            if not fn:
                continue
            try:
                rows = [r for r in fn() if r and r[1]]
            except Exception as exc:
                rows = [("error", repr(exc))]
            if rows:
                sections.append((key, rows))
        self.sections = sections

    def on_key(self, key: str) -> bool:
        if key == "UP":
            self.scroll = max(0, self.scroll - 1)
        elif key == "DOWN":
            self.scroll += 1
        elif key == "r":
            self._last = 0.0
        else:
            return False
        return True

    def on_mouse(self, kind: str, x: int, y: int) -> bool:
        if kind == "WHEELUP":
            self.scroll = max(0, self.scroll - 2)
        elif kind == "WHEELDOWN":
            self.scroll += 2
        else:
            return False
        return True

    def _lines(self, width: int) -> list[Text]:
        th = self.theme
        out: list[Text] = []
        label_w = min(16, max(8, width // 4))
        for n, (key, rows) in enumerate(self.sections):
            if n:
                out.append(Text(""))
            out.append(Text(key.upper(), style=f"bold {th.accent}"))
            for label, value in rows:
                line = Text(no_wrap=True)
                line.append(fit(label, label_w).ljust(label_w), th.dim)
                line.append(" ")
                line.append(fit(value, width - label_w - 1))
                out.append(line)
        return out

    def status(self) -> str:
        return "↑↓ scroll" if self.scroll else ""

    def render(self, width: int, height: int):
        if not self.sections:
            return Text("scanning hardware…", style=self.theme.dim)
        lines = self._lines(width)
        if width >= 100 and len(lines) > height:
            # two columns
            col_w = (width - 3) // 2
            lines = self._lines(col_w)
            half = (len(lines) + 1) // 2
            # split at a section boundary near the middle so headers stay with their rows
            cut = half
            for i in range(half, min(len(lines), half + 8)):
                if lines[i].plain == "":
                    cut = i + 1
                    break
            left, right = lines[:cut], lines[cut:]
            while left and left[-1].plain == "":
                left.pop()
            merged = []
            for i in range(max(len(left), len(right))):
                row = Text(no_wrap=True)
                l = left[i] if i < len(left) else Text("")
                row.append_text(l)
                row.append(" " * max(0, col_w - len(l.plain)))
                row.append(" │ ", self.theme.border)
                if i < len(right):
                    row.append_text(right[i])
                merged.append(row)
            lines = merged
        self.scroll = max(0, min(self.scroll, max(0, len(lines) - height)))
        return Group(*lines[self.scroll:self.scroll + height])
