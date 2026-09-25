"""Downloads: the dlwatch tracker as a panel (curl, wget, aria2c, flatpak, rsync, git, scp, pip, yarn/npm/pnpm/bun
installs, go mod download, Android sdkmanager, gradle, podman/docker pull, Steam, plus whatever `tools` names)."""
from __future__ import annotations

import os
import time

from rich.console import Group
from rich.text import Text

from ..core import Panel, bar, density, entry_rows, finished_rows, fit, human, rate, tilde
from ..dlwatch import NetMeter, Tracker, eta


class DlPanel(Panel):
    name = "dl"
    title = "Downloads"
    interval = 1.0
    help = {"c": "clear finished"}
    options = {"tools": "extra process names that count as downloaders on this machine, e.g. [\"axel\", \"lftp\"]",
               "ignore": "process names never to list"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.tracker = Tracker(tools=set(self._list("tools")), ignore=set(self._list("ignore")))
        self.net = NetMeter()

    def _list(self, key: str) -> list[str]:
        v = self.cfg.get(key) or []
        return [str(x) for x in (v if isinstance(v, list) else [v])]

    def sample(self, dt: float) -> None:
        self.net.tick(dt)
        self.tracker.scan(dt)

    def on_key(self, key: str) -> bool:
        if key == "c":
            self.tracker.finished.clear()
            return True
        return False

    def status(self) -> str:
        return f"↓ {rate(self.net.rate)} {self.net.iface}" if self.net.iface else ""

    def render(self, width: int, height: int):
        th = self.theme
        rows: list = []
        # known-size downloads first (they have bars), then unknown size, then process-only entries; fastest first
        active = sorted(self.tracker.active.values(),
                        key=lambda d: (0 if d.total else 1 if d.path else 2, -d.rate, d.display_name.lower()))
        if not active:
            rows.append(Text("nothing downloading — watching…", style=th.dim))
        n = len(active)
        fin = min(len(self.tracker.finished), 3) + 1 if self.tracker.finished else 0
        per, fin = density(n, height, fin)
        body_h = max(1, height - fin)
        shown = active if n * per <= body_h else active[: max(0, body_h // per - 1)]
        stats_w = max((len(self._stats(d).plain) for d in shown if d.path or (d.samples and d.size > 0)), default=0)
        for d in shown:
            rows.extend(self._entry(d, width, per, stats_w))
        if len(shown) < n:
            rows.append(Text(f"… +{n - len(shown)} more", style=th.dim))
        if fin and len(rows) < height:
            rows.extend(finished_rows([(w, nm, human(sz)) for w, nm, sz in self.tracker.finished], width, th, height - len(rows)))
        return Group(*rows[:height])

    def _stats(self, d) -> Text:
        th = self.theme
        stats = Text(no_wrap=True)
        if d.total:
            stats.append(f"{d.pct:3d}%", "bold")
            stats.append(f" {human(d.size)}/{human(d.total)} ")
            stats.append(rate(d.rate), th.accent)
            stats.append(" ")
            stats.append(f"ETA {eta(d.eta_seconds)}", th.warn)
        else:
            stats.append("size unknown " if d.path else "≈ " if d.approx else "", th.dim)
            stats.append(f"{human(d.size)} so far ")
            stats.append(rate(d.rate), th.accent)
        return stats

    def _entry(self, d, width: int, per: int, stats_w: int = 0) -> list:
        """1, 2 or 3 lines for one download depending on how crowded the panel is."""
        th = self.theme
        glyph = "♨ " if d.tool == "steam" else "▶ " if d.path else "● "
        if not d.path:                                                   # process only (flatpak, yarn, sdkmanager…)
            age = int(time.monotonic() - d.first_seen)
            measured = self._stats(d) if d.samples and d.size > 0 else None
            if per == 1 and measured is not None:
                return entry_rows(1, glyph, d.label, "magenta", "", measured, width, th, stats_w=stats_w)
            line = Text.assemble((glyph, "magenta"), (fit(d.label, width - 20), "magenta"), (f"  {d.tool} · {age}s", th.dim))
            if measured is None:
                return [line] + ([Text("")] if per == 3 else [])
            return [line, measured] + ([Text("")] if per == 3 else [])
        note = f" · {d.note}" if d.note else ""
        bar_fn = (lambda w: bar(d.size / d.total, w, th, palette=[th.good, th.good, th.accent])) if d.total else None
        return entry_rows(per, glyph, d.display_name, "bold", f"  {d.tool}{note}", self._stats(d), width, th,
                          bar_fn=bar_fn, stats_w=stats_w, tail_note="  → " + tilde(os.path.dirname(d.path)))
