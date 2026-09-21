"""Fast process scanner: one read of /proc/PID/stat per process, ~10x cheaper than psutil.process_iter.

Shared by the process panel and the downloads panel so the table is scanned once per tick.
"""
from __future__ import annotations

import os
import pwd
import time
from dataclasses import dataclass, field

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
PF_KTHREAD = 0x00200000


def _mem_total() -> int:
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    return 1


MEM_TOTAL = _mem_total()


@dataclass
class Snapshot:
    ts: float
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    threads: int = 0
    by_pid: dict[int, dict] = field(default_factory=dict)


class Scanner:
    def __init__(self) -> None:
        self.prev: dict[tuple[int, int], tuple[float, int]] = {}
        self.users: dict[int, str] = {}
        self.cmd_cache: dict[int, tuple[int, str]] = {}
        self.snap: Snapshot | None = None

    def user(self, uid: int) -> str:
        name = self.users.get(uid)
        if name is None:
            try:
                name = pwd.getpwuid(uid).pw_name
            except KeyError:
                name = str(uid)
            self.users[uid] = name
        return name

    def cmdline(self, row: dict) -> str:
        """Full command line, read lazily and cached per (pid, start time)."""
        pid, start = row["pid"], row["start"]
        hit = self.cmd_cache.get(pid)
        if hit and hit[0] == start:
            return hit[1]
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            raw = b""
        cmd = raw.replace(b"\0", b" ").strip().decode(errors="replace") or f"[{row['name']}]"
        self.cmd_cache[pid] = (start, cmd)
        return cmd

    def display_name(self, row: dict) -> str:
        """comm is cut at 15 chars by the kernel; recover the full name from argv[0] when it looks truncated."""
        name = row["name"]
        if len(name) < 15 or row["kernel"]:
            return name
        full = row.get("_full")
        if full is None:
            argv0 = self.cmdline(row).split(" ", 1)[0]
            base = argv0.rsplit("/", 1)[-1]
            full = base if base.startswith(name) or name.startswith(base[:15]) and len(base) > 15 else name
            row["_full"] = full
        return full

    def snapshot(self, max_age: float = 0.5) -> Snapshot:
        now = time.monotonic()
        if self.snap and now - self.snap.ts < max_age:
            return self.snap
        return self.scan()

    def scan(self) -> Snapshot:
        now = time.monotonic()
        snap = Snapshot(ts=now)
        prev, cur = self.prev, {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{entry}/stat", "rb") as fh:
                    data = fh.read()
                uid = os.stat(f"/proc/{entry}").st_uid
            except OSError:
                continue
            lp = data.rfind(b")")
            comm = data[data.find(b"(") + 1: lp].decode(errors="replace")
            f = data[lp + 2:].split()
            try:
                state = chr(f[0][0])
                ppid, flags = int(f[1]), int(f[6])
                ticks = int(f[11]) + int(f[12])
                nthreads, start, rss = int(f[17]), int(f[19]), int(f[21]) * PAGE
            except (IndexError, ValueError):
                continue
            key = (pid, start)
            p = prev.get(key)
            cpu = 0.0
            if p and now > p[0]:
                cpu = max(0.0, (ticks - p[1]) / CLK_TCK / (now - p[0]) * 100)
            cur[key] = (now, ticks)
            row = {"pid": pid, "name": comm, "user": self.user(uid), "uid": uid, "cpu": cpu, "mem": rss / MEM_TOTAL * 100,
                   "rss": rss, "thr": nthreads, "st": state.upper(), "kernel": bool(flags & PF_KTHREAD), "ppid": ppid,
                   "start": start}
            snap.rows.append(row)
            snap.by_pid[pid] = row
            snap.total += 1
            snap.threads += nthreads
        self.prev = cur
        if len(self.cmd_cache) > 4 * max(1, snap.total):
            self.cmd_cache = {k: v for k, v in self.cmd_cache.items() if k in snap.by_pid}
        self.snap = snap
        return snap


SCANNER = Scanner()
