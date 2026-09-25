"""The single entry point dispatches on program name and first argument; config tables reach the trackers."""
import contextlib
import io
import sys
import unittest
from unittest import mock

from mon import __version__, cli
from mon.jobs import JobTracker, classify


class Dispatch(unittest.TestCase):
    def run_cli(self, prog, argv):
        with mock.patch.object(sys, "argv", [prog] + argv):
            with mock.patch("mon.core.run_standalone") as standalone, mock.patch("mon.dlwatch.main") as dlw, \
                    mock.patch("mon.dash.main") as dash:
                cli.main(argv)
                return standalone, dlw, dash

    def test_program_name_picks_the_panel(self):
        standalone, dlw, dash = self.run_cli("cpumon", ["-i", "2"])
        standalone.assert_called_once_with("cpu", ["-i", "2"])
        dlw.assert_not_called()
        dash.assert_not_called()

    def test_first_argument_picks_the_panel(self):
        standalone, _, _ = self.run_cli("mondash", ["jobs", "--bare"])
        standalone.assert_called_once_with("jobs", ["--bare"])

    def test_dlwatch_by_name_and_by_argument(self):
        _, dlw, _ = self.run_cli("dlwatch", ["~/Downloads", "4G"])
        dlw.assert_called_once_with(["~/Downloads", "4G"])
        _, dlw, _ = self.run_cli("mondash", ["dlwatch", "-w"])
        dlw.assert_called_once_with(["-w"])

    def test_plain_mondash_is_the_dashboard(self):
        _, _, dash = self.run_cli("mondash", ["-l", "wide"])
        dash.assert_called_once_with(["-l", "wide"])

    def test_unknown_panel_exits(self):
        with self.assertRaises(SystemExit):
            self.run_cli("mondash", ["nope"])

    def test_version(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch.object(sys, "argv", ["mondash"]):
            cli.main(["--version"])
        self.assertEqual(buf.getvalue().strip(), f"mondash {__version__}")


class ConfigTables(unittest.TestCase):
    def test_user_tools_become_jobs(self):
        self.assertIsNone(classify("myencoder", ["-i", "a"]))
        t = JobTracker(tools={"myencoder": "media", "weird": "not-a-kind"}, ignore={"ffmpeg"})
        self.assertEqual(classify("myencoder", ["-i", "a"], t.finite).kind, "media")
        self.assertEqual(classify("weird", [], t.finite).kind, "script")       # unknown kind falls back to script
        self.assertIsNone(classify("ffmpeg", ["-i", "a"], t.finite))           # ignored, although built in
        self.assertNotIn("ffmpeg", t.known)


if __name__ == "__main__":
    unittest.main()
