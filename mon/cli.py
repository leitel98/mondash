"""One entry point for every command.

Installed as `mondash`, `dlwatch`, `cpumon`, `memmon`, … — the program name picks what runs. The same
function also accepts the panel as a first argument (`mondash cpu`, `mondash dlwatch ~/Downloads`), which is
how the single-file binary offers all of them, and how `python -m mon cpu` works from a checkout.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import __version__

USAGE = """\
mondash [options]              the dashboard (see mondash --help)
mondash PANEL [options]        one monitor on its own: cpu mem net disk proc gpu temp power sys dl cmd hw gif jobs
mondash dlwatch [DIR [TOTAL]]  see what is downloading right now, or watch a folder fill up
mondash --doctor               what this machine exposes and how running programs are classified
mondash --version

Each panel is also installed under its own name: cpumon, memmon, netmon, … and dlwatch."""


def _linux_only() -> None:
    if not sys.platform.startswith("linux"):
        sys.exit("mondash reads /proc and /sys and runs on Linux only")


def main(argv: list[str] | None = None) -> None:
    _linux_only()
    argv = list(sys.argv[1:] if argv is None else argv)
    prog = Path(sys.argv[0]).name if sys.argv else ""

    from .core import panel_types
    types = panel_types()

    if argv[:1] == ["--version"] or argv[:1] == ["-V"]:
        print(f"mondash {__version__}")
        return
    if argv[:1] == ["--doctor"]:
        from .doctor import main as doctor
        doctor(argv[1:])
        return

    # `dlwatch …` or `mondash dlwatch …`
    if prog == "dlwatch" or argv[:1] == ["dlwatch"]:
        from .dlwatch import main as dlwatch_main
        dlwatch_main(argv[1:] if argv[:1] == ["dlwatch"] else argv)
        return

    # `cpumon …` (the program name says which panel) or `mondash cpu …`
    kind = None
    if prog.endswith("mon") and prog[:-3] in types:
        kind = prog[:-3]
    elif argv[:1] and argv[0] in types:
        kind, argv = argv[0], argv[1:]
    if kind:
        from .core import run_standalone
        run_standalone(kind, argv)
        return

    if argv[:1] and not argv[0].startswith("-"):
        sys.exit(f"mondash: unknown panel or command '{argv[0]}'\n\n{USAGE}")
    from .dash import main as dash_main
    dash_main(argv)


if __name__ == "__main__":
    main()
