"""Readers for what the host exposes: /proc, /sys, sensors, the default route. No rich, no panels.

Everything here degrades to None / "" / 0 when a file is missing, so panels can run on machines that
lack a battery, a discrete GPU, hwmon sensors, or a default route.
"""
from __future__ import annotations

import platform
import re
import time
from pathlib import Path

HOME = str(Path.home())


def read_file(path: str | Path, default: str | None = None) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def read_int(path: str | Path, default: int | None = None) -> int | None:
    txt = read_file(path)
    try:
        return int(txt) if txt is not None else default
    except ValueError:
        return default


_cache: dict[str, tuple[float, object]] = {}


def cached(key: str, ttl: float, fn):
    """Share one expensive read (e.g. all hwmon sensors) between panels that sample in the same tick."""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (now, value)
    return value


def temperatures() -> dict:
    import psutil
    try:
        return cached("temps", 0.9, psutil.sensors_temperatures)
    except Exception:            # no hwmon at all (containers, some VMs)
        return {}


CPU_CHIPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal")     # hwmon chips that report the CPU


def cpu_temperature() -> float | None:
    """Hottest CPU sensor reading, whatever the chip is called, or None without sensors."""
    temps = [e.current for chip in CPU_CHIPS for e in temperatures().get(chip, [])]
    return max(temps) if temps else None


def read_psi(kind: str) -> str | None:
    """Pressure-stall 'some avg10' for cpu/memory/io, formatted, or None."""
    txt = read_file(f"/proc/pressure/{kind}")
    if not txt:
        return None
    for line in txt.splitlines():
        if line.startswith("some"):
            for part in line.split():
                if part.startswith("avg10="):
                    return part.split("=", 1)[1] + "%"
    return None


def default_iface() -> str | None:
    """Interface that carries the default route, else the one that has received the most bytes."""
    try:
        for line in Path("/proc/net/route").read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) > 1 and parts[1] == "00000000":
                return parts[0]
    except OSError:
        pass
    try:
        import psutil
        stats = {k: v.bytes_recv for k, v in psutil.net_io_counters(pernic=True).items() if k != "lo"}
        return max(stats, key=stats.get) if stats else None
    except Exception:
        return None


def os_name() -> str:
    for line in (read_file("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return platform.system()


def cpu_model() -> str:
    """'model name' from /proc/cpuinfo without the marketing noise ('Intel(R) Core(TM) i7-8750H CPU @ 2.20GHz' → 'Intel Core i7-8750H')."""
    for line in (read_file("/proc/cpuinfo") or "").splitlines():
        if line.startswith(("model name", "Model", "cpu model")):        # x86, arm (Raspberry Pi), mips
            name = line.split(":", 1)[1].strip()
            name = re.sub(r"\((R|TM|tm)\)", "", name)
            for junk in (" CPU", " Processor"):
                name = name.replace(junk, "")
            return " ".join(name.split()).split("@")[0].strip()
    return platform.machine() or "CPU"


def boot_time() -> float:
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except OSError:
        pass
    return time.time() - time.monotonic()


def tilde(path: str) -> str:
    return "~" + path[len(HOME):] if path.startswith(HOME) else path
