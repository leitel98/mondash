#!/usr/bin/env python3
"""Headless regression check: sample every panel, render each at several sizes, compose every layout at 3 terminal sizes.

Run:  python3 -m mon.tests.render_check   (from ~/Projects/scripts). Prints 'errors: none' when all is well.
"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from rich.console import Console  # noqa: E402

from mon.core import Theme, load_config, make_panel, panel_types  # noqa: E402
from mon.dash import Dashboard  # noqa: E402


def main() -> int:
    conf = load_config()
    theme = Theme.from_dict(conf.get("theme", {}))
    panels = {}
    for name in panel_types():
        cfg = dict(conf.get("panels", {}).get(name, {}))
        if name == "cmd":
            cfg["command"] = "printf 'hello\\nworld\\n'"
        panels[name] = make_panel(name, cfg, theme)
    for _ in range(3):
        now = time.monotonic()
        for p in panels.values():
            p.tick(now)
        time.sleep(0.6)
    errors = []
    out = Console(width=140, force_terminal=True, color_system="truecolor", file=open(os.devnull, "w"))
    for name, p in panels.items():
        if p._error:
            errors.append(f"{name}: sample error {p._error}")
        for (w, h) in ((30, 6), (40, 10), (60, 14), (90, 20), (120, 30), (20, 3)):
            try:
                lines = out.render_lines(p.render(w, h), out.options.update_dimensions(w, h), pad=True, new_lines=False)
                if any(len("".join(s.text for s in ln)) > w for ln in lines):
                    errors.append(f"{name} {w}x{h}: overflow")
            except Exception:
                errors.append(f"{name} {w}x{h}: " + traceback.format_exc().splitlines()[-1])
    for size in ((120, 40), (80, 24), (200, 55)):
        for lay in conf["layouts"]:
            d = Dashboard(None, lay)
            for k in list(d.panels):
                if k in panels:
                    d.panels[k] = panels[k]
            try:
                c = Console(width=size[0], height=size[1], record=True, force_terminal=True, color_system="truecolor",
                            file=open(os.devnull, "w"))
                c.print(d.render(*size))
                lines = c.export_text().splitlines()
                if len(lines) != size[1] or any(len(l) > size[0] for l in lines):
                    errors.append(f"dash {lay} {size}: bad frame ({len(lines)} lines)")
            except Exception:
                errors.append(f"dash {lay} {size}: " + traceback.format_exc().splitlines()[-1])
    print("errors:", errors or "none")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
