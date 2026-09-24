"""Jobs: everything the machine is working on right now — builds, compilers, bundlers, test runs, encoders,
archivers, copies, package and system installs — grouped by process tree, with resource use, the current
stage and a progress estimate from how long the same job took last time on this machine.

Pure logic (no rich): the `jobs` panel and the `jobsmon` launcher render it. Downloads are the `dl` panel's
business, so pure downloaders (curl, yarn install, sdkmanager, …) are deliberately not jobs here.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .procscan import CLK_TCK, SCANNER, Snapshot

HOME = str(Path.home())
HOME_REAL = os.path.realpath(HOME)
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "mon"
HISTORY_FILE = CACHE_DIR / "jobs.json"

# comm (15-char /proc name) -> kind for tools whose name alone says "work in progress". All finite.
FINITE: dict[str, str] = {}
for _kind, _names in {
    "compile": ("cc1", "cc1plus", "clang", "clang++", "gcc", "g++", "cc", "c++", "as", "rustc", "swiftc", "javac",
                "kotlinc", "zig", "ghc", "tsc", "swc", "esbuild", "nasm", "yasm", "cython", "nvcc", "glslc", "dxc", "moc"),
    "link": ("ld", "ld.lld", "lld", "ld.gold", "ld.bfd", "mold", "gold", "lto-wrapper", "lto1", "collect2"),
    "build": ("make", "gmake", "ninja", "cmake", "meson", "bazel", "buck2", "mvn", "sbt", "dotnet", "msbuild", "cabal",
              "stack", "mix", "rebar3", "gradle", "ant", "scons", "waf", "xmake", "premake5", "autoreconf", "configure",
              "libtool", "aapt2", "d8", "r8", "dex2oat", "flatc", "protoc", "bindgen", "wasm-pack", "wasm-opt", "emcc"),
    "test": ("pytest", "py.test", "tox", "nox", "ctest", "gotestsum", "phpunit", "rspec", "minitest", "bats"),
    "container": ("buildah", "kaniko", "img", "buildkitd", "buildkit-runc"),
    "media": ("ffmpeg", "HandBrakeCLI", "magick", "convert", "mogrify", "gifski", "gifsicle", "sox", "lame", "flac", "opusenc",
              "x264", "x265", "aomenc", "SvtAv1EncApp", "rav1e", "av1an", "vspipe", "mkvmerge", "mkvextract", "cwebp",
              "avifenc", "cjpeg", "pngquant", "oxipng", "zopflipng", "optipng", "jpegoptim", "yt-dlp", "whisper", "tesseract",
              "blender", "inkscape", "gs", "pdftoppm", "pdftocairo", "qpdf", "ocrmypdf", "libreoffice", "soffice.bin"),
    "archive": ("tar", "zip", "unzip", "7z", "7za", "7zz", "xz", "unxz", "zstd", "unzstd", "gzip", "gunzip", "bzip2", "bunzip2",
                "pigz", "pbzip2", "pixz", "lz4", "brotli", "unrar", "rar", "zpaq", "squashfs", "mksquashfs", "unsquashfs",
                "cpio", "ar", "bsdtar", "lzip", "lrzip", "plzip"),
    "copy": ("cp", "mv", "dd", "shred", "pv", "cat", "install", "ddrescue", "partclone.ext4", "mkfs.ext4", "mkfs.btrfs",
             "mkfs.xfs", "mkfs.vfat", "e2fsck", "fsck.ext4", "xfs_repair", "btrfs", "resize2fs", "cryptsetup", "wipefs",
             "badblocks", "fstrim", "sgdisk", "parted"),
    "backup": ("borg", "restic", "rclone", "duplicity", "kopia", "bup", "rdiff-backup", "duplicati", "timeshift", "snapper"),
    "system": ("dnf", "dnf5", "dnf-3", "rpm-ostree", "bootc", "ostree", "yum", "microdnf", "apt", "apt-get", "aptitude", "dpkg",
               "pacman", "yay", "paru", "zypper", "emerge", "nix", "nix-build", "nix-env", "nixos-rebuild", "rpm", "rpmbuild",
               "mock", "makepkg", "flatpak-builder", "appimagetool", "snapcraft", "debuild", "dpkg-buildpackage", "pbuilder",
               "dracut", "mkinitcpio", "mkinitramfs", "update-initramfs", "grub2-mkconfig", "grub-mkconfig", "akmods",
               "akmodsbuild", "dkms", "depmod", "ldconfig", "updatedb", "plocate-build", "mandb", "fc-cache", "gtk-update-icon",
               "glib-compile-sch", "update-desktop-", "systemd-sysusers", "kernel-install", "vmlinuz", "sbctl", "mokutil"),
    "python": ("mypy", "pyright", "ruff", "black", "isort", "pylint", "flake8", "sphinx-build", "mkdocs", "pyinstaller", "nuitka",
               "poetry", "uv", "pipx", "conda", "mamba", "micromamba", "hatch", "pdm", "maturin"),
    "vcs": ("git-gc", "git-repack", "git-fsck", "git-pack-objects", "git-index-pack", "git-lfs", "hg", "svn", "bzr", "jj"),
    "ml": ("ollama-runner", "llama-server", "llama-cli", "koboldcpp", "comfyui", "invokeai", "stable-diffusion", "rembg"),
    "db": ("pg_dump", "pg_restore", "pg_dumpall", "mysqldump", "mysqlpump", "mongodump", "mongorestore", "sqlite3", "vacuumdb", "reindexdb"),
    "vm": ("virt-install", "qemu-img", "virt-sparsify", "virt-resize", "virt-builder", "packer", "vagrant"),
    "iac": ("terraform", "tofu", "pulumi", "ansible", "ansible-playboo", "kubectl", "helm", "skaffold", "kustomize", "argocd"),
}.items():
    for _n in _names:
        FINITE[_n[:15]] = _kind

# Runtimes whose name says nothing: the command line decides. Bare interpreters running unknown scripts are not jobs.
CMDLINE_TOOLS = {"node", "java", "go", "docker", "podman", "git", "bun", "deno", "cargo", "python", "python3", "ruby", "perl",
                 "dotnet", "nextest", "rustup", "pip", "pip3", "swift", "flutter", "dart", "xcodebuild", "npm", "npx", "pnpm", "yarn"}
# comms that hide the real program: Node names its main thread, npm/yarn rewrite their process title
ODD_COMMS = ("MainThread", "npm", "yarn", "pnpm", "bun", "node")
CMDLINE_TOOLS |= {f"python3.{i}" for i in range(8, 20)}
PKG_INSTALL_VERBS = {"install", "i", "ci", "add", "update", "up", "upgrade", "install-test", "it"}     # dl's business
WATCH_FLAGS = {"--watch", "-w", "--watchAll", "watch", "--hot", "dev", "serve", "start", "preview"}

# member comm -> stage word shown after the job label (gcc's cc1plus under ninja under gradle = "C++")
STAGE_OF = {"cc1plus": "C++", "cc1": "C", "clang": "clang", "clang++": "clang", "ld": "link", "ld.lld": "link", "lld": "link",
            "mold": "link", "javac": "javac", "aapt2": "aapt2", "ninja": "ninja", "cmake": "cmake", "rustc": "rustc",
            "d8": "d8", "r8": "r8", "esbuild": "esbuild", "tsc": "tsc", "swc": "swc", "as": "asm", "tar": "tar", "xz": "xz",
            "zstd": "zstd", "gzip": "gzip", "ffmpeg": "ffmpeg", "protoc": "protoc", "dex2oat": "dex2oat", "lto1": "lto"}

# never shown in the "busy" catch-all: interactive apps, compositors, players, VMs, our own dashboard
BUSY_IGNORE = {"firefox", "firefox-bin", "chrome", "chromium", "chromium-browse", "brave", "vivaldi-bin", "msedge", "opera",
               "Isolated Web Co", "Web Content", "WebKitWebProces", "WebKitNetworkPr", "RDD Process", "Socket Process",
               "Xwayland", "Xorg", "gnome-shell", "kwin_wayland", "kwin_x11", "plasmashell", "mutter", "sway", "Hyprland",
               "hyprland", "cosmic-comp", "niri", "steam", "steamwebhelper", "gamescope", "wine", "wine64", "wineserver",
               "wine-preloader", "wine64-preloade", "qemu-system-x86", "qemu-system-aar", "emulator", "qemu-kvm", "crosvm",
               "VirtualBoxVM", "vmware-vmx", "crashpad_handle", "pipewire", "pipewire-pulse", "wireplumber", "pulseaudio",
               "mpv", "vlc", "totem", "celluloid", "obs", "Discord", "slack", "spotify", "code", "codium", "electron",
               "zed", "cursor", "Cursor", "konsole", "gnome-terminal-", "kitty", "alacritty", "foot", "wezterm-gui",
               "ptyxis", "tmux: server", "mondash", "dlwatch", "btop", "htop", "top", "glances", "nvtop"}


_GRADLE_TASK_RE = __import__("re").compile(r"(:?[A-Za-z][\w-]*)(:[A-Za-z][\w-]*)*")


def _positional(args: list[str]) -> list[str]:
    return [a for a in args if a and not a.startswith("-")]


def _script(args: list[str]) -> str:
    """Basename of the first non-flag argument (the script a runtime is executing), '' if none."""
    pos = _positional(args)
    return os.path.basename(pos[0]) if pos else ""


def py_names_with_dots() -> set[str]:
    return {"sphinx.cmd.build", "torch.distributed.run", "http.server"}


@dataclass(frozen=True)
class Det:
    kind: str            # gradle, expo, compile, media, …  (groups the history and picks the glyph)
    label: str           # what the row says: "expo run:android", "cargo build", "ffmpeg"
    finite: bool         # True: shows while alive. False: a daemon/server, shows only while its tree is busy
    stage: str = ""      # word this process contributes as a stage when it is a member of a bigger job


def classify(comm: str, args: list[str]) -> Det | None:
    """Decide whether one process is (part of) a job. comm is the /proc name (15 chars), args the command
    line without argv[0]. Returns None for everything that is not work in progress or belongs to `dl`."""
    if comm in FINITE:
        kind = FINITE[comm]
        verb = ""
        if comm in ("cargo", "make", "gmake", "ninja", "cmake", "mvn", "gradle", "dotnet", "docker", "podman", "flutter", "dart",
                    "git", "dnf", "dnf5", "dnf-3", "rpm-ostree", "bootc", "apt", "apt-get", "pacman", "zypper", "nix", "poetry",
                    "uv", "pipx", "conda", "mamba", "terraform", "tofu", "pulumi", "helm", "kubectl", "btrfs", "cryptsetup"):
            pos = _positional(args)
            verb = pos[0] if pos else ""
        if comm in ("dnf", "dnf5", "dnf-3", "yum", "microdnf", "apt", "apt-get", "zypper", "pacman") and verb in (
                "search", "info", "list", "repoquery", "provides", "help", "history", "show", "policy", "-Ss", "-Si", "-Q", "-Qi"):
            return None                                   # queries, not work
        label = f"{comm} {verb}".strip() if verb and len(verb) < 24 else comm
        return Det(kind, label, True, STAGE_OF.get(comm, ""))
    if comm not in CMDLINE_TOOLS:
        return None
    pos = _positional(args)
    joined = " ".join(args)

    if comm == "java":
        if "GradleDaemon" in joined:
            return Det("gradle", "gradle daemon", False, "gradle")
        if "KotlinCompileDaemon" in joined:
            return Det("gradle", "kotlin daemon", False, "kotlin")
        if "GradleWrapperMain" in joined or "org.gradle.launcher" in joined or "-Dorg.gradle.appname" in joined:
            tasks, skip = [], False
            for a in args:
                if skip:                                  # the argument of -x / --exclude-task is not a task to run
                    skip = False
                    continue
                if a in ("-x", "--exclude-task", "-p", "--project-dir", "-b", "--build-file", "-c", "--settings-file", "-I", "--init-script",
                         "--console", "--warning-mode", "--max-workers", "-g", "--gradle-user-home", "--project-cache-dir", "-F",
                         "--dependency-verification", "--priority", "-P", "-D", "--include-build", "--write-locks"):
                    skip = True
                    continue
                if a.startswith("-") or a.endswith(".jar") or "/" in a or "=" in a:
                    continue
                if _GRADLE_TASK_RE.fullmatch(a):
                    tasks.append(a)
            return Det("gradle", ("gradle " + " ".join(tasks[:2]))[:32] if tasks else "gradle", True, "gradle")
        if "plexus.classworlds" in joined or "org.apache.maven" in joined:
            return Det("build", "maven " + " ".join(a for a in pos if a in ("compile", "package", "install", "verify", "test", "deploy"))[:30], True, "maven")
        if "sbt-launch" in joined or "xsbt.boot" in joined:
            return Det("build", "sbt", True, "sbt")
        if "com.android.sdklib" in joined or "sdkmanager" in joined:
            return None                                   # dl
        if "com.android.tools" in joined or "lint" in joined and "android" in joined:
            return Det("gradle", "android lint", True, "lint")
        return None

    if comm in ("node", "bun", "deno", "npm", "npx", "yarn", "pnpm"):
        if comm == "node":
            script, rest = _script(args), pos[1:]
        else:                                             # the runtime is the program (bun, deno) or a rewritten title (npm, yarn)
            script, rest = comm, pos
        verb = rest[0] if rest else ""
        if comm == "deno":
            if verb in ("compile", "test", "task", "bundle", "lint", "fmt", "check", "doc", "bench", "publish", "install"):
                return Det("test" if verb in ("test", "bench") else "compile", f"deno {verb}", True, "deno")
            if verb in ("run", "serve") and any(a in ("--watch", "-w") for a in args):
                return Det("script", f"deno {verb}", False, "deno")
            return None
        if script in ("yarn", "yarn.js", "yarn.cjs", "npm", "npm-cli.js", "pnpm", "pnpm.cjs", "bun"):
            pm = "yarn" if script.startswith("yarn") else "npm" if script.startswith("npm") else "pnpm" if script.startswith("pnpm") else "bun"
            if pm == "yarn" and verb == "":
                verb = "install"
            if verb in PKG_INSTALL_VERBS:
                return None                               # dl
            if verb in ("run", "run-script", "exec", "dlx", "x", "create"):
                if verb in ("exec", "dlx", "x", "create"):
                    return None                           # wrapper: the child is the job
                verb = rest[1] if len(rest) > 1 else ""
            if not verb or verb in ("--version", "-v", "help", "why", "list", "ls", "info", "outdated", "audit", "login", "whoami", "config"):
                return None
            finite = verb not in WATCH_FLAGS and not any(w in args for w in ("--watch", "-w", "--watchAll"))
            return Det("script", f"{pm} {verb}", finite, verb[:12])
        if script in ("npx-cli.js", "npx"):
            return None
        if script in ("cli.js", "cli.cjs", "index.js", "main.js", "bin.js", "run.js"):
            # generic entry names: recover the package from the path (…/node_modules/<pkg>/…)
            path = pos[0] if pos else ""
            if "node_modules/" in path:
                seg = path.split("node_modules/")[-1].split("/")
                script = seg[1] if seg[0].startswith("@") and len(seg) > 1 else seg[0]
        name = script.removesuffix(".js").removesuffix(".cjs").removesuffix(".mjs")
        if name in ("expo", "expo-cli"):
            if verb in ("start", "start:web"):
                return Det("expo", "expo start", False, "metro")
            if verb.startswith("run:") or verb in ("prebuild", "export", "export:embed", "export:web", "install", "customize", "doctor", "lint"):
                return Det("expo", f"expo {verb}", True, "expo")
            return None
        if name in ("eas", "eas-cli"):
            return Det("expo", f"eas {verb}"[:30], True, "eas") if verb and not verb.startswith("-") else None
        if name in ("react-native", "metro"):
            return Det("script", f"{name} {verb}".strip(), verb != "start" and verb != "", "metro")
        finite_names = {"tsc": "compile", "tsup": "compile", "tsdown": "compile", "esbuild": "compile", "webpack": "compile",
                        "webpack-cli": "compile", "rollup": "compile", "rolldown": "compile", "babel": "compile", "swc": "compile",
                        "vite": "compile", "rspack": "compile", "rsbuild": "compile", "parcel": "compile", "turbo": "build",
                        "nx": "build", "lerna": "build", "jest": "test", "vitest": "test", "mocha": "test", "ava": "test",
                        "playwright": "test", "cypress": "test", "karma": "test", "eslint": "lint", "biome": "lint",
                        "oxlint": "lint", "stylelint": "lint", "prettier": "lint", "next": "compile", "nuxt": "compile", "nuxi": "compile",
                        "astro": "compile", "remix": "compile", "gatsby": "compile", "ng": "compile", "storybook": "compile",
                        "docusaurus": "compile", "vitepress": "compile", "typedoc": "compile", "tsx": "script", "ts-node": "script",
                        "prisma": "db", "drizzle-kit": "db", "knex": "db", "sequelize": "db", "typeorm": "db", "wrangler": "iac",
                        "vercel": "iac", "netlify": "iac", "firebase": "iac", "serverless": "iac", "cdk": "iac", "sst": "iac",
                        "electron-builder": "build", "electron-forge": "build", "tauri": "build", "patch-package": "build",
                        "husky": "vcs", "lint-staged": "lint", "sharp": "media", "puppeteer": "test", "lighthouse": "test"}
        if name in finite_names:
            watching = verb in WATCH_FLAGS or any(a in ("--watch", "-w", "--watchAll", "--ui") for a in args)
            if name in ("prettier", "husky") and not rest:
                return None
            label = f"{name} {verb}".strip() if verb and len(verb) < 16 and not verb.startswith("-") else name
            return Det(finite_names[name], label, not watching, name)
        return None

    if comm == "go":
        verb = pos[0] if pos else ""
        if verb in ("build", "test", "vet", "generate", "install", "run", "tool", "fmt", "fix", "doc", "work"):
            return Det("compile" if verb in ("build", "install", "run") else "test" if verb == "test" else "build",
                       f"go {verb}", True, "go")
        return None                                       # `go mod download`, `go get` → dl

    if comm == "cargo":
        verb = pos[0] if pos else ""
        if verb in ("watch",):
            return Det("compile", "cargo watch", False, "cargo")
        if verb in ("build", "b", "check", "c", "clippy", "test", "t", "bench", "doc", "d", "run", "r", "install", "publish", "package",
                    "fix", "fmt", "nextest", "tarpaulin", "llvm-cov", "miri", "audit", "update", "vendor", "tree", "generate", "make"):
            return Det("test" if verb in ("test", "t", "bench", "nextest", "tarpaulin", "llvm-cov", "miri") else "compile",
                       f"cargo {verb}", True, "cargo")
        return None

    if comm in ("docker", "podman"):
        if "pull" in pos or "push" in pos or "login" in pos:
            return None                                   # dl
        for v in ("build", "buildx", "compose", "save", "load", "commit", "export", "import", "system", "image"):
            if v in pos:
                sub = pos[pos.index(v) + 1] if pos.index(v) + 1 < len(pos) else ""
                if v == "compose" and sub not in ("build", "up", "pull", "create"):
                    return None
                if v == "system" and sub != "prune":
                    return None
                if v == "image" and sub not in ("prune", "build", "save", "load"):
                    return None
                if v not in ("compose", "system", "image"):
                    sub = ""                              # `build -t tag .`: the tag is not a sub-command
                return Det("container", f"{comm} {v} {sub}".rstrip(), True, comm)
        return None

    if comm == "git":
        verb = pos[0] if pos else ""
        if verb in ("gc", "repack", "fsck", "prune", "filter-branch", "filter-repo", "bisect", "rebase", "merge", "cherry-pick",
                    "lfs", "submodule", "subtree", "worktree", "stash", "checkout", "switch", "restore", "apply", "am",
                    "format-patch", "archive", "bundle", "count-objects", "verify-pack", "pack-objects", "index-pack", "maintenance"):
            return Det("vcs", f"git {verb}", True, "git")
        return None                                       # clone/fetch/pull/push → dl

    if comm.startswith("python") or comm in ("ruby", "perl"):
        script = _script(args)
        mod = args[args.index("-m") + 1] if "-m" in args and args.index("-m") + 1 < len(args) else ""
        name = (mod or script).removesuffix(".py").removesuffix(".rb").removesuffix(".pl")
        if mod and name not in py_names_with_dots():
            name = name.split(".")[0]                     # `-m pytest.__main__`, `-m pip.__main__` → first package
        if name in ("pip", "pip3", "pip._internal", "ensurepip", "http.server", "venv"):
            return None
        py_kinds = {"pytest": "test", "unittest": "test", "nose2": "test", "tox": "test", "nox": "test", "coverage": "test",
                    "behave": "test", "robot": "test", "mypy": "python", "pyright": "python", "ruff": "python", "black": "python",
                    "isort": "python", "pylint": "python", "flake8": "python", "build": "build", "setup": "build", "build_ext": "build",
                    "cython": "compile", "cythonize": "compile", "nuitka": "build", "PyInstaller": "build", "pyinstaller": "build",
                    "sphinx": "build", "sphinx.cmd.build": "build", "mkdocs": "build", "pdoc": "build", "manage": "script",
                    "celery": "script", "alembic": "db", "django": "script", "scrapy": "script", "train": "ml", "finetune": "ml",
                    "inference": "ml", "torch.distributed.run": "ml", "torchrun": "ml", "accelerate": "ml", "deepspeed": "ml",
                    "jupyter": "script", "ansible-playbook": "iac", "ansible": "iac", "yt_dlp": "media", "yt-dlp": "media",
                    "whisper": "media", "gallery-dl": "media", "streamlink": "media", "rembg": "media", "ocrmypdf": "media",
                    "meson": "build", "scons": "build", "waf": "build", "conan": "build", "west": "build", "platformio": "build",
                    "pio": "build", "idf": "build", "esptool": "build", "bumpversion": "vcs", "twine": "build", "hatch": "python",
                    "pdm": "python", "poetry": "python", "uv": "python", "pipx": "python", "conda": "python", "mamba": "python"}
        if name in ("manage",) and pos[1:2] and pos[1] in ("runserver", "shell"):
            return Det("script", f"manage {pos[1]}", False, "django")
        if name in py_kinds:
            verb = pos[1] if len(pos) > 1 and not pos[1].startswith("-") and len(pos[1]) < 16 else ""
            if name == "manage" and verb in ("migrate", "test", "collectstatic", "makemigrations", "loaddata", "dumpdata"):
                return Det("test" if verb == "test" else "db" if "migrat" in verb or "data" in verb else "build", f"manage {verb}", True, "django")
            return Det(py_kinds[name], f"{name} {verb}".strip(), True, name[:10])
        return None

    if comm in ("dotnet",):
        verb = pos[0] if pos else ""
        return Det("build", f"dotnet {verb}", verb not in ("watch", "run"), "dotnet") if verb in (
            "build", "test", "publish", "pack", "restore", "run", "watch", "clean", "format", "ef") else None

    if comm in ("flutter", "dart"):
        verb = pos[0] if pos else ""
        if verb in ("build", "test", "analyze", "pub", "compile", "run", "create", "precache", "doctor", "gen-l10n", "format"):
            return Det("compile" if verb in ("build", "compile", "run") else "test" if verb in ("test", "analyze") else "build",
                       f"{comm} {verb}", verb != "run", comm)
        return None

    if comm == "swift":
        verb = pos[0] if pos else ""
        return Det("compile", f"swift {verb}", True, "swift") if verb in ("build", "test", "run", "package") else None

    if comm in ("rustup", "nextest"):
        return Det("compile", f"{comm} {pos[0] if pos else ''}".strip(), True, comm)

    return None


@dataclass
class Job:
    key: int                              # root pid (negative keys are reserved for future non-process jobs)
    kind: str
    label: str
    finite: bool
    first_seen: float
    started: float                        # wall-clock start of the root process (finite) or of the busy period (daemon)
    project: str | None = None            # basename of the root's cwd, when it is not $HOME
    members: set[int] = field(default_factory=set)
    cpu: float = 0.0                      # subtree CPU %, summed (100 = one core)
    peak_cpu: float = 0.0
    rss: int = 0
    stage: str = ""
    read_rate: float = 0.0
    write_rate: float = 0.0
    read_total: int = 0
    write_total: int = 0
    last_busy: float = 0.0
    busy_since: float = 0.0
    io_prev: tuple[float, int, int] | None = None
    est_total: float | None = None        # median duration of the same job on previous runs, from history
    est_runs: int = 0
    note: str = ""
    visible: bool = True                  # False while a finite tool sits idle (it is still tracked until it exits)

    @property
    def name(self) -> str:
        return f"{self.label} · {self.project}" if self.project else self.label

    @property
    def hist_key(self) -> str:
        return f"{self.kind}:{self.label}:{self.project or ''}"

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.started)

    @property
    def frac(self) -> float | None:
        """Estimated completion 0..0.98 from history, None when this job has never run before."""
        if not self.est_total:
            return None
        return min(0.98, self.elapsed / self.est_total)

    @property
    def eta(self) -> float | None:
        if not self.est_total:
            return None
        return self.est_total - self.elapsed          # negative = over the estimate


@dataclass
class Busy:
    pid: int
    name: str
    cpu: float
    rss: int
    since: float


class History:
    """Durations of finished jobs, keyed by kind:label:project, so the next run of the same thing gets a bar."""

    KEEP = 6
    MIN_SECONDS = 5.0

    def __init__(self, path: Path = HISTORY_FILE) -> None:
        self.path = path
        self.data: dict[str, list[float]] = {}
        self.dirty = False
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                self.data = {k: [float(x) for x in v][-self.KEEP:] for k, v in raw.items() if isinstance(v, list)}
        except (OSError, ValueError):
            self.data = {}

    def estimate(self, key: str) -> tuple[float | None, int]:
        runs = self.data.get(key) or []
        if not runs:
            return None, 0
        return statistics.median(runs), len(runs)

    def record(self, key: str, seconds: float) -> None:
        if seconds < self.MIN_SECONDS:
            return
        runs = self.data.setdefault(key, [])
        runs.append(round(seconds, 1))
        del runs[: -self.KEEP]
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=0, sort_keys=True))
            os.replace(tmp, self.path)
        except OSError:
            pass
        self.dirty = False


def _boot_time() -> float:
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except OSError:
        pass
    return time.time() - time.monotonic()


BOOT = _boot_time()


def proc_started(row: dict) -> float:
    """Wall-clock start of a process from its /proc start ticks."""
    return BOOT + row["start"] / CLK_TCK


def read_io(pid: int) -> tuple[int, int] | None:
    try:
        r = w = 0
        with open(f"/proc/{pid}/io", "rb") as fh:
            for line in fh:
                if line.startswith(b"rchar:"):
                    r = int(line.split()[1])
                elif line.startswith(b"wchar:"):
                    w = int(line.split()[1])
        return r, w
    except (OSError, ValueError):
        return None


def proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


class JobTracker:
    """Scan the shared /proc snapshot, classify processes, group them into jobs by process tree, measure them."""

    QUIET_AFTER = 10.0         # seconds of an idle tree before a daemon job (Metro, tsc --watch) counts as finished
    QUIET_FINITE = 30.0        # a finite tool may pause this long (locks, network, IO waits) and still be shown
    BUSY_CPU = 5.0             # subtree CPU % that counts as "working"
    BUSY_IO = 256 * 1024       # …or bytes/s of read+write (a `cp` or `tar` barely uses CPU)
    IO_MEMBERS_CAP = 160       # read /proc/pid/io for at most this many processes per job

    def __init__(self, busy: bool = True, busy_cpu: float = 40.0, busy_after: float = 8.0,
                 busy_ignore: set[str] | None = None, history: History | None = None) -> None:
        self.me = os.getpid()
        self.active: dict[int, Job] = {}
        self.finished: deque[tuple[str, str, float, str]] = deque(maxlen=12)       # (hh:mm:ss, name, seconds, kind)
        self.history = history if history is not None else History()
        self.dets: dict[tuple[int, int], Det | None] = {}                          # (pid, start) -> classification
        self.cwds: dict[int, str | None] = {}
        self.busy_on = busy
        self.busy_cpu = busy_cpu
        self.busy_after = busy_after
        self.busy_ignore = BUSY_IGNORE | (busy_ignore or set())
        self.busy_seen: dict[int, float] = {}                                       # pid -> when it first went hot
        self.busy: list[Busy] = []
        self.ancestors_of_me = self._ancestors(self.me)
        self.t0 = time.monotonic()

    # -- helpers ------------------------------------------------------------------------------------------------
    @staticmethod
    def _ancestors(pid: int) -> set[int]:
        out: set[int] = set()
        for _ in range(64):
            try:
                with open(f"/proc/{pid}/stat", "rb") as fh:
                    data = fh.read()
                ppid = int(data[data.rfind(b")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                break
            out.add(pid)
            if ppid <= 1:
                break
            pid = ppid
        return out

    def _det(self, row: dict) -> Det | None:
        key = (row["pid"], row["start"])
        if key in self.dets:
            return self.dets[key]
        comm = row["name"]
        det = None
        if not row["kernel"] and row["st"] != "Z":
            known = comm in FINITE or comm in CMDLINE_TOOLS
            if not known and (comm in ODD_COMMS or comm.startswith(("npm ", "yarn ", "pnpm ", "bun ")) or len(comm) >= 15):
                # thread names and rewritten titles: fall back to the program in argv[0]
                cmd = SCANNER.cmdline(row)
                argv0 = os.path.basename(cmd.split(" ", 1)[0]) if cmd and not cmd.startswith("[") else ""
                if argv0 in FINITE or argv0 in CMDLINE_TOOLS:
                    comm, known = argv0, True
            if known:
                cmd = SCANNER.cmdline(row)
                args = cmd.split(" ")[1:] if cmd and not cmd.startswith("[") else []
                try:
                    det = classify(comm, args)
                except Exception:
                    det = None
        self.dets[key] = det
        return det

    # -- scan -------------------------------------------------------------------------------------------------
    def scan(self, dt: float) -> None:
        snap: Snapshot = SCANNER.snapshot(0.5)
        now = time.monotonic()
        rows = snap.by_pid
        children: dict[int, list[int]] = {}
        for row in snap.rows:
            children.setdefault(row["ppid"], []).append(row["pid"])

        detected: dict[int, Det] = {}
        for row in snap.rows:
            if row["pid"] == self.me or row["pid"] in self.ancestors_of_me:
                continue
            det = self._det(row)
            if det is not None:
                detected[row["pid"]] = det

        # root of each detected process = its top-most detected ancestor
        roots: dict[int, int] = {}
        for pid in detected:
            root, cur, hops = pid, pid, 0
            while hops < 64:
                ppid = rows.get(cur, {}).get("ppid", 0)
                if ppid <= 1 or ppid not in rows:
                    break
                if ppid in detected:
                    root = ppid
                cur = ppid
                hops += 1
            roots[pid] = root

        # orphan build daemons (gradle/kotlin daemons that outlived or were never children of the client) join the
        # gradle/expo job that is running, if there is exactly one candidate
        groups: dict[int, set[int]] = {}
        for pid, root in roots.items():
            groups.setdefault(root, set()).add(pid)
        gradle_roots = [r for r in groups if detected[r].kind in ("gradle", "expo") and detected[r].finite]
        for root in list(groups):
            d = detected[root]
            if not d.finite and d.kind == "gradle" and len(gradle_roots) == 1 and gradle_roots[0] != root:
                target = gradle_roots[0]
                groups[target] |= groups.pop(root)
                for pid in list(roots):
                    if roots[pid] == root:
                        roots[pid] = target

        seen: set[int] = set()
        in_jobs: set[int] = set()
        for root, dets_in in groups.items():
            root_det = detected[root]
            # members: the whole subtree under the root, plus any detected processes merged in with their subtrees
            members: set[int] = set()
            stack = [root] + [p for p in dets_in if p != root]
            while stack and len(members) < 5000:
                p = stack.pop()
                if p in members:
                    continue
                members.add(p)
                stack.extend(children.get(p, ()))
            cpu = sum(rows[p]["cpu"] for p in members if p in rows)
            rss = sum(rows[p]["rss"] for p in members if p in rows)
            finite = any(detected[p].finite for p in dets_in)
            busy_now = cpu >= self.BUSY_CPU

            job = self.active.get(root)
            if job is None:
                if not finite and not busy_now:
                    continue                              # idle daemon: not a job until it works
                if finite and not busy_now and time.time() - proc_started(rows[root]) > self.QUIET_FINITE:
                    continue                              # an old idle tool (a `cat` holding a pipe, a `go run` server): wait until it works
                if root not in self.cwds:
                    cwd = proc_cwd(root)
                    self.cwds[root] = cwd
                cwd = self.cwds[root]
                real = os.path.realpath(cwd) if cwd else ""
                project = os.path.basename(real.rstrip("/")) if real and real not in (HOME_REAL, HOME, "/") else None
                started = proc_started(rows[root]) if finite else time.time()
                job = Job(root, root_det.kind, root_det.label, finite, now, started, project)
                job.est_total, job.est_runs = self.history.estimate(job.hist_key)
                self.active[root] = job
            elif not job.finite and finite:
                job.finite, job.label, job.kind = True, root_det.label, root_det.kind
            job.members = members
            job.cpu = cpu
            job.peak_cpu = max(job.peak_cpu, cpu)
            job.rss = rss
            # stage: the hottest notable member that is not the root itself
            best, best_cpu = "", 0.0
            for p in members:
                if p == root or p not in rows:
                    continue
                r = rows[p]
                stage = detected[p].stage if p in detected else STAGE_OF.get(r["name"], "")
                if stage and r["cpu"] > best_cpu:
                    best, best_cpu = stage, r["cpu"]
            if best_cpu < 2.0 and job.stage and best != job.stage:
                pass                                     # keep the previous stage through a short lull
            elif best:
                job.stage = best
            # io: summed rchar/wchar over the tree, as rates
            r_tot = w_tot = 0
            for p in list(members)[: self.IO_MEMBERS_CAP]:
                io = read_io(p)
                if io:
                    r_tot += io[0]
                    w_tot += io[1]
            if job.io_prev is not None and now > job.io_prev[0]:
                span = now - job.io_prev[0]
                rr = max(0.0, (r_tot - job.io_prev[1]) / span)
                wr = max(0.0, (w_tot - job.io_prev[2]) / span)
                job.read_rate = job.read_rate * 0.6 + rr * 0.4
                job.write_rate = job.write_rate * 0.6 + wr * 0.4
            job.io_prev = (now, r_tot, w_tot)
            job.read_total, job.write_total = r_tot, w_tot
            if busy_now or job.read_rate + job.write_rate >= self.BUSY_IO or (job.finite and not job.last_busy):
                job.last_busy = now                       # a fresh finite job counts as working until proven idle
                if not job.busy_since:
                    job.busy_since = now
            job.visible = now - job.last_busy <= (self.QUIET_FINITE if job.finite else self.QUIET_AFTER)
            seen.add(root)
            in_jobs |= members

        # retire: finite jobs whose root is gone, daemon jobs whose tree went quiet
        for key in list(self.active):
            job = self.active[key]
            if key in seen:
                if job.finite or now - job.last_busy <= self.QUIET_AFTER:
                    continue
                elapsed = now - job.busy_since if job.busy_since else 0.0
            elif key in rows and not job.finite:
                if now - job.last_busy <= self.QUIET_AFTER:
                    continue
                elapsed = now - job.busy_since if job.busy_since else 0.0
            elif job.finite:
                idle_tail = max(0.0, now - job.last_busy) if job.last_busy else 0.0
                elapsed = max(0.0, job.elapsed - idle_tail)          # a server that compiled then sat idle: count the work
            else:
                elapsed = now - job.busy_since if job.busy_since else 0.0
            self.active.pop(key)
            if job.finite and job.elapsed > 3600 and elapsed < job.elapsed * 0.5:
                continue                                  # a long-lived tool that mostly idled is not a finished job
            if job.finite:
                self.history.record(job.hist_key, elapsed)   # daemon bursts (a Metro rebundle) are no basis for an estimate
            if job.finite or elapsed >= 10.0:
                self.finished.appendleft((time.strftime("%H:%M:%S"), job.name, elapsed, job.kind))
        self.history.save()

        # busy catch-all: sustained CPU outside any job
        if self.busy_on:
            hot: list[Busy] = []
            alive: set[int] = set()
            for row in snap.rows:
                pid = row["pid"]
                if pid in in_jobs or pid == self.me or pid in self.ancestors_of_me or row["kernel"]:
                    continue
                name = SCANNER.display_name(row)
                if name in self.busy_ignore or row["name"] in self.busy_ignore:
                    continue
                if row["cpu"] >= self.busy_cpu:
                    alive.add(pid)
                    since = self.busy_seen.setdefault(pid, now)
                    if now - since >= self.busy_after:
                        hot.append(Busy(pid, name, row["cpu"], row["rss"], proc_started(row)))
                elif pid in self.busy_seen and row["cpu"] < self.busy_cpu * 0.5:
                    self.busy_seen.pop(pid, None)
                elif pid in self.busy_seen:
                    alive.add(pid)
                    if now - self.busy_seen[pid] >= self.busy_after:
                        hot.append(Busy(pid, name, row["cpu"], row["rss"], proc_started(row)))
            for pid in list(self.busy_seen):
                if pid not in alive:
                    del self.busy_seen[pid]
            hot.sort(key=lambda b: -b.cpu)
            self.busy = hot
        else:
            self.busy = []

        # forget classifications of processes that are gone
        if len(self.dets) > 4 * max(1, len(rows)):
            self.dets = {k: v for k, v in self.dets.items() if k[0] in rows}
            self.cwds = {k: v for k, v in self.cwds.items() if k in rows}

    def sorted_jobs(self) -> list[Job]:
        """Visible jobs: finite ones first, then working daemons; hottest first inside each group."""
        return sorted((j for j in self.active.values() if j.visible), key=lambda j: (0 if j.finite else 1, -j.cpu, j.name.lower()))
