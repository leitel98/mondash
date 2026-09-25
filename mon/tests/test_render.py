"""Headless regression check: sample every panel, render each at several sizes, compose every layout at 3 terminal
sizes, all against the default config (the user's dash.toml is not touched).

Run:  python3 -m unittest mon.tests.test_render
"""
import os
import time
import traceback
import unittest

from rich.console import Console

from mon.core import Theme, default_config, make_panel, panel_types
from mon.dash import Dashboard

SIZES = ((30, 6), (40, 10), (60, 14), (90, 20), (120, 30), (20, 3))
TERMINALS = ((120, 40), (80, 24), (200, 55))


def build_panels(conf):
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
    return panels


class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conf = default_config()
        cls.panels = build_panels(cls.conf)
        cls.sink = open(os.devnull, "w")
        cls.out = Console(width=140, force_terminal=True, color_system="truecolor", file=cls.sink)

    @classmethod
    def tearDownClass(cls):
        for p in cls.panels.values():
            p.close()
        cls.sink.close()

    def test_panels_sample_without_error(self):
        errors = [f"{name}: {p._error}" for name, p in self.panels.items() if p._error]
        self.assertEqual(errors, [])

    def test_panels_fit_their_box(self):
        errors = []
        for name, p in self.panels.items():
            for (w, h) in SIZES:
                try:
                    lines = self.out.render_lines(p.render(w, h), self.out.options.update_dimensions(w, h), pad=True, new_lines=False)
                    if any(len("".join(s.text for s in ln)) > w for ln in lines):
                        errors.append(f"{name} {w}x{h}: overflow")
                except Exception:
                    errors.append(f"{name} {w}x{h}: " + traceback.format_exc().splitlines()[-1])
        self.assertEqual(errors, [])

    def test_layouts_compose(self):
        errors = []
        for size in TERMINALS:
            for lay in self.conf["layouts"]:
                d = Dashboard.from_config(self.conf, lay)
                for k in list(d.panels):
                    if k in self.panels:
                        d.panels[k] = self.panels[k]
                try:
                    c = Console(width=size[0], height=size[1], record=True, force_terminal=True, color_system="truecolor",
                                file=self.sink)
                    c.print(d.render(*size))
                    lines = c.export_text().splitlines()
                    if len(lines) != size[1] or any(len(ln) > size[0] for ln in lines):
                        errors.append(f"dash {lay} {size}: bad frame ({len(lines)} lines)")
                except Exception:
                    errors.append(f"dash {lay} {size}: " + traceback.format_exc().splitlines()[-1])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
