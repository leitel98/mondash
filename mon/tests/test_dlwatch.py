"""Unit tests for the download classifier.  Run:  python3 -m unittest mon.tests.test_dlwatch"""
import unittest

from mon.dlwatch import Download, Tracker, classify, output_from_args, parse_total


def det(comm, cmd, cwd=None, downloaders=None):
    args = cmd.split(" ")[1:]
    return classify(comm, args, cwd, downloaders) if downloaders else classify(comm, args, cwd)


class Classify(unittest.TestCase):
    def test_plain_downloaders(self):
        self.assertEqual(det("curl", "curl -O https://x/y.zip")[:2], ("curl", "https://x/y.zip"))
        self.assertEqual(det("wget", "wget https://x/y")[0], "wget")
        self.assertEqual(det("flatpak", "flatpak install flathub org.gimp.GIMP")[1], "flatpak install org.gimp.GIMP")
        self.assertEqual(det("flatpak", "flatpak update -y")[1], "flatpak update (all apps)")
        self.assertEqual(det("rsync", "rsync -av a b")[1], "rsync → b")
        self.assertEqual(det("git", "git clone https://x/y")[1], "git https://x/y")
        self.assertEqual(det("yt-dlp", "yt-dlp https://v/1")[1], "yt-dlp https://v/1")

    def test_package_managers_under_node(self):
        self.assertEqual(det("node", "node /usr/lib/node_modules/yarn/bin/yarn.js install", "/p/app")[1], "yarn install · app")
        self.assertEqual(det("node", "node /usr/lib/node_modules/yarn/bin/yarn.js")[1], "yarn install")
        self.assertEqual(det("node", "node /usr/lib/node_modules/npm/bin/npm-cli.js ci")[0], "npm")
        self.assertIsNone(det("node", "node /usr/lib/node_modules/npm/bin/npm-cli.js run dev"))
        self.assertIsNone(det("node", "node server.js"))
        self.assertEqual(det("bun", "bun add react")[1], "bun add")

    def test_runtimes(self):
        self.assertEqual(det("go", "go mod download")[1], "go mod download")
        self.assertIsNone(det("go", "go build ./..."))
        self.assertEqual(det("podman", "podman pull alpine")[1], "podman pull alpine")
        self.assertIsNone(det("podman", "podman build -t x ."))
        self.assertEqual(det("uv", "uv pip install rich")[1], "uv pip install")
        self.assertEqual(det("cargo", "cargo fetch")[1], "cargo fetch")
        self.assertIsNone(det("cargo", "cargo build"))
        sdk = det("java", "java -Dcom.android.sdklib.toolsdir=/sdk/cmdline-tools/latest/bin -cp x com.android.sdklib.tool.sdkmanager.SdkManagerCli platform-tools")
        self.assertEqual(sdk[0], "sdkmanager")
        self.assertEqual(sdk[2], "/sdk/.temp")
        self.assertIsNone(det("java", "java -jar app.jar"))

    def test_user_added_tool(self):
        self.assertIsNone(det("mydl", "mydl https://x/y"))
        self.assertEqual(det("mydl", "mydl https://x/y", downloaders={"mydl"})[:2], ("mydl", "https://x/y"))

    def test_tracker_tables(self):
        t = Tracker(tools={"axel", "lftp"}, ignore={"git", "lftp"})
        self.assertIn("axel", t.downloaders)
        self.assertIn("axel", t.file_tools)               # user tools get the open-file treatment too
        self.assertNotIn("git", t.downloaders)
        self.assertNotIn("lftp", t.downloaders)
        self.assertEqual(t.comm_map["git-remote-https"[:15]], "git-remote-https")   # 15-char /proc name maps back


class Helpers(unittest.TestCase):
    def test_output_from_args(self):
        self.assertEqual(output_from_args(["-o", "a.zip", "https://x"], "curl", "/tmp"), "/tmp/a.zip")
        self.assertEqual(output_from_args(["--output=/abs/a.zip", "https://x"], "curl"), "/abs/a.zip")
        self.assertEqual(output_from_args(["-O", "https://x/f.tar.gz?x=1"], "curl"), "f.tar.gz")
        self.assertIsNone(output_from_args(["-o", "-", "https://x"], "curl"))
        self.assertIsNone(output_from_args(["https://x"], "wget"))

    def test_parse_total(self):
        self.assertEqual(parse_total(None), ("none", 0))
        self.assertEqual(parse_total("93"), ("count", 93))
        self.assertEqual(parse_total("1.5G"), ("bytes", int(1.5 * 1024 ** 3)))

    def test_display_name(self):
        d = Download(1, "curl", "x", None, "/dl/" + "a" * 64 + "--foo-1.2.tar.gz.part", 0.0)
        self.assertEqual(d.display_name, "foo-1.2.tar.gz")
        d = Download(1, "curl", "x", None, "/dl/rich--13.0.0.x86_64_linux.bottle.tar.gz", 0.0)
        self.assertEqual(d.display_name, "rich 13.0.0 (bottle)")

    def test_rate_smoothing(self):
        d = Download(1, "curl", "x", None, None, 0.0)
        d.push_size(0, 1.0)
        d.push_size(1000, 1.0)
        self.assertEqual(d.rate, 1000)
        d.push_size(2000, 1.0)
        self.assertAlmostEqual(d.rate, 1000)
        self.assertEqual(d.pct, 0)
        d.total = 4000
        self.assertEqual(d.pct, 50)


if __name__ == "__main__":
    unittest.main()
