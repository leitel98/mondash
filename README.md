# mondash

A lego-style terminal dashboard for Linux, built from small independent monitors. Every monitor
runs on its own or as a brick in a grid you describe in plain text. Written in Python on top of
`rich` and `psutil`; no root, no daemons, a few percent of one core.

Panels: CPU, memory, network, disks, processes (task-manager style, mouse aware), GPU (NVIDIA via
NVML, Intel and AMD via sysfs), temperatures, power/battery, system strip, hardware inventory,
downloads (curl/wget/aria2c/flatpak/rsync/git/scp/pip, yarn/npm/pnpm/bun installs, go mod
  download, Android sdkmanager, gradle, podman/docker pull and Steam, via the bundled `dlwatch`),
jobs (everything being processed: builds, compilers, bundlers, test runs, encoders, archivers,
  copies, package and system installs — see below), any shell command, and an animation panel for GIFs.

## Install

Pick one:

```
pipx install mondash                 # or: uv tool install mondash
```

That puts `mondash`, `dlwatch` and the single monitors (`cpumon`, `memmon`, `netmon`, `diskmon`, `procmon`,
`gpumon`, `tempmon`, `powermon`, `sysmon`, `dlmon`, `cmdmon`, `hwmon`, `gifmon`, `jobsmon`) on your PATH.
Add `mondash[gif]` to get GIF playback in the animation panel (needs Pillow).

Or download a single-file binary from the [releases page](https://github.com/leitel98/mondash/releases)
(`mondash-linux-x86_64` or `mondash-linux-aarch64`), make it executable, and run it. Everything is in that one
file: `mondash` is the dashboard, `mondash cpu` a single monitor, `mondash dlwatch` the download watcher.

From a checkout:

```
git clone https://github.com/leitel98/mondash.git && cd mondash
./install.sh                         # pipx / uv / pip --user, whichever you have, plus desktop entries
python3 -m mon                       # or run it straight from the folder (needs rich and psutil)
```

The config appears at `~/.config/mon/dash.toml` on first run; your own `.gif` files go in `~/.config/mon/gifs`.
Requires Linux, Python 3.11+ and a terminal with true colour and mouse support (Konsole, kitty, foot, Alacritty,
GNOME Terminal, Ptyxis, …). `./install.sh --uninstall` removes it again.

## Mouse

Click a panel to focus it. Click a panel's title bar to zoom it (again to restore). In the
process table: click a column header to sort by it, click a row to select it, click the
selected row again for details, right-click a row to kill it (then `y`), wheel to scroll.
Press `m` to hand the mouse back to the terminal when you want to select text
(`mouse = false` in dash.toml or `--no-mouse` to start that way).

## Keys (dashboard)

| key | action |
|---|---|
| `q` | quit |
| `Tab` / `Shift-Tab` / `1-9` | focus a panel (focused panel gets the other keys) |
| `Enter` / `f` | zoom the focused panel, again to restore |
| `l` / `L` | next / previous layout |
| `space` | pause |
| `+` / `-` | faster / slower |
| `r` | reload dash.toml |
| `g` | hide / show the animation panel (a neighbour absorbs its space) |
| `H` | hardware inventory full screen, in any layout (`Esc` closes) |
| `m` | toggle mouse capture |
| `?` | help, including the focused panels' own keys |

The bottom bar always shows the global keys plus the focused panel's keys (`hints = false` hides it).

Panel keys: processes `c m p n` or `←→` sort, `↑↓` select, `d` details, `/` (or click the filter box) filter,
`k`/`K` kill, `u` mine only, `h` kernel threads, `t` divide CPU% by cores; jobs `b` busy list, `c` clear finished;
downloads `c` clear finished;
network `n`/`p` interface, `a` autoscale; cpu `g` core details; gpu `p` process list;
disk `i` I/O section; temps `c` per-core; cmd `R` run now.

## Layouts

Layouts are ASCII grids in `dash.toml`, like CSS `grid-template-areas`:

```toml
[layouts.mine]
rows = ["3", 2, 3]        # "3" = exactly 3 lines, bare numbers = weights of the rest
cols = [2, 1]
grid = """
sys  sys
cpu  gpu
proc dl
"""
```

A name used in a grid is a panel. `[panels.NAME]` sets its options; `type = "cmd"` with
`command = "..."` turns any shell command into a panel. Theme colours, bar and graph
characters, thresholds and the box style live under `[theme]`.

## Adding a panel

Subclass `mon.core.Panel` in a new file under `mon/panels/`, implement `sample(dt)` and
`render(width, height)`, register it in `mon/panels/__init__.py`. Helpers in `mon.core`:
`braille_graph`, `sparkline`, `bar`, `meter`, `kv_line`, `Hist`, `human`, `rate`; readers for `/proc` and `/sys`
live in `mon.host`. `python3 -m unittest discover -s mon/tests -t .` renders every panel at several sizes and
composes every layout, so a new panel that overflows its box fails the suite.

## Cost

The screen is drawn by a row-diff renderer (`mon.core.Screen`): every frame is rendered to
per-row strings and only rows that changed are written, so a 1 Hz dashboard sends a few KB/s
and an animation only rewrites its own rows. Panels whose `sample()` returns False are not
repainted at all.

The dashboard reads `/proc/PID/stat` directly (one read per process, shared by the process
and downloads panels), talks to the NVIDIA driver through NVML instead of forking
`nvidia-smi`, reads the hwmon sensors once per tick for all panels, and only redraws panels
whose data changed. Measured at 120x40 with all panels: roughly 5–8% of one core while a
game was running. To spend less, raise `refresh` or a panel's `interval` in dash.toml, or
press `-` while it runs.

## Jobs

The `jobs` panel (and `jobsmon` on its own) answers "what is this machine working on right now?".
It recognises work by process name and command line — gradle/kotlin daemons, javac, gcc/clang/ld,
rustc/cargo, go build, tsc/esbuild/webpack/vite, jest/vitest/pytest, expo/eas, make/ninja/cmake,
docker/podman build, ffmpeg and friends, tar/zip/xz/zstd, cp/mv/dd, borg/restic/rclone,
dnf/rpm-ostree/apt/pacman, mypy/ruff, pg_dump, terraform… — and groups everything by process tree,
so `expo run:android` → gradle → kotlin daemon → ninja → clang shows as one row with the hottest
member as its stage (`expo run:android · nf-mobile · C++`). Each row has the tree's CPU, memory,
read/write rates and elapsed time. Downloads stay in `dl`; pure downloaders are never jobs.

Progress bars come from history: the first run of a job shows a sweeping bar and its elapsed time;
every finished run is remembered in `~/.cache/mon/jobs.json` (per kind, label and project folder,
last 6 durations) and the next run shows `≈42% ETA 9m12s` against the median — turning yellow/red
when it goes over. Servers and daemons (`expo start`, `tsc --watch`, gradle daemons) are jobs only
while their tree is busy; a finite tool that sits idle for 30 s (a `cat` holding a pipe, a `go run`
server) hides until it works again. Anything else that holds more than 40 % of a core for 8 s and
is not a browser, compositor, player, terminal or VM is listed under `busy:` (`b` toggles it,
`busy_cpu` / `busy_after` / `busy_ignore` tune it). `c` clears the finished list.

## Steam downloads

dlwatch (and therefore the `dl` panel) reads `steamapps/appmanifest_*.acf` in every Steam
library to find installs and updates, then measures progress from the bytes written under
`steamapps/downloading/<appid>` (Steam only rewrites the manifest at checkpoints), averaged over a minute, against
the manifest's BytesToStage total. Entries show the game name, progress, speed, ETA and, once
Steam starts moving files into the game folder, a `committed N%` note. If nothing changes for
10 seconds the entry says `stalled`; Steam pauses downloads while a game is running unless
you allow downloads during gameplay in its settings.

## Making it recognise your programs

The jobs and downloads panels know a few hundred tools by name (see `mon/jobs.py` and `mon/dlwatch.py`), but your
machine runs things mine does not. `mondash --doctor` prints what the panels can see on this machine (sensors,
GPU backend, battery, Steam libraries, terminal) and, more usefully, which running processes are burning CPU
without being recognised. Teach it in `dash.toml`:

```toml
[panels.jobs]
tools = { myencoder = "media", buildthing = "build" }   # process name → kind
ignore = ["cat"]                                        # never a job
busy_ignore = ["blender"]                               # never in the busy list

[panels.dl]
tools = ["axel", "lftp"]                                # extra downloaders (they get a bar when an output file is found)
ignore = ["rsync"]
```

Kinds for jobs: `compile link build test lint container media archive copy backup system python vcs ml db vm iac script`.
Process names are what `/proc` shows (the first 15 characters of the executable name). If something common is
missing, open an issue with the `--doctor` output and it goes into the built-in tables.

## Portability

Nothing is hard-coded to this machine. Every number is read at run time from `/proc`, `/sys`
(hwmon, cpufreq, drm, power_supply, block, dmi), udev's DMI export (`udevadm info`), `lspci`,
the NVIDIA driver (NVML, falling back to `nvidia-smi`), the amdgpu and i915 sysfs interfaces,
and Steam's own manifest files found through `libraryfolders.vdf` (native, Flatpak and Snap installs). Panels
degrade when a source is missing: no battery → the power panel shows AC and governor only, no discrete GPU →
the integrated one, no sensors → "no temperature sensors", no default route → the busiest interface. A panel that
throws keeps the dashboard running and shows the error in its own frame until the next good sample. Requirements
are Linux, Python 3.11+, `rich`, `psutil`, and optionally `pillow` for GIF playback. AMD GPU support is written
against the documented sysfs files but was not exercised here (this machine has NVIDIA + Intel).

## Development and releases

```
git clone https://github.com/leitel98/mondash.git && cd mondash
pip install -e ".[gif]"                                # editable install: edits take effect immediately
python3 -m unittest discover -s mon/tests -t .         # classifier tests + headless render of every panel and layout
```

Pushing a tag `vX.Y.Z` runs the release workflow: it builds the wheel and sdist, a one-file binary for x86_64 and
aarch64 (built on Ubuntu 22.04, so they run on any distro from 2022 on), and attaches them to a GitHub release.
`pypi.yml` publishes the same tag to PyPI once a trusted publisher is configured for this repository on pypi.org
and the repository variable `PYPI_PUBLISH` is set to `true`.
The version lives in `mon/__init__.py`.
