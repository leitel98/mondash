"""Command: run any shell command on an interval and show its (ANSI-coloured) output. Lego brick for everything else."""
from __future__ import annotations

import subprocess
import threading

from rich.text import Text

from ..core import Panel


class CmdPanel(Panel):
    name = "cmd"
    title = "Command"
    interval = 5.0
    help = {"R": "run now"}
    options = {"command": "shell command to run (required)", "tail": "true/false — show the last lines instead of the first (default true)"}

    def __init__(self, cfg, theme):
        super().__init__(cfg, theme)
        self.command = self.cfg.get("command", "echo set [panels.NAME] command = \"...\"")
        self.title = self.cfg.get("title", self.command[:24])
        self.output = ""
        self.rc: int | None = None
        self.running = False
        self.lock = threading.Lock()

    def _run(self) -> None:
        try:
            r = subprocess.run(self.command, shell=True, capture_output=True, text=True, timeout=max(2.0, self.interval * 2))
            out = r.stdout if r.stdout else r.stderr
            with self.lock:
                self.output, self.rc = out, r.returncode
        except Exception as exc:
            with self.lock:
                self.output, self.rc = f"{exc!r}", -1
        finally:
            self.running = False

    def sample(self, dt: float) -> None:
        if not self.running:
            self.running = True
            threading.Thread(target=self._run, daemon=True).start()

    def on_key(self, key: str) -> bool:
        if key == "R":
            self._last = 0.0
            return True
        return False

    def status(self) -> str:
        return "" if self.rc in (None, 0) else f"exit {self.rc}"

    def render(self, width: int, height: int):
        with self.lock:
            out = self.output
        lines = out.rstrip("\n").splitlines()
        if bool(self.cfg.get("tail", True)):
            lines = lines[-height:]
        else:
            lines = lines[:height]
        text = Text.from_ansi("\n".join(lines))
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text
