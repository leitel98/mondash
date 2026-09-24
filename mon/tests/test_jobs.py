"""Unit tests for the job classifier and history.  Run:  python3 -m unittest mon.tests.test_jobs"""
import tempfile
import unittest
from pathlib import Path

from mon.jobs import History, classify


class Classify(unittest.TestCase):
    def det(self, comm, cmd):
        return classify(comm, cmd.split(" ")[1:])

    def test_gradle_tree(self):
        client = self.det("java", "java -Xmx64m -Dorg.gradle.appname=gradlew -jar gradle-wrapper.jar app:assembleDebug -x lint -x test -PreactNativeArchitectures=x86_64 --console plain")
        self.assertEqual((client.kind, client.finite, client.label), ("gradle", True, "gradle app:assembleDebug"))
        self.assertEqual(self.det("java", "java -Dorg.gradle.appname=gradlew -jar w.jar").label, "gradle")
        daemon = self.det("java", "java --add-opens=x -cp gradle-daemon-main.jar org.gradle.launcher.daemon.bootstrap.GradleDaemon 9.3.1")
        self.assertEqual((daemon.kind, daemon.finite, daemon.stage), ("gradle", False, "gradle"))
        kotlin = self.det("java", "java -cp kotlin-compiler.jar org.jetbrains.kotlin.daemon.KotlinCompileDaemon")
        self.assertEqual((kotlin.finite, kotlin.stage), (False, "kotlin"))

    def test_sdkmanager_is_a_download_not_a_job(self):
        self.assertIsNone(self.det("java", "java -Dcom.android.sdklib.toolsdir=/sdk/cmdline-tools/latest/bin -cp x com.android.sdklib.tool.sdkmanager.SdkManagerCli platform-tools"))

    def test_expo(self):
        run = self.det("node", "node /p/node_modules/.bin/expo run:android --no-bundler --device Pixel")
        self.assertEqual((run.kind, run.label, run.finite), ("expo", "expo run:android", True))
        start = self.det("node", "node /p/node_modules/.bin/expo start")
        self.assertEqual((start.finite, start.stage), (False, "metro"))
        self.assertIsNone(self.det("node", "node /p/node_modules/.bin/expo --version"))

    def test_package_managers(self):
        self.assertIsNone(self.det("node", "node /usr/lib/node_modules/yarn/bin/yarn.js install"))
        self.assertIsNone(self.det("node", "node /usr/lib/node_modules/yarn/bin/yarn.js"))
        self.assertIsNone(self.det("node", "node /usr/lib/node_modules/npm/bin/npm-cli.js exec expo run:android"))
        build = self.det("node", "node /usr/lib/node_modules/yarn/bin/yarn.js build")
        self.assertEqual((build.label, build.finite), ("yarn build", True))
        dev = self.det("node", "node /usr/lib/node_modules/npm/bin/npm-cli.js run dev")
        self.assertEqual((dev.label, dev.finite), ("npm dev", False))

    def test_node_tools(self):
        tsc = self.det("node", "node /p/node_modules/typescript/bin/tsc --noEmit")
        self.assertEqual((tsc.kind, tsc.finite), ("compile", True))
        watch = self.det("node", "node /p/node_modules/typescript/bin/tsc --watch")
        self.assertFalse(watch.finite)
        jest = self.det("node", "node /p/node_modules/jest/bin/jest.js --ci")
        self.assertEqual(jest.kind, "test")
        vite = self.det("node", "node /p/node_modules/vite/bin/vite.js build")
        self.assertEqual((vite.label, vite.finite), ("vite build", True))
        generic = self.det("node", "node /p/node_modules/@nx/cli/bin/cli.js build")
        self.assertIsNone(generic)                       # scoped generic entry → package 'cli', unknown
        eslint = self.det("node", "node /p/node_modules/eslint/bin/eslint.js src")
        self.assertEqual(eslint.kind, "lint")

    def test_plain_tools(self):
        self.assertEqual(self.det("cc1plus", "cc1plus -quiet x.cpp").kind, "compile")
        self.assertEqual(self.det("ffmpeg", "ffmpeg -i a.mkv b.mp4").kind, "media")
        self.assertEqual(self.det("tar", "tar xf big.tar").kind, "archive")
        self.assertEqual(self.det("rpm-ostree", "rpm-ostree upgrade").label, "rpm-ostree upgrade")
        self.assertIsNone(self.det("dnf", "dnf search vim"))
        self.assertEqual(self.det("dnf", "dnf install vim").label, "dnf install")
        self.assertEqual(self.det("cargo", "cargo build --release").label, "cargo build")
        self.assertFalse(self.det("cargo", "cargo watch -x run").finite)
        self.assertIsNone(self.det("cargo", "cargo --version"))
        self.assertEqual(self.det("go", "go build ./...").kind, "compile")
        self.assertIsNone(self.det("go", "go mod download"))
        self.assertIsNone(self.det("git", "git clone https://x/y"))
        self.assertEqual(self.det("git", "git gc --aggressive").kind, "vcs")
        self.assertEqual(self.det("podman", "podman build -t x .").label, "podman build")
        self.assertIsNone(self.det("podman", "podman pull alpine"))

    def test_python(self):
        self.assertEqual(self.det("python3", "python3 -m pytest tests").kind, "test")
        self.assertEqual(self.det("pytest", "pytest tests").kind, "test")
        self.assertIsNone(self.det("python3", "python3 -m pip install rich"))
        self.assertIsNone(self.det("python3", "python3 script.py"))
        self.assertEqual(self.det("python3", "python3 -m sphinx.cmd.build docs out").kind, "build")

    def test_rewritten_titles(self):
        self.assertIsNone(self.det("npm", "npm exec tsc --noEmit"))
        run = self.det("npm", "npm run build")
        self.assertEqual((run.label, run.finite), ("npm build", True))
        self.assertIsNone(self.det("yarn", "yarn install"))
        self.assertEqual(self.det("yarn", "yarn build").label, "yarn build")
        self.assertEqual(self.det("bun", "bun test").label, "bun test")
        self.assertEqual(self.det("deno", "deno compile main.ts").kind, "compile")

    def test_not_jobs(self):
        self.assertIsNone(self.det("bash", "bash -c ls"))
        self.assertIsNone(self.det("node", "node server.js"))
        self.assertIsNone(self.det("java", "java -jar app.jar"))
        self.assertIsNone(self.det("curl", "curl -O https://x/y"))


class HistoryTest(unittest.TestCase):
    def test_roundtrip_and_median(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "jobs.json"
            h = History(path)
            self.assertEqual(h.estimate("k"), (None, 0))
            h.record("k", 3.0)                           # below MIN_SECONDS: ignored
            for secs in (100, 120, 90, 300):
                h.record("k", secs)
            h.save()
            h2 = History(path)
            median, runs = h2.estimate("k")
            self.assertEqual((median, runs), (110.0, 4))
            for secs in range(10):
                h2.record("k", 60 + secs)
            self.assertEqual(len(h2.data["k"]), History.KEEP)


if __name__ == "__main__":
    unittest.main()
