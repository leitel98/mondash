"""Disks: filesystem usage meters (deduped by device) and per-device read/write throughput."""
from __future__ import annotations

import psutil
from rich.console import Group
from rich.text import Text

from ..core import Hist, Panel, braille_graph, fit, human, kv_line, meter, nice_max, pad, rate, read_psi, sparkline

PSEUDO = {"tmpfs", "devtmpfs", "overlay", "squashfs", "efivarfs", "proc", "sysfs", "cgroup2", "autofs", "fuse.portal",
          "fusectl", "debugfs", "tracefs", "configfs", "securityfs", "pstore", "bpf", "hugetlbfs", "mqueue", "binfmt_misc",
          "ramfs", "nsfs", "selinuxfs", "fuse.gvfsd-fuse", "rpc_pipefs"}


class DiskPanel(Panel):
    name = "disk"
    title = "Disks"
    interval = 2.0
    help = {"i": "I/O section"}
    options = {"mounts": "list of mount points to show (default: all real filesystems)",
               "devices": "list of block devices for I/O (default: whole disks)", "io": "true/false — I/O section"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.mounts: list[tuple[str, str, psutil._common.sdiskusage]] = []
        self.io_hist: dict[str, tuple[Hist, Hist]] = {}
        self.io_rates: dict[str, tuple[float, float]] = {}
        self.prev_io: dict[str, tuple[int, int]] = {}
        self.show_io = bool(self.cfg.get("io", True))
        self.psi: str | None = None

    def on_key(self, key: str) -> bool:
        if key == "i":
            self.show_io = not self.show_io
            return True
        return False

    def _devices(self, counters: dict) -> list[str]:
        want = self.cfg.get("devices")
        if want:
            return [d for d in want if d in counters]
        devs = []
        for d in counters:
            if d.startswith(("loop", "ram", "zram", "dm-", "sr")):
                continue
            # keep whole disks: nvme0n1 (not nvme0n1p1), sda (not sda1)
            if d.startswith("nvme") and "p" in d[len("nvme0n1"):]:
                continue
            if d[:2] in ("sd", "vd", "hd") and d[-1].isdigit():
                continue
            if d.startswith("mmcblk") and "p" in d:
                continue
            devs.append(d)
        return devs

    PREFER = ["/", "/var/home", "/home", "/var", "/boot", "/boot/efi"]

    @classmethod
    def _rank(cls, mp: str) -> tuple[int, int, str]:
        return (cls.PREFER.index(mp) if mp in cls.PREFER else len(cls.PREFER), len(mp), mp)

    def sample(self, dt: float) -> None:
        want = self.cfg.get("mounts")
        seen_dev: dict[str, str] = {}
        mounts = []
        for p in psutil.disk_partitions(all=False):
            if p.fstype in PSEUDO or p.mountpoint.startswith(("/proc", "/sys", "/dev", "/run")):
                continue
            if want and p.mountpoint not in want:
                continue
            try:
                usage = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            if not want:
                # btrfs subvolumes and bind mounts share a device: keep the most meaningful mount point
                key = p.device
                if key in seen_dev and self._rank(seen_dev[key]) <= self._rank(p.mountpoint):
                    continue
                seen_dev[key] = p.mountpoint
                mounts = [m for m in mounts if m[3] != key]
            mounts.append((p.mountpoint, p.fstype, usage, p.device))
        mounts.sort(key=lambda m: self._rank(m[0]))
        self.mounts = [(m[0], m[1], m[2]) for m in mounts]
        counters = psutil.disk_io_counters(perdisk=True) or {}
        for dev in self._devices(counters):
            c = counters[dev]
            cur = (c.read_bytes, c.write_bytes)
            if dev in self.prev_io:
                r = max(0.0, (cur[0] - self.prev_io[dev][0]) / dt)
                w = max(0.0, (cur[1] - self.prev_io[dev][1]) / dt)
                self.io_rates[dev] = (r, w)
                hr, hw = self.io_hist.setdefault(dev, (Hist(), Hist()))
                hr.push(r)
                hw.push(w)
            self.prev_io[dev] = cur
        self.psi = read_psi("io")

    def render(self, width: int, height: int):
        th = self.theme
        rows: list = []
        label_w = min(max((len(m[0]) for m in self.mounts), default=6), max(6, width // 3), 18)
        if self.mounts and height > len(self.mounts) + 2:
            total = sum(u.total for _, _, u in self.mounts)
            free = sum(u.free for _, _, u in self.mounts)
            rows.append(kv_line([("storage", f"{human(total - free)} used", th.accent), ("", f"{human(free)} free", th.good),
                                 ("of", human(total), None)], width, th))
        for mp, fs, u in self.mounts:
            if len(rows) >= height:
                break
            label = fit(mp, label_w, tail=True)
            if width >= 64:
                value = f"{human(u.used)} used · {human(u.free)} free of {human(u.total)}"
            elif width >= 44:
                value = f"{human(u.used)}/{human(u.total)} · {human(u.free)} free"
            elif width >= 34:
                value = f"{human(u.used)}/{human(u.total)}"
            else:
                value = f"{u.percent:.0f}%"
            line = meter(pad(label, label_w), u.percent / 100, value, width, th, label_w=label_w, value_w=len(value))
            rows.append(line)
        if self.show_io and self.io_rates and len(rows) < height:
            devs = list(self.io_rates)
            remaining = height - len(rows)
            if remaining >= 1:
                rows.append(Text(""))
                remaining -= 1
            for dev in devs:
                if remaining <= 0:
                    break
                r, w = self.io_rates[dev]
                hr, hw = self.io_hist[dev]
                text = Text.assemble((pad(dev, 9), th.dim), (" R ", th.dim), (pad(rate(r), 9, "right"), th.good),
                                     ("  W ", th.dim), (pad(rate(w), 9, "right"), th.warn), " ")
                spare = width - len(text.plain)
                if spare >= 6:
                    combined = [a + b for a, b in zip(hr.data, hw.data)]
                    text.append_text(sparkline(combined, spare, nice_max(max(combined[-spare:] or [1]), 1024 * 1024), th))
                rows.append(text)
                remaining -= 1
            if remaining >= 3 and devs:
                dev = devs[0]
                hr, hw = self.io_hist[dev]
                combined = [a + b for a, b in zip(hr.data, hw.data)]
                rows.append(braille_graph(combined, width, remaining - (1 if self.psi else 0), nice_max(max(combined[-2 * width:] or [1]), 1024 * 1024), th))
        if self.psi and len(rows) < height:
            rows.append(kv_line([("io pressure", self.psi, None)], width, th))
        if not rows:
            rows.append(Text("no filesystems", style=th.dim))
        return Group(*rows)
