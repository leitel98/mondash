"""Processes: task-manager style table. Sort by clicking headers or with keys, select, inspect, kill."""
from __future__ import annotations

import os
import signal
import time

import psutil
from rich.console import Group
from rich.text import Text

from ..core import Panel, duration, fit, human, pad, tilde
from ..procscan import SCANNER

SORT_KEYS = {"c": "cpu", "m": "mem", "p": "pid", "n": "name", "U": "user"}
SORT_ORDER = ["cpu", "mem", "pid", "name", "user"]


class ProcPanel(Panel):
    name = "proc"
    title = "Processes"
    interval = 2.0
    help = {"c m p n": "sort cpu/mem/pid/name", "← →": "sort column", "↑ ↓": "select", "d": "details", "/": "filter",
            "k / K": "kill / force kill", "u": "mine", "h": "kernel threads", "t": "÷ cores"}
    options = {"sort": "cpu mem pid name user", "kernel": "true/false — show kernel threads (default false)",
               "rows_cmd": "min width to show the command column (default 90)", "mine": "true/false — only this user's processes"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.sort = self.cfg.get("sort", "cpu")
        self.rows: list[dict] = []
        self.sel = 0
        self.top = 0
        self.filter = ""
        self.typing = False
        self.mine = bool(self.cfg.get("mine", False))
        self.kernel = bool(self.cfg.get("kernel", False))
        self.normalize = False
        self.details = False
        self.pending_kill: tuple[int, int, str, float] | None = None
        self.msg = ""
        self.ncpu = psutil.cpu_count() or 1
        self.total = 0
        self.threads = 0
        self._cols: list[tuple[int, int, str | None]] = []       # x0, x1, sort key — filled during render for mouse
        self._body_h = 1
        self._filt_row = 1          # 1 when the filter input box is drawn above the header (0 on tiny panels)
        self._sel_reset = False     # filter text changed: next sample selects the first match instead of following the pid
        self._detail_cache: tuple[int, float, list[Text]] | None = None
        try:
            self.me = psutil.Process().username()
        except Exception:
            self.me = None

    # sampling ----------------------------------------------------------------
    def sample(self, dt: float) -> None:
        snap = SCANNER.snapshot(max_age=min(0.5, self.interval / 2))
        rows = []
        flt = self.filter.lower()
        for r in snap.rows:
            if self.mine and r["user"] != self.me:
                continue
            if not self.kernel and r["kernel"]:
                continue
            if flt and flt not in SCANNER.display_name(r).lower() and flt != str(r["pid"]) and flt not in SCANNER.cmdline(r).lower():
                continue
            rows.append(r)
        total, threads = snap.total, snap.threads
        key = {"cpu": lambda r: -r["cpu"], "mem": lambda r: -r["mem"], "pid": lambda r: r["pid"],
               "name": lambda r: r["name"].lower(), "user": lambda r: (r["user"].lower(), -r["cpu"])}[self.sort]
        rows.sort(key=key)
        sel_pid = self.rows[self.sel]["pid"] if self.rows and self.sel < len(self.rows) else None
        self.rows, self.total, self.threads = rows, total, threads
        if self._sel_reset:
            self._sel_reset = False
            self.sel = self.top = 0
        elif sel_pid is not None:
            for n, r in enumerate(rows):
                if r["pid"] == sel_pid:
                    self.sel = n
                    break
        self.sel = max(0, min(self.sel, len(rows) - 1))
        if self.pending_kill and time.monotonic() - self.pending_kill[3] > 6:
            self.pending_kill = None

    def status(self) -> str:
        bits = [f"{len(self.rows)}/{self.total} procs", f"{self.threads} thr", f"sort {self.sort}"]
        if self.mine:
            bits.append("mine")
        if self.kernel:
            bits.append("+kernel")
        return " · ".join(bits)

    # input -------------------------------------------------------------------
    def _resample(self) -> None:
        self._last = 0.0

    def _move(self, delta: int) -> None:
        self.sel = max(0, min(len(self.rows) - 1, self.sel + delta))

    def _set_sort(self, key: str) -> None:
        self.sort = key
        self._resample()

    def on_key(self, key: str) -> bool:
        if self.typing and not self.pending_kill:
            if key in ("ENTER", "ESC"):
                self.typing = False
                if key == "ESC":
                    self.filter = ""
            elif key in ("UP", "DOWN", "PGUP", "PGDN", "HOME", "END"):
                self.typing = False                      # keep the filter, move on to the list
                return self.on_key(key)
            elif key == "BACKSPACE":
                self.filter = self.filter[:-1]
                self._sel_reset = True
            elif len(key) == 1 and key.isprintable():
                self.filter += key
                self._sel_reset = True
            else:
                return True
            self._resample()
            return True
        if self.pending_kill:
            pid, sig, name, _ = self.pending_kill
            self.pending_kill = None
            if key == "y":
                try:
                    os.kill(pid, sig)
                    self.msg = f"sent {signal.Signals(sig).name} to {pid} {name}"
                except (ProcessLookupError, PermissionError) as exc:
                    self.msg = f"kill {pid} failed: {exc.strerror}"
                self._resample()
            else:
                self.msg = "kill cancelled"
            return True
        self.msg = ""
        if key in SORT_KEYS:
            self._set_sort(SORT_KEYS[key])
        elif key in ("LEFT", "RIGHT"):
            i = SORT_ORDER.index(self.sort)
            self._set_sort(SORT_ORDER[(i + (1 if key == "RIGHT" else -1)) % len(SORT_ORDER)])
        elif key == "UP":
            self._move(-1)
        elif key == "DOWN":
            self._move(1)
        elif key == "PGUP":
            self._move(-self._body_h)
        elif key == "PGDN":
            self._move(self._body_h)
        elif key == "HOME":
            self.sel = 0
        elif key == "END":
            self.sel = max(0, len(self.rows) - 1)
        elif key == "/":
            self.typing = True
        elif key == "ESC":
            if self.details:
                self.details = False
            else:
                self.filter = ""
                self._resample()
        elif key == "d":
            self.details = not self.details
        elif key == "u":
            self.mine = not self.mine
            self._resample()
        elif key == "h":
            self.kernel = not self.kernel
            self._resample()
        elif key == "t":
            self.normalize = not self.normalize
        elif key in ("k", "K") and self.rows:
            r = self.rows[self.sel]
            self.pending_kill = (r["pid"], signal.SIGKILL if key == "K" else signal.SIGTERM, r["name"], time.monotonic())
        else:
            return False
        return True

    def on_mouse(self, kind: str, x: int, y: int) -> bool:
        if kind == "WHEELUP":
            self._move(-3)
        elif kind == "WHEELDOWN":
            self._move(3)
        elif kind in ("CLICK", "RCLICK"):
            off = self._filt_row
            if y == 0 and off:                           # filter box → start typing
                self.typing = True
            elif y == off:                               # header → sort
                self.typing = False
                for x0, x1, key in self._cols:
                    if key and x0 <= x < x1:
                        self._set_sort(key)
                        break
            elif off + 1 <= y <= off + self._body_h:
                self.typing = False                      # keep the filter, act on the clicked row
                n = self.top + y - off - 1
                if n < len(self.rows):
                    if n == self.sel and kind == "CLICK":
                        self.details = not self.details   # click the selected row again → details
                    self.sel = n
                    if kind == "RCLICK":
                        r = self.rows[n]
                        self.pending_kill = (r["pid"], signal.SIGTERM, r["name"], time.monotonic())
        else:
            return False
        return True

    # details -----------------------------------------------------------------
    def _details(self, pid: int, width: int) -> list[Text]:
        th = self.theme
        now = time.monotonic()
        if self._detail_cache and self._detail_cache[0] == pid and now - self._detail_cache[1] < 1.0:
            return self._detail_cache[2]
        out: list[Text] = []
        try:
            p = psutil.Process(pid)
            with p.oneshot():
                started = time.strftime("%H:%M:%S", time.localtime(p.create_time()))
                age = duration(time.time() - p.create_time())
                cpu_t = p.cpu_times()
                facts = [("started", f"{started} ({age} ago)"), ("cpu time", duration(cpu_t.user + cpu_t.system)),
                         ("nice", str(p.nice())), ("threads", str(p.num_threads()))]
                try:
                    facts.append(("fds", str(p.num_fds())))
                except psutil.Error:
                    pass
                try:
                    io = p.io_counters()
                    facts.append(("read", human(io.read_bytes)))
                    facts.append(("written", human(io.write_bytes)))
                except psutil.Error:
                    pass
                try:
                    facts.append(("cwd", tilde(p.cwd())))
                except psutil.Error:
                    pass
                try:
                    parent = p.parent()
                    if parent:
                        facts.append(("parent", f"{parent.pid} {parent.name()}"))
                except psutil.Error:
                    pass
                line = Text(no_wrap=True)
                for i, (k, v) in enumerate(facts):
                    if len(line.plain) + len(k) + len(v) + 4 > width:
                        break
                    if i:
                        line.append("  ")
                    line.append(k + " ", th.dim)
                    line.append(v, th.accent)
                out.append(line)
                cmd = " ".join(p.cmdline()) or p.name()
                out.append(Text(fit(cmd, width), style=th.dim))
        except psutil.Error as exc:
            out.append(Text(f"details unavailable: {exc}", style=th.dim))
        self._detail_cache = (pid, now, out)
        return out

    # render ------------------------------------------------------------------
    def _filter_box(self, width: int) -> Text:
        th = self.theme
        box = Text(no_wrap=True, overflow="ellipsis")
        box.append("⌕ ", th.accent if (self.typing or self.filter) else th.dim)
        if self.typing:
            box.append(self.filter, "bold")
            box.append("▏", th.accent)
            box.append("  Enter keeps · Esc clears", th.dim)
        elif self.filter:
            box.append(self.filter, f"bold {th.accent}")
            box.append(f"  {len(self.rows)} match · / edits · Esc clears", th.dim)
        else:
            box.append("filter: press / or click here to type", th.dim)
        box.truncate(width, overflow="ellipsis")
        return box

    def render(self, width: int, height: int):
        th = self.theme
        out: list = []
        show_cmd = width >= int(self.cfg.get("rows_cmd", 90))
        show_user = width >= 60
        show_thr = width >= 70
        fixed = 7 + 1 + (11 if show_user else 0) + 7 + 7 + 8 + (5 if show_thr else 0) + 2
        name_w = max(8, min(28, width - fixed) if show_cmd else width - fixed)
        cmd_w = max(0, width - fixed - name_w - 1) if show_cmd else 0

        cols: list[tuple[int, int, str | None]] = []
        head = Text(no_wrap=True)

        def col(label: str, w: int, align: str = "left", key: str | None = None) -> None:
            x0 = len(head.plain)
            active = key is not None and key == self.sort
            text = label + ("▼" if active else "")
            if active and align == "right":
                text = text[: w]
            head.append(pad(text, w, align), style=f"bold {th.accent}" if active else f"bold {th.dim}")
            cols.append((x0, x0 + w + 1, key))
            head.append(" ")

        col("PID", 7, "right", "pid")
        if show_user:
            col("USER", 10, "left", "user")
        col("NAME", name_w, "left", "name")
        col("CPU%", 6, "right", "cpu")
        col("MEM%", 6, "right", "mem")
        col("RSS", 7, "right")
        if show_thr:
            col("THR", 4, "right")
        col("S", 1)
        if show_cmd:
            col("COMMAND", cmd_w)
        head.rstrip()
        self._cols = cols
        # filter input box: always visible so it can be found and clicked; typing goes into it after `/` or a click
        self._filt_row = 1 if (height >= 4 or self.typing or self.filter) else 0
        if self._filt_row:
            out.append(self._filter_box(width))
        out.append(head)

        footer: list[Text] = []
        if self.pending_kill:
            pid, sig, name, _ = self.pending_kill
            footer.append(Text(f" send {signal.Signals(sig).name} to {pid} {name}?  y = yes, any other key = no ", style=f"bold {th.bad}"))
        elif self.msg:
            footer.append(Text(self.msg, style=th.warn))
        if self.details and self.rows and height >= 6:
            footer = self._details(self.rows[self.sel]["pid"], width) + footer

        body_h = max(1, height - 1 - self._filt_row - len(footer))
        self._body_h = body_h
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + body_h:
            self.top = self.sel - body_h + 1
        self.top = max(0, min(self.top, max(0, len(self.rows) - body_h)))
        for n in range(self.top, min(len(self.rows), self.top + body_h)):
            r = self.rows[n]
            cpu = r["cpu"] / self.ncpu if self.normalize else r["cpu"]
            selected = n == self.sel
            line = Text(no_wrap=True, style="reverse" if selected else "")
            dim = th.dim if not selected else ""
            line.append(pad(str(r["pid"]), 7, "right"), dim)
            line.append(" ")
            if show_user:
                line.append(pad(r["user"], 10), dim if r["user"] != self.me or selected else "")
                line.append(" ")
            line.append(pad(SCANNER.display_name(r), name_w), "bold" if selected else "")
            line.append(" ")
            line.append(pad(f"{cpu:5.1f}", 6, "right"), th.level(min(100, cpu)) if not selected else "")
            line.append(" ")
            line.append(pad(f"{r['mem']:5.1f}", 6, "right"), th.level(r["mem"] * 2) if not selected else "")
            line.append(" ")
            line.append(pad(human(r["rss"]), 7, "right"))
            if show_thr:
                line.append(" ")
                line.append(pad(str(r["thr"]), 4, "right"), dim)
            line.append(" ")
            line.append(r["st"], {"R": th.good, "D": th.bad, "Z": th.bad, "T": th.warn}.get(r["st"], th.dim) if not selected else "")
            if show_cmd:
                line.append(" ")
                line.append(fit(SCANNER.cmdline(r), cmd_w), dim)
            out.append(line)
        if not self.rows:
            out.append(Text("no matching processes", style=th.dim))
        out.extend(footer)
        return Group(*out)
