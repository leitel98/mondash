"""`mondash --doctor`: what this machine exposes to each panel, and how the programs running right now are
classified by the jobs and downloads trackers. Paste its output into a bug report when something is missing."""
from __future__ import annotations

import glob
import os
import platform
import shutil
import sys

from . import __version__


def _yes(v) -> str:
    return "yes" if v else "no"


def main(argv: list[str] | None = None) -> None:
    import psutil

    from . import dlwatch, jobs
    from .core import CONFIG_FILE, USER_GIFS, find_terminal, load_config
    from .host import cpu_model, cpu_temperature, default_iface, os_name, temperatures
    from .panels import _nvidia
    from .panels.gpu import AmdGpu, IntelGpu
    from .procscan import SCANNER

    verbose = "-v" in (argv or []) or "--verbose" in (argv or [])
    conf = load_config()
    print(f"mondash {__version__} · python {platform.python_version()} · {os_name()} · kernel {platform.release()} · {platform.machine()}")
    print(f"config    {CONFIG_FILE} ({'exists' if CONFIG_FILE.exists() else 'will be created'})")
    print(f"gifs      {USER_GIFS} ({len(glob.glob(str(USER_GIFS / '*.gif')))} files); pillow {_yes(_has('PIL'))}")
    term = find_terminal("x")
    print(f"terminal  {term[0] if term else 'none found (-w will not work; set $TERMINAL)'} · TERM={os.environ.get('TERM', '')} "
          f"· COLORTERM={os.environ.get('COLORTERM', '')}")
    print()
    print("sources")
    print(f"  cpu       {cpu_model()} · {psutil.cpu_count()} threads · freq {_yes(_safe(psutil.cpu_freq))} · psi {_yes(os.path.exists('/proc/pressure/cpu'))}")
    temps = temperatures()
    print(f"  sensors   {', '.join(sorted(temps)) or 'none'} · cpu temp {cpu_temperature() or 'n/a'}")
    fans = _safe(psutil.sensors_fans) or {}
    print(f"  fans      {', '.join(sorted(fans)) or 'none'}")
    print(f"  gpu       nvidia: {_nvidia.BACKEND} · amd: {_yes(AmdGpu.find())} · intel: {_yes(IntelGpu.find())}")
    bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
    print(f"  power     battery: {', '.join(os.path.basename(b) for b in bats) or 'none'} · rapl {_yes(glob.glob('/sys/class/powercap/intel-rapl:*'))} "
          f"· backlight {_yes(glob.glob('/sys/class/backlight/*'))} · platform_profile {_yes(os.path.exists('/sys/firmware/acpi/platform_profile'))}")
    print(f"  network   default iface {default_iface() or 'none'} · all: {', '.join(i for i in psutil.net_io_counters(pernic=True) if i != 'lo')}")
    disks = _safe(psutil.disk_io_counters, perdisk=True) or {}
    print(f"  disks     io counters: {', '.join(sorted(disks)) or 'none'}")
    print(f"  hardware  lspci {_yes(shutil.which('lspci'))} · udevadm {_yes(shutil.which('udevadm'))} · dmi {_yes(os.path.exists('/sys/class/dmi/id'))}")
    print(f"  steam     libraries: {', '.join(dlwatch.SteamTracker._libraries()) or 'none'}")
    print()

    # classification of what runs now
    snap = SCANNER.snapshot(0)
    dl_cfg = conf.get("panels", {}).get("dl", {})
    jobs_cfg = conf.get("panels", {}).get("jobs", {})
    tracker = dlwatch.Tracker(set(dl_cfg.get("tools", [])), set(dl_cfg.get("ignore", [])))
    jt = jobs.JobTracker(tools=jobs_cfg.get("tools", {}) or {}, ignore=set(jobs_cfg.get("ignore", [])))
    dls, jbs, cands = [], [], []
    for row in snap.rows:
        if row["kernel"] or row["pid"] == os.getpid():
            continue
        comm = tracker.comm_map.get(row["name"])
        if comm:
            k = tracker._classify_row(row, comm)
            if k:
                dls.append((row["pid"], comm, k[1]))
        det = jt._det(row)
        if det:
            jbs.append((row["pid"], row["name"], det.kind, det.label, "finite" if det.finite else "daemon"))
        elif row["cpu"] >= 5 or verbose:
            cands.append((row["pid"], SCANNER.display_name(row), row["cpu"]))
    print(f"downloads recognised now ({len(dls)})")
    for pid, comm, label in dls:
        print(f"  {pid:>7}  {comm:<14} {label}")
    print(f"jobs recognised now ({len(jbs)})")
    for pid, name, kind, label, fin in jbs:
        print(f"  {pid:>7}  {name:<15} {kind:<9} {fin:<6} {label}")
    # second snapshot so CPU% means something
    import time
    time.sleep(0.6)
    snap = SCANNER.snapshot(0)
    hot = sorted((r for r in snap.rows if not r["kernel"] and r["cpu"] >= 5 and jt._det(r) is None and r["pid"] != os.getpid()),
                 key=lambda r: -r["cpu"])[:15]
    print(f"busy but not recognised as a job or download (cpu ≥ 5%): {len(hot)}")
    for r in hot:
        cmd = SCANNER.cmdline(r)
        print(f"  {r['pid']:>7}  {SCANNER.display_name(r):<20} {r['cpu']:5.0f}%  {cmd[:90]}")
    if hot:
        print()
        print("to make one of these a job:   [panels.jobs]  tools = { \"name\" = \"media\" }   (kinds: " + " ".join(jobs.KINDS) + ")")
        print("to make one a download:       [panels.dl]    tools = [\"name\"]")
        print("to hide one from busy:        [panels.jobs]  busy_ignore = [\"name\"]")
    if not sys.stdout.isatty():
        return


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception:
        return None
