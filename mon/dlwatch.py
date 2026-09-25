"""dlwatch — see everything that is downloading right now, with a live bar for each.

Usage:
  dlwatch                 auto mode: finds every download in progress (curl, wget,
                          aria2c, flatpak, rsync, git, scp, pip, Steam, plus yarn/npm/pnpm/bun
                          installs, go mod download, Android sdkmanager, gradle, podman/docker
                          pull) and shows a live bar
  dlwatch -w              same, in its own terminal window
  dlwatch DIR [TOTAL]     folder mode: watch a folder fill up. TOTAL is a file count
                          (93) or a size (4.7G). Options: -p '*.zip'  -l file.log

Keys: q quits.

The tracking logic here is also what the dashboard's `dl` panel shows. Extra downloaders can be named in
dash.toml: [panels.dl] tools = ["axel", "lftp"], and `ignore` hides ones you never want listed.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from rich.console import Console, Group
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .core import Keys, duration, human, relaunch_in_window, self_argv
from .host import HOME, default_iface, tilde
from .procscan import SCANNER

# --------------------------------------------------------------------------- what counts as a download

DOWNLOADERS = {"curl", "wget", "wget2", "aria2c", "axel", "rsync", "flatpak", "git", "git-remote-https",   # /proc shows both
               "scp", "sftp", "pip", "pip3", "lftp", "snap", "megadl", "gallery-dl", "youtube-dl", "yt-dlp"}
FILE_TOOLS = {"curl", "wget", "wget2", "aria2c", "axel", "lftp", "megadl"}     # tools where one output file can be tracked
# Runtimes whose process name says nothing: the tool is recognised from the command line instead
# (yarn/npm/pnpm under node, sdkmanager/gradle under java, go mod download, podman/docker pull).
CMDLINE_TOOLS = {"node", "java", "go", "podman", "docker", "bun", "deno", "cargo", "uv", "pipx", "conda", "mamba"}
PKG_VERBS = {"install", "i", "ci", "add", "update", "up", "upgrade", "install-test", "it", "sync", "pull"}
URL_RE = re.compile(r"(?:https?|ftp)://[^\s\"']+")
APPID_RE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_-]+){2,}")
SKIP_PREFIX = ("/dev/", "/proc/", "/sys/", "/run/")
STEAM_ROOTS = ("~/.local/share/Steam", "~/.steam/steam", "~/.steam/root",
               "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",          # Flatpak
               "~/snap/steam/common/.local/share/Steam")                          # Snap


def eta(seconds: float | None) -> str:
    return "--" if seconds is None or seconds <= 0 else duration(seconds)


def shorten(path: str, width: int) -> str:
    p = tilde(path)
    if len(p) <= width:
        return p
    half = max(4, width // 2 - 1)
    return p[:half] + "…" + p[-half:]


def parse_total(text: str | None) -> tuple[str, int]:
    """'93' -> ('count', 93);  '4.7G' -> ('bytes', N);  None -> ('none', 0)"""
    if not text:
        return "none", 0
    if text.isdigit():
        return "count", int(text)
    m = re.fullmatch(r"(\d+\.?\d*)([KkMmGgTt])[Bb]?", text)
    if not m:
        sys.exit(f"dlwatch: bad TOTAL '{text}' (use 93, 800M, 4.2G)")
    mult = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[m.group(2).lower()]
    return "bytes", int(float(m.group(1)) * mult)


class NetMeter:
    def __init__(self) -> None:
        self.iface = default_iface()
        self.prev = self._read()
        self.rate = 0.0

    def _read(self) -> int:
        counters = psutil.net_io_counters(pernic=True)
        return counters[self.iface].bytes_recv if self.iface in counters else 0

    def tick(self, dt: float) -> float:
        now = self._read()
        self.rate = max(0.0, (now - self.prev) / dt)
        self.prev = now
        return self.rate


def wait_or_quit(keys: Keys, seconds: float) -> bool:
    """Sleep `seconds`, returning True early if 'q' was pressed."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if keys.poll(0.1) == "q":
            return True
    return False


# --------------------------------------------------------------------------- auto mode


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects ourselves so a HEAD that turns into a GET on redirect still stays a HEAD."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = urllib.request.Request(newurl, method="HEAD", headers=dict(req.headers))
        return new


class SizeProbe:
    """Asks each URL once for Content-Length, in the background (standard library only)."""

    RETRY_AFTER = 30.0          # a failed probe (0) is retried after this many seconds
    TIMEOUT = 12

    def __init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.results: dict[str, int] = {}
        self.failed_at: dict[str, float] = {}
        self.pending: dict[str, object] = {}

    def get(self, url: str) -> int:
        if url in self.results:
            if self.results[url] or time.monotonic() - self.failed_at.get(url, 0) < self.RETRY_AFTER:
                return self.results[url]
            del self.results[url]                        # stale failure: probe again
        fut = self.pending.get(url)
        if fut is None:
            self.pending[url] = self.pool.submit(self.head, url)
        elif fut.done():
            self.results[url] = fut.result()
            if not self.results[url]:
                self.failed_at[url] = time.monotonic()
            del self.pending[url]
            return self.results[url]
        return 0

    @classmethod
    def _head(cls, url: str, headers: dict) -> tuple[int, dict, int]:
        """(status, headers, content-length) for a HEAD request, redirects followed, errors returned not raised."""
        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        try:
            with opener.open(req, timeout=cls.TIMEOUT) as r:
                return r.status, dict(r.headers), int(r.headers.get("Content-Length") or 0)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), 0

    @classmethod
    def head(cls, url: str) -> int:
        try:
            headers = {"User-Agent": "dlwatch-probe"}
            status, hdrs, length = cls._head(url, headers)
            auth = {k.lower(): v for k, v in hdrs.items()}.get("www-authenticate", "")
            if status == 401 and "bearer" in auth.lower():
                # OCI registry (Homebrew bottles on ghcr.io etc.): fetch an anonymous pull token and retry
                fields = dict(re.findall(r'(\w+)="([^"]*)"', auth))
                if fields.get("realm"):
                    q = urllib.parse.urlencode({k: v for k, v in fields.items() if k in ("service", "scope")})
                    req = urllib.request.Request(fields["realm"] + ("?" + q if q else ""), headers=headers)
                    with urllib.request.urlopen(req, timeout=cls.TIMEOUT) as r:
                        import json
                        tok = json.loads(r.read().decode() or "{}")
                    token = tok.get("token") or tok.get("access_token")
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                        status, hdrs, length = cls._head(url, headers)
            if status >= 400:              # error bodies are tiny JSON, not the file
                return 0
            return length
        except Exception:
            return 0


@dataclass
class Download:
    pid: int
    tool: str
    label: str
    url: str | None
    path: str | None
    first_seen: float
    fd: int | None = None
    size: int = 0
    total: int = 0
    rate: float = 0.0
    samples: int = 0
    note: str = ""                       # extra status text (Steam: staging progress, paused)
    last_change: float = 0.0
    last_walk: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=64))
    reported: str = ""
    staging: str | None = None           # directory whose growth is the download (sdkmanager .temp)
    approx: bool = False                 # size comes from the process read counter, not a file
    io_base: int = -1

    @property
    def name(self) -> str:
        if self.tool == "steam":
            return self.label
        return os.path.basename(self.path) if self.path else self.label

    @property
    def display_name(self) -> str:
        """Human name: drops Homebrew's sha256-- prefix and partial-file suffixes."""
        n = self.name
        m = re.match(r"^[0-9a-f]{40,64}--(.+)$", n)
        if m:
            n = m.group(1)
        for suf in (".incomplete", ".part", ".crdownload", ".download", ".partial", ".tmp", ".aria2"):
            if n.endswith(suf) and len(n) > len(suf):
                n = n[: -len(suf)]
        m = re.match(r"^(.+?)--(.+?)\.[a-z0-9_]+_(?:linux|darwin|sonoma|sequoia|ventura|monterey)\.bottle(?:\.\d+)?\.tar\.gz$", n)
        if m:
            n = f"{m.group(1)} {m.group(2)} (bottle)"
        return n

    @property
    def pct(self) -> int:
        return min(100, self.size * 100 // self.total) if self.total else 0

    def push_size(self, size: int, dt: float) -> None:
        """Feed a new size observation into the smoothed rate."""
        delta = max(0, size - self.size) / dt
        if self.samples == 0:
            self.rate = 0.0                       # first look: no speed yet
        elif self.samples == 1:
            self.rate = delta                     # second look: raw speed
        else:
            self.rate = self.rate * 0.7 + delta * 0.3   # then smoothed
        self.size = size
        self.samples += 1

    def update_size(self, dt: float) -> None:
        if not self.path and self.fd is None:
            return
        size = None
        if self.fd is not None:                    # works even when the process lives in
            try:                                   # another mount namespace (sandbox, container)
                size = os.stat(f"/proc/{self.pid}/fd/{self.fd}").st_size
            except OSError:
                self.fd = None
        if size is None and self.path:
            try:
                size = os.stat(self.path).st_size
            except OSError:
                return
        if size is None:
            return
        self.push_size(size, dt)

    @property
    def eta_seconds(self) -> float | None:
        if self.total and self.rate > 0:
            return (self.total - self.size) / self.rate
        return None


def output_from_args(args: list[str], tool: str = "", cwd: str | None = None) -> str | None:
    out = None
    for i, a in enumerate(args):
        if tool == "curl" and a == "-O":           # curl -O: remote file name, no argument
            url_m = URL_RE.search(" ".join(args))
            out = os.path.basename(url_m.group(0).split("?")[0]) if url_m else None
            break
        for flag in ("-o", "--output", "-O", "--out", "-d", "--dir"):
            if a == flag and i + 1 < len(args):
                out = args[i + 1]
                break
            if a.startswith(flag + "="):
                out = a.split("=", 1)[1]
                break
        if out is not None:
            break
    if not out or out == "-":                      # "-" is stdout, nothing to measure
        return None
    if not os.path.isabs(out) and cwd:
        out = os.path.join(cwd, out)
    return out


def output_from_proc(pid: int) -> tuple[str, int | None] | None:
    """The file a downloader is writing, from its open descriptors: (path, fd)."""
    try:
        files = psutil.Process(pid).open_files()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return None
    for f in files:
        p = f.path
        if p.startswith(SKIP_PREFIX) or ".so" in os.path.basename(p):
            continue
        if getattr(f, "mode", "w") in ("w", "a", "r+", "a+"):
            fd = getattr(f, "fd", None)
            if fd is not None and fd >= 0:
                return p, fd                        # size read via /proc/<pid>/fd, path may be
            if os.path.isfile(p):                   # invisible from here (other namespace)
                return p, None
    return None


def _positional(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _pm_from_script(comm: str, script: str) -> str:
    if comm == "bun":
        return "bun"
    if "yarn" in script:
        return "yarn"
    if "pnpm" in script:
        return "pnpm"
    if script in ("npm", "npm-cli.js", "npx-cli.js"):
        return "npm"
    return ""


def classify(comm: str, args: list[str], cwd: str | None = None,
             downloaders: set[str] = DOWNLOADERS) -> tuple[str, str, str | None] | None:
    """(tool, label, staging_dir) when this process is downloading, else None.

    comm is the /proc name (15 chars), args the command line without argv[0]. Tools in `downloaders`
    match on the name alone; CMDLINE_TOOLS are runtimes, so the verb decides (a dev server run by
    yarn is not a download, `yarn install` is). staging_dir, when known, is a small directory whose
    growth is the download; otherwise progress comes from the process's read counter."""
    if comm in downloaders:
        url_m = URL_RE.search(" ".join(args))
        return comm, describe(comm, args, url_m.group(0) if url_m else None), None
    if comm not in CMDLINE_TOOLS:
        return None
    pos = _positional(args)
    where = f" · {os.path.basename(cwd)}" if cwd and cwd != HOME else ""
    if comm in ("node", "bun"):
        script = os.path.basename(pos[0]) if pos else ""
        pm = _pm_from_script(comm, script)
        if not pm:
            return None
        rest = pos[1:] if pm != "bun" else pos
        verb = rest[0] if rest else ""
        if pm == "yarn" and verb == "":
            verb = "install"
        if verb not in PKG_VERBS:
            return None
        return pm, f"{pm} {verb}{where}", None
    if comm == "deno":
        return ("deno", f"deno {pos[0]}{where}", None) if pos[:1] and pos[0] in ("install", "add", "cache") else None
    if comm == "java":
        joined = " ".join(args)
        if "com.android.sdklib" in joined or "sdkmanager" in joined:
            pkgs = [a for a in pos if ";" in a or a in ("platform-tools", "emulator")]
            staging = None
            for a in args:                        # -Dcom.android.sdklib.toolsdir=<sdk>/cmdline-tools/<v>[/bin]
                if a.startswith("-Dcom.android.sdklib.toolsdir="):
                    parts = a.split("=", 1)[1].rstrip("/").split(os.sep)
                    if "cmdline-tools" in parts:
                        sdk = os.sep.join(parts[: parts.index("cmdline-tools")]) or os.sep
                        staging = os.path.join(sdk, ".temp")
            if staging is None and os.environ.get("ANDROID_HOME"):
                staging = os.path.join(os.environ["ANDROID_HOME"], ".temp")
            return "sdkmanager", "sdkmanager " + (" ".join(pkgs)[:60] if pkgs else "update"), staging
        if "org.gradle" in joined or "gradle-wrapper" in joined:
            if "GradleDaemon" in joined or "GradleWrapperMain" in joined:
                return "gradle", f"gradle{where}", None
        return None
    if comm == "go":
        if pos[:2] == ["mod", "download"] or (pos and pos[0] in ("get", "install") and len(pos) > 1):
            return "go", f"go {' '.join(pos[:2])}{where}", None
        return None
    if comm == "cargo":
        if pos[:1] and pos[0] in ("fetch", "vendor") or (pos[:1] == ["install"] and len(pos) > 1):
            return "cargo", f"cargo {pos[0]}{where}", None
        return None
    if comm in ("uv", "pipx", "conda", "mamba"):
        verb = pos[0] if pos else ""
        if verb in PKG_VERBS or (comm == "uv" and pos[:2] in (["pip", "install"], ["tool", "install"], ["python", "install"])):
            return comm, f"{comm} {' '.join(pos[:2])[:24]}{where}", None
        return None
    if comm in ("podman", "docker"):
        if "pull" in pos:
            image = pos[pos.index("pull") + 1] if pos.index("pull") + 1 < len(pos) else ""
            return comm, f"{comm} pull {image}".rstrip(), None
        return None
    return None


def read_rchar(pid: int) -> int | None:
    """Bytes this process has read from any descriptor, sockets included (/proc/<pid>/io). A cheap
    proxy for download volume when a tool writes no single file we can watch."""
    try:
        with open(f"/proc/{pid}/io", "rb") as f:
            for line in f:
                if line.startswith(b"rchar:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


_DIR_CACHE: dict[str, tuple[float, int | None]] = {}


def dir_size(path: str, cap: int = 3000, ttl: float = 2.0) -> int | None:
    """Total bytes under a small staging directory; None if it does not exist or has more than cap
    entries (then it is not a cheap thing to walk every second and the caller falls back)."""
    now = time.monotonic()
    hit = _DIR_CACHE.get(path)
    if hit and now - hit[0] < ttl:
        return hit[1]
    total, seen, stack = 0, 0, [path]
    try:
        while stack:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    seen += 1
                    if seen > cap:
                        _DIR_CACHE[path] = (now, None)
                        return None
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
    except OSError:
        _DIR_CACHE[path] = (now, None)
        return None
    _DIR_CACHE[path] = (now, total)
    return total


def describe(tool: str, args: list[str], url: str | None) -> str:
    cmd = " ".join(args)
    if tool == "flatpak":
        verbs = [a for a in args if a in ("install", "update", "upgrade", "remote-ls", "run", "repair", "uninstall")]
        verb = verbs[0] if verbs else ""
        m = APPID_RE.search(" ".join(a for a in args if not a.startswith("-")))
        if m:
            return f"flatpak {verb} {m.group(0)}".replace("  ", " ")
        return f"flatpak {verb or 'update'}" + (" (all apps)" if verb in ("", "update", "upgrade") else "")
    if tool == "snap":
        pos = _positional(args)
        return f"snap {' '.join(pos[:2])}".strip() if pos[:1] and pos[0] in ("install", "refresh", "download") else f"snap {cmd[:30]}"
    if tool in ("rsync", "scp", "sftp"):
        return f"{tool} → {args[-1] if args else ''}"
    if tool.startswith("git"):
        return f"git {url or 'clone/fetch'}"
    if tool.startswith("pip"):
        return f"pip {args[-1] if args else ''}"
    if tool in ("yt-dlp", "youtube-dl", "gallery-dl"):
        return f"{tool} {url or cmd[:40]}"
    return url or cmd


STEAM_FLAG_RUNNING = 1024           # StateFlags bit set while Steam is actively downloading/updating an app
STEAM_FLAG_INSTALLED = 4
ACF_KV_RE = re.compile(r'^\s*"([A-Za-z]+)"\s+"([^"]*)"', re.M)


class SteamTracker:
    """Steam downloads: read steamapps/appmanifest_*.acf in every library (no downloader process to watch)."""

    RESCAN_LIBS = 120.0     # seconds between looks for new library folders

    def __init__(self) -> None:
        self.libs = self._libraries()
        self.libs_at = time.monotonic()
        self.known: set[int] = set()

    @staticmethod
    def _libraries() -> list[str]:
        libs: dict[str, None] = {}
        for root in STEAM_ROOTS:
            root = os.path.expanduser(root)
            if not os.path.isdir(os.path.join(root, "steamapps")):
                continue
            libs[os.path.realpath(root)] = None
            try:
                text = Path(root, "steamapps", "libraryfolders.vdf").read_text()
            except OSError:
                continue
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                p = m.group(1).replace("\\\\", "/")
                if os.path.isdir(os.path.join(p, "steamapps")):
                    libs[os.path.realpath(p)] = None
        return list(libs)

    RATE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] Current download rate: ([\d.]+) Mbps")

    def _reported_rate(self) -> str:
        """Steam's own 'Current download rate' from logs/content_log.txt, if it is less than two minutes old."""
        for root in STEAM_ROOTS:
            log = os.path.join(os.path.expanduser(root), "logs", "content_log.txt")
            try:
                with open(log, "rb") as fh:
                    fh.seek(0, 2)
                    fh.seek(max(0, fh.tell() - 8192))
                    tail = fh.read().decode(errors="replace")
            except OSError:
                continue
            hits = self.RATE_RE.findall(tail)
            if not hits:
                return ""
            stamp, mbps = hits[-1]
            try:
                age = time.time() - time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                return ""
            return f"net {float(mbps):.1f} Mbps" if age < 120 else ""
        return ""

    @staticmethod
    def _allocated(root: str) -> int | None:
        """Bytes actually written under root (st_blocks, so preallocated sparse files don't count as done)."""
        if not os.path.isdir(root):
            return None
        total = 0
        stack = [root]
        while stack:
            try:
                with os.scandir(stack.pop()) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                total += e.stat(follow_symlinks=False).st_blocks * 512
                        except OSError:
                            continue
            except OSError:
                continue
        return total

    @staticmethod
    def _manifest(path: str) -> dict[str, str]:
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            return {}
        out: dict[str, str] = {}
        for k, v in ACF_KV_RE.findall(text):
            out.setdefault(k, v)
        return out

    def scan(self, dt: float, active: dict[int, Download], finished: deque) -> set[int]:
        """Update `active` with Steam entries (keyed by -appid). Returns the keys seen this round."""
        seen: set[int] = set()
        now = time.monotonic()
        if now - self.libs_at > self.RESCAN_LIBS:            # Steam installed or a library added while we run
            self.libs, self.libs_at = self._libraries(), now
        for lib in self.libs:
            for mf in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
                try:
                    appid = int(os.path.basename(mf)[12:-4])
                except ValueError:
                    continue
                m = self._manifest(mf)
                try:
                    flags = int(m.get("StateFlags", "0"))
                    total = int(m.get("BytesToDownload", "0"))
                    done = int(m.get("BytesDownloaded", "0"))
                    stage_total = int(m.get("BytesToStage", "0"))
                    staged = int(m.get("BytesStaged", "0"))
                except ValueError:
                    continue
                running = bool(flags & STEAM_FLAG_RUNNING)
                queued = os.path.isdir(os.path.join(lib, "steamapps", "downloading", str(appid)))
                if not running and not queued:
                    continue
                if total <= 0:                       # placeholder entries (redistributables) carry no size yet
                    continue
                key = -appid
                seen.add(key)
                d = active.get(key)
                if d is None:
                    install = os.path.join(lib, "steamapps", "common", m.get("installdir") or str(appid))
                    d = Download(key, "steam", m.get("name") or f"app {appid}", None, install, time.monotonic())
                    d.last_change = time.monotonic()
                    active[key] = d
                    self.known.add(appid)
                # Steam only rewrites the manifest at checkpoints, so measure the download folder on disk
                # (uncompressed, compared with BytesToStage); fall back to the manifest's compressed counters.
                now = time.monotonic()
                if now - d.last_walk >= 2.0:
                    on_disk = self._allocated(os.path.join(lib, "steamapps", "downloading", str(appid))) if stage_total else None
                    if on_disk is not None:
                        d.total = stage_total
                        size = min(stage_total, max(on_disk, staged))
                    else:
                        d.total = total
                        size = done
                    if size != d.size:
                        d.last_change = now
                    d.size = size
                    d.samples += 1
                    d.history.append((now, size))                     # bursty writer: average over ~60 s
                    while len(d.history) > 2 and now - d.history[0][0] > 60:
                        d.history.popleft()
                    (t0, s0), (t1, s1) = d.history[0], d.history[-1]
                    d.rate = max(0.0, (s1 - s0) / (t1 - t0)) if t1 > t0 else 0.0
                    d.last_walk = now
                notes = []
                if not running:
                    notes.append("paused")
                elif d.samples > 2 and now - d.last_change > 10:
                    notes.append("stalled")            # e.g. Steam pauses downloads while a game is running
                if stage_total and staged * 100 // stage_total > 0 and staged < stage_total:
                    notes.append(f"committed {min(100, staged * 100 // stage_total)}%")   # moved into the game folder
                if running and now - d.last_walk < 0.01:                    # a walk just happened: refresh the log note
                    d.reported = self._reported_rate()
                if d.reported:
                    notes.append(d.reported)
                d.note = " · ".join(notes)
        for key in list(active):
            if key < 0 and key not in seen and active[key].tool == "steam":
                d = active.pop(key)
                if d.total and d.size >= d.total * 0.95:
                    finished.appendleft((time.strftime("%H:%M:%S"), d.label, d.total))
        return seen


class Tracker:
    """Finds downloader processes in the shared /proc snapshot, measures each one, plus Steam.

    `tools` adds process names that count as downloaders on this machine (they are also given the
    open-file treatment, so a bar appears when the output file and a Content-Length can be found);
    `ignore` removes names you never want listed."""

    def __init__(self, tools: set[str] | None = None, ignore: set[str] | None = None) -> None:
        self.active: dict[int, Download] = {}
        self.finished: deque[tuple[str, str, int]] = deque(maxlen=8)
        self.probe = SizeProbe()
        self.me = os.getpid()
        self.steam = SteamTracker()
        extra = {t for t in (tools or set()) if t}
        self.ignore = set(ignore or set())
        self.downloaders = (DOWNLOADERS | extra) - self.ignore
        self.file_tools = (FILE_TOOLS | extra) - self.ignore
        self.cmdline_tools = CMDLINE_TOOLS - self.ignore
        # /proc comm is truncated to 15 chars: map what /proc shows back to the full tool name
        self.comm_map = {name[:15]: name for name in self.downloaders | self.cmdline_tools}
        self.comm_map["MainThread"] = "node"                                            # recent Node names its main thread
        self.kinds: dict[int, tuple | None] = {}     # pid -> classify() result, so a runtime's cmdline is read once

    def apply_total(self, d: Download) -> None:
        """Probe the URL for a total; drop totals the file has already outgrown (redirect/error bodies)."""
        if d.path and d.url and not d.total:
            d.total = self.probe.get(d.url)
        if d.total and d.size > d.total:
            d.total = 0
            if d.url:
                self.probe.results[d.url] = 0

    def scan(self, dt: float) -> None:
        self.scan_processes(dt)
        self.steam.scan(dt, self.active, self.finished)

    def measure(self, d: Download, dt: float) -> None:
        """Advance size/rate: from the output file when there is one, else from the staging directory,
        else from the process's own read counter (marked approximate)."""
        if d.path or d.fd is not None:
            d.update_size(dt)
            return
        if d.staging:
            size = dir_size(d.staging)
            if size is not None:
                d.approx = False
                d.push_size(size, dt)
                return
        rchar = read_rchar(d.pid)
        if rchar is None:
            return
        if d.io_base < 0:
            d.io_base = rchar
        d.approx = True
        d.push_size(rchar - d.io_base, dt)

    def retire(self, d: Download) -> None:
        if d.path or d.size > 0:
            self.finished.appendleft((time.strftime("%H:%M:%S"), d.display_name, max(d.size, d.total)))

    def _classify_row(self, row: dict, comm: str) -> tuple | None:
        pid = row["pid"]
        if pid in self.kinds:
            return self.kinds[pid]
        cmd = SCANNER.cmdline(row)
        args = cmd.split(" ")[1:] if cmd and not cmd.startswith("[") else []
        cwd = None
        if comm in self.cmdline_tools or comm in self.file_tools:
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                cwd = None
        kind = classify(comm, args, cwd, self.downloaders) if (args or comm == "flatpak") else None
        if kind is not None:
            kind = (*kind, args, cwd)
        self.kinds[pid] = kind
        return kind

    def scan_processes(self, dt: float) -> None:
        snap = SCANNER.snapshot(0.5)
        seen: set[int] = set()
        for row in snap.rows:
            comm = self.comm_map.get(row["name"])
            if not comm or row["pid"] == self.me or row["kernel"] or row["st"] == "Z":
                continue
            pid = row["pid"]
            d = self.active.get(pid)
            if d is None:
                kind = self._classify_row(row, comm)
                if kind is None:
                    seen.add(pid)                       # keep the negative answer cached while it lives
                    continue
                tool, label, staging, args, cwd = kind
                url_m = URL_RE.search(" ".join(args))
                url = url_m.group(0) if url_m else None
                path = output_from_args(args, tool, cwd) if tool in self.file_tools else None
                d = Download(pid, tool, label, url, path, time.monotonic(), staging=staging)
                self.active[pid] = d
            seen.add(pid)
            # a path from the arguments may not exist yet; give it a few seconds before guessing from open files
            if d.fd is None and d.tool in self.file_tools and (
                    d.path is None or (not os.path.isfile(d.path) and time.monotonic() - d.first_seen > 3)):
                found = output_from_proc(pid)
                if found:
                    d.path, d.fd = found
            self.measure(d, dt)
            self.apply_total(d)
        for pid in list(self.active):
            if pid not in seen and pid > 0:
                self.retire(self.active.pop(pid))
        for pid in list(self.kinds):
            if pid not in seen:
                del self.kinds[pid]


def render_auto(tracker: Tracker, net: NetMeter, width: int) -> Group:
    header = Table.grid(expand=True)
    header.add_column(); header.add_column(justify="right")
    header.add_row(
        Text.assemble(("dlwatch", "bold"), ("  active downloads   ", "dim"),
                      (f"↓ {human(net.rate)}/s", "cyan"), (f" on {net.iface}", "dim")),
        Text(time.strftime("%H:%M:%S"), style="dim"))
    parts: list = [header, Text()]

    if not tracker.active:
        parts.append(Text("  nothing downloading right now — watching…", style="dim"))
    for d in sorted(tracker.active.values(), key=lambda x: x.name):
        if d.path:
            glyph = "♨ " if d.tool == "steam" else "▶ "
            parts.append(Text.assemble((glyph, "bold"), (shorten(d.display_name, 48), "bold"), ("   " + d.tool, "dim"),
                                       ((" · " + d.note) if d.note else "", "dim")))
            row = Table.grid(padding=(0, 2))
            row.add_column(width=36); row.add_column()
            if d.total:
                pct = d.pct
                bar = ProgressBar(total=d.total, completed=d.size, width=34,
                                  complete_style="green", finished_style="green", style="grey37")
                stats = Text.assemble((f"{pct:3d}%", "bold"), f"  {human(d.size)} / {human(d.total)}   ",
                                      (f"{human(d.rate)}/s", "cyan"), "   ", (f"ETA {eta(d.eta_seconds)}", "yellow"))
            else:
                bar = ProgressBar(total=None, pulse=True, width=34, pulse_style="green", style="grey37")
                stats = Text.assemble(("size unknown", "dim"), f"  {human(d.size)} so far   ", (f"{human(d.rate)}/s", "cyan"))
            row.add_row(bar, stats)
            parts.append(row)
            parts.append(Text("  → " + shorten(os.path.dirname(d.path), width - 6), style="dim"))
        else:
            age = int(time.monotonic() - d.first_seen)
            parts.append(Text.assemble(("● ", "magenta"), (d.label[: width - 24], "magenta"),
                                       (f"   {d.tool} · running {age}s", "dim")))
            if d.samples and d.size > 0:
                parts.append(Text.assemble("  ", ("≈ " if d.approx else "", "dim"), f"{human(d.size)} so far   ",
                                           (f"{human(d.rate)}/s", "cyan"),
                                           ("   (bytes read by the process)" if d.approx else "   (staging dir)", "dim")))
        parts.append(Text())

    if tracker.finished:
        parts.append(Text("finished this session:", style="dim"))
        for when, name, size in tracker.finished:
            parts.append(Text.assemble(f"  {when}  ", ("✔", "green"), f" {shorten(name, 45)}  ", (human(size), "dim")))
        parts.append(Text())
    parts.append(Text("q quit", style="dim"))
    return Group(*parts)


def run_auto(console: Console, tools: set[str] | None = None, ignore: set[str] | None = None) -> None:
    tracker, net = Tracker(tools, ignore), NetMeter()
    last = time.monotonic()
    with Keys() as keys, Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            now = time.monotonic()
            dt = max(0.2, now - last); last = now
            net.tick(dt)
            tracker.scan(dt)
            live.update(render_auto(tracker, net, console.width), refresh=True)
            if wait_or_quit(keys, 1.0):
                break
    if tracker.finished:
        console.print("[dim]finished while watching:[/dim]")
        for when, name, size in tracker.finished:
            console.print(f"  {when}  [green]✔[/green] {name}  [dim]{human(size)}[/dim]")


# --------------------------------------------------------------------------- folder mode

def folder_stats(folder: Path, pattern: str) -> tuple[int, int]:
    files = [p for p in folder.glob(pattern) if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def run_folder(console: Console, folder: Path, total_text: str | None, pattern: str, logfile: str | None) -> None:
    if not folder.is_dir():
        sys.exit(f"dlwatch: not a directory: {folder}")
    mode, total = parse_total(total_text)
    net = NetMeter()
    start = time.monotonic()
    count, size = folder_stats(folder, pattern)
    start_size, prev_size, last = size, size, start

    with Keys() as keys, Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            now = time.monotonic(); dt = max(0.2, now - last); last = now
            net.tick(dt)
            count, size = folder_stats(folder, pattern)
            grow = max(0, size - prev_size) / dt; prev_size = size
            elapsed = now - start

            parts: list = [Text.assemble(("dlwatch", "bold"), ("  folder  ", "dim"),
                                         shorten(str(folder), console.width - 40), "   ",
                                         (f"↓ {human(net.rate)}/s", "cyan")), Text()]
            if mode == "count":
                pct = min(100, count * 100 // total) if total else 0
                parts.append(ProgressBar(total=total, completed=min(count, total), width=40, complete_style="green", style="grey37"))
                parts.append(Text.assemble((f"  {pct}%", "bold"), f"   {count} / {total} files"))
            elif mode == "bytes":
                pct = min(100, size * 100 // total) if total else 0
                avg = (size - start_size) / elapsed if elapsed > 3 else 0
                parts.append(ProgressBar(total=total, completed=min(size, total), width=40, complete_style="green", style="grey37"))
                parts.append(Text.assemble((f"  {pct}%", "bold"), f"   {human(size)} / {human(total)}   ",
                                           (f"ETA {eta((total - size) / avg if avg > 0 else None)}", "yellow")))
            else:
                parts.append(Text.assemble((str(count), "bold"), " files   ", (human(size), "bold")))
            parts.append(Text.assemble("folder growing at ", (f"{human(grow)}/s", "green")))
            if logfile and os.path.isfile(logfile):
                try:
                    with open(logfile, "rb") as fh:
                        fh.seek(0, 2); fh.seek(max(0, fh.tell() - 4096))
                        tail = fh.read().decode(errors="replace").rstrip("\n").splitlines()
                    parts.extend([Text(), Text.assemble(("log: ", "dim"), (tail[-1] if tail else "")[: console.width - 6])])
                except OSError:
                    pass
            parts.extend([Text(), Text(f"elapsed {duration(elapsed)} · q quit", style="dim")])
            live.update(Group(*parts), refresh=True)
            if wait_or_quit(keys, 1.0):
                break


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="dlwatch", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n", 2)[2])
    ap.add_argument("dir", nargs="?", help="folder mode: folder to watch")
    ap.add_argument("total", nargs="?", help="folder mode: expected file count (93) or size (4.7G)")
    ap.add_argument("-p", "--pattern", default="*", help="folder mode: only count files matching this glob")
    ap.add_argument("-l", "--log", help="folder mode: show the last line of this log file")
    ap.add_argument("-t", "--tool", action="append", default=[], metavar="NAME",
                    help="extra process name to treat as a downloader (also: [panels.dl] tools in dash.toml)")
    ap.add_argument("-w", "--window", action="store_true", help="open in a new terminal window")
    a = ap.parse_args(argv)

    if a.window:
        relaunch_in_window(self_argv(), "dlwatch")
        return
    console = Console()
    try:
        if a.dir:
            run_folder(console, Path(a.dir).expanduser(), a.total, a.pattern, a.log)
        else:
            from .core import load_config
            cfg = load_config().get("panels", {}).get("dl", {})
            run_auto(console, set(cfg.get("tools", [])) | set(a.tool), set(cfg.get("ignore", [])))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
