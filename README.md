# mondash

A lego-style terminal dashboard for Linux, built from small independent monitors. Every monitor
runs on its own or as a brick in a grid you describe in plain text. Written in Python on top of
`rich` and `psutil`; no root, no daemons, a few percent of one core.

Panels: CPU, memory, network, disks, processes (task-manager style, mouse aware), GPU (NVIDIA via
NVML, Intel and AMD via sysfs), temperatures, power/battery, system strip, hardware inventory,
downloads (curl/wget/aria2c/flatpak/rsync/git/scp/pip and Steam, via the bundled `dlwatch`),
any shell command, and an animation panel for GIFs.

## Install

```
git clone https://github.com/leitel98/mondash.git ~/Projects/mondash
pip install --user rich psutil pillow
~/Projects/mondash/install.sh        # symlinks the launchers into ~/.local/bin, adds desktop entries
mondash                              # config appears at ~/.config/mon/dash.toml on first run
```

Everything lives in the cloned folder: the `mon` package, the launchers, `dlwatch`, and a `gifs/`
folder the animation panel lists by default (drop your own `.gif` files there). Requires Python
3.11+ and a terminal with true colour and mouse support (Konsole, kitty, foot, Alacritty, GNOME
Terminal, …). `./install.sh --uninstall` removes the links again.

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

Panel keys: processes `c m p n` or `←→` sort, `↑↓` select, `d` details, `/` filter, `k`/`K` kill,
`u` mine only, `h` kernel threads, `t` divide CPU% by cores;
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
`braille_graph`, `sparkline`, `bar`, `meter`, `kv_line`, `Hist`, `human`, `rate`.

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

## Steam downloads

dlwatch (and therefore the `dl` panel) reads `steamapps/appmanifest_*.acf` in every Steam
library to find installs and updates, then measures progress from the bytes written under
`steamapps/downloading/<appid>` (Steam only rewrites the manifest at checkpoints), averaged over a minute, against
the manifest's BytesToStage total. Entries show the game name, progress, speed, ETA and, once
Steam starts moving files into the game folder, a `committed N%` note. If nothing changes for
10 seconds the entry says `stalled`; Steam pauses downloads while a game is running unless
you allow downloads during gameplay in its settings.

## Portability

Nothing is hard-coded to this machine. Every number is read at run time from `/proc`, `/sys`
(hwmon, cpufreq, drm, power_supply, block, dmi), udev's DMI export (`udevadm info`), `lspci`,
the NVIDIA driver (NVML, falling back to `nvidia-smi`), the amdgpu and i915 sysfs interfaces,
and Steam's own manifest files found through `libraryfolders.vdf`. Panels degrade when a
source is missing: no battery → the power panel shows AC and governor only, no discrete GPU →
the integrated one, no sensors → "no temperature sensors". Requirements are Linux, Python
3.11+, `rich`, `psutil`, and optionally `pillow` for GIF playback. AMD GPU support is written
against the documented sysfs files but was not exercised here (this machine has NVIDIA + Intel).
