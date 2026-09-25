"""Jobs: everything being processed right now — builds, compilers, bundlers, tests, encoders, archivers, copies,
package/system installs — one row per process tree with stage, CPU, memory, IO and an estimated bar/ETA from the
last runs of the same job. Downloads stay in the `dl` panel."""
from __future__ import annotations

import time

from rich.console import Group
from rich.text import Text

from ..core import Panel, bar, density, duration, entry_rows, finished_rows, fit, human, rate
from ..jobs import Job, JobTracker

GLYPH = {"gradle": "⚙", "expo": "⚙", "compile": "⚒", "link": "⚒", "build": "⚙", "script": "▶", "test": "✓", "lint": "✓",
         "container": "▣", "media": "♫", "archive": "▤", "copy": "⇄", "backup": "⇄", "system": "⬢", "python": "⚒", "vcs": "⑂",
         "ml": "∴", "db": "▥", "vm": "▣", "iac": "⬢"}


class JobsPanel(Panel):
    name = "jobs"
    title = "Jobs"
    interval = 1.0
    help = {"b": "busy list", "c": "clear finished"}
    options = {"busy": "true/false — also list processes burning CPU that are not a known job (default true)",
               "busy_cpu": "CPU % a process must hold to count as busy (default 40)",
               "busy_after": "seconds it must hold it (default 8)",
               "busy_ignore": "extra process names to never list as busy",
               "tools": "table of extra programs that are jobs on this machine: process name → kind "
                        "(compile link build test container media archive copy backup system python vcs ml db vm iac script)",
               "ignore": "process names never to count as a job",
               "finished": "how many finished jobs to keep on screen (default 4)"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        tools = self.cfg.get("tools") or {}
        self.tracker = JobTracker(busy=bool(self.cfg.get("busy", True)), busy_cpu=float(self.cfg.get("busy_cpu", 40)),
                                  busy_after=float(self.cfg.get("busy_after", 8)), busy_ignore=set(self._list("busy_ignore")),
                                  tools={str(k): str(v) for k, v in tools.items()} if isinstance(tools, dict) else {},
                                  ignore=set(self._list("ignore")))
        self.show_busy = bool(self.cfg.get("busy", True))
        self.keep_finished = int(self.cfg.get("finished", 4))
        self._sig: tuple = ()

    def _list(self, key: str) -> list[str]:
        v = self.cfg.get(key) or []
        return [str(x) for x in (v if isinstance(v, list) else [v])]

    def sample(self, dt: float):
        self.tracker.scan(dt)
        sig = (tuple((j.key, round(j.cpu), int(j.elapsed), j.stage, j.rss >> 24) for j in self.tracker.sorted_jobs()),
               tuple((b.pid, round(b.cpu)) for b in self.tracker.busy) if self.show_busy else (),
               len(self.tracker.finished), time.strftime("%M") if not self.tracker.active else "")
        # the sweep bar animates only while a job without history is on screen
        if any(j.frac is None for j in self.tracker.sorted_jobs()):
            sig = (*sig, int(time.monotonic() * 2))
        if sig == self._sig:
            return False
        self._sig = sig

    def on_key(self, key: str) -> bool:
        if key == "b":
            self.show_busy = not self.show_busy
            self.tracker.busy_on = self.show_busy
            return True
        if key == "c":
            self.tracker.finished.clear()
            return True
        return False

    def status(self) -> str:
        jobs = self.tracker.sorted_jobs()
        n = len(jobs)
        cpu = sum(j.cpu for j in jobs)
        if not n:
            return ""
        return f"{n} running · {cpu:.0f}% cpu"

    # -- render -----------------------------------------------------------------------------------------------
    def render(self, width: int, height: int):
        th = self.theme
        jobs = self.tracker.sorted_jobs()
        busy = self.tracker.busy if self.show_busy else []
        finished = list(self.tracker.finished)[: self.keep_finished]
        rows: list = []
        n = len(jobs)
        if not n:
            rows.append(Text("nothing being processed — watching…" if not busy else "no known job running", style=th.dim))
        # extra sections take space only when the jobs fit comfortably
        extra = 0
        if busy:
            extra += 1 + min(len(busy), 3)
        if finished:
            extra += 1 + len(finished)
        per, extra = density(n, height, extra)
        body_h = max(1, height - extra)
        shown = jobs if n * per <= body_h else jobs[: max(0, body_h // per - 1)]
        stats_w = max((len(self._stats(j).plain) for j in shown), default=0)
        for j in shown:
            rows.extend(self._entry(j, width, per, stats_w))
        if len(shown) < n:
            rows.append(Text(f"… +{n - len(shown)} more", style=th.dim))
        if busy and extra:
            rows.append(Text("busy:", style=th.dim))
            for b in busy[:3]:
                age = duration(time.time() - b.since)
                line = Text.assemble(("● ", th.warn), (fit(b.name, max(6, width - 30)), ""),
                                     (f"  {b.cpu:.0f}%", th.level(min(100.0, b.cpu / 2))), (f" {human(b.rss)}", th.dim), (f"  {age}", th.dim))
                line.no_wrap = True
                line.overflow = "ellipsis"
                rows.append(line)
        if finished and extra and len(rows) < height:
            rows.extend(finished_rows([(w, nm, duration(secs)) for w, nm, secs, _ in finished], width, th, height - len(rows)))
        return Group(*rows[:height])

    def _stats(self, j: Job) -> Text:
        th = self.theme
        s = Text(no_wrap=True)
        frac = j.frac
        if frac is not None:
            over = j.eta is not None and j.eta < 0
            s.append(f"≈{int(frac * 100):2d}%", "bold")
            s.append(" ")
            s.append(f"+{duration(-j.eta)} over" if over else f"ETA {duration(j.eta)}", th.warn if not over else th.bad)
        else:
            s.append(duration(j.elapsed), "bold")
            s.append(" first run", th.dim)
        s.append(f"  {j.cpu:.0f}%", th.accent)
        s.append(f" {human(j.rss)}", th.dim)
        io = []
        if j.read_rate >= 200 * 1024:
            io.append(f"r {rate(j.read_rate)}")
        if j.write_rate >= 200 * 1024:
            io.append(f"w {rate(j.write_rate)}")
        if io:
            s.append("  " + " ".join(io), th.dim)
        return s

    def _entry(self, j: Job, width: int, per: int, stats_w: int = 0) -> list:
        th = self.theme
        glyph = GLYPH.get(j.kind, "●") + " "
        stage = f" · {j.stage}" if j.stage and j.stage != j.label.split(" ")[0] else ""
        name_style = "bold" if j.finite else "magenta"
        if per == 1:
            return entry_rows(1, glyph, j.name + stage, name_style, "", self._stats(j), width, th,
                              bar_fn=lambda w: self._bar(j, w), stats_w=stats_w)
        tail = ""
        if j.est_runs:
            tail = f"  · median of {j.est_runs} run{'s' if j.est_runs > 1 else ''}: {duration(j.est_total)}"
        elif j.frac is None and j.members:
            tail = f"  · {len(j.members)} processes"
        rows = entry_rows(per, glyph, j.name, name_style, f"  {j.kind} · {duration(j.elapsed)}", self._stats(j), width, th,
                          bar_fn=lambda w: self._bar(j, w), tail_note=tail)
        if stage:                                        # the stage word sits between the name and the note, in accent
            note = f"  {j.kind} · {duration(j.elapsed)}"
            head = Text.assemble((glyph, name_style), (fit(j.name, max(6, width - len(stage) - len(note) - 2)), name_style),
                                 (stage, th.accent), (note, th.dim))
            head.no_wrap = True
            rows[0] = head
        return rows

    def _bar(self, j: Job, width: int) -> Text:
        """Estimated progress bar when history exists; otherwise a slow sweep so the eye still sees motion."""
        th = self.theme
        frac = j.frac
        if frac is not None:
            palette = [th.good, th.good, th.accent] if not (j.eta is not None and j.eta < 0) else [th.warn, th.warn, th.bad]
            return bar(frac, width, th, palette=palette)
        width = max(1, width)
        pos = int(time.monotonic() * 2) % max(1, width)
        head = max(1, width // 8)
        out = Text(no_wrap=True)
        for i in range(width):
            d = (i - pos) % width
            out.append(th.bar_fill if d < head else th.bar_empty, th.accent if d < head else th.bar_empty_style)
        return out
