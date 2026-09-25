#!/usr/bin/env bash
# Install mondash from this checkout for the current user, plus desktop entries. Safe to re-run after `git pull`.
#   ./install.sh              install with pipx, uv or pip --user (whichever is available, in that order)
#   ./install.sh --uninstall  remove it again
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

if [[ "${1:-}" == "--uninstall" ]]; then
  if command -v pipx >/dev/null && pipx list --short 2>/dev/null | grep -q '^mondash '; then pipx uninstall mondash
  elif command -v uv >/dev/null && uv tool list 2>/dev/null | grep -q '^mondash '; then uv tool uninstall mondash
  else python3 -m pip uninstall -y mondash 2>/dev/null || true; fi
  rm -f "$APPS/mondash.desktop" "$APPS/dlwatch.desktop"
  echo "removed mondash and its desktop entries"; exit 0
fi

# symlinks left by the pre-1.0 installer would make pip write its launchers through them into the checkout
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
for l in mondash dlwatch cpumon memmon netmon diskmon procmon gpumon tempmon powermon sysmon dlmon cmdmon hwmon gifmon jobsmon; do
  if [[ -L "$BIN_DIR/$l" && "$(readlink -f "$BIN_DIR/$l")" == "$HERE/"* ]]; then rm -f "$BIN_DIR/$l"; fi
done

if command -v pipx >/dev/null; then
  pipx install --force "$HERE[gif]"
elif command -v uv >/dev/null; then
  uv tool install --force "$HERE[gif]"
else
  python3 -m pip install --user --upgrade "$HERE[gif]" || {
    echo "pip could not install (externally managed Python?). Install pipx (dnf/apt install pipx) and re-run." >&2; exit 1; }
fi

BIN="$(command -v mondash || true)"
if [[ -z "$BIN" ]]; then
  BIN="$HOME/.local/bin/mondash"
  echo "note: $HOME/.local/bin is not on your PATH yet; add it or log out and in again"
fi
mkdir -p "$APPS"
cat > "$APPS/mondash.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=System Dashboard (mondash)
Comment=Lego terminal dashboard: cpu, gpu, memory, network, disks, processes, temps, power, downloads, jobs
Exec=$BIN
Icon=utilities-system-monitor
Terminal=true
Categories=Utility;System;Monitor;
Keywords=monitor;btop;dashboard;cpu;gpu;
DESK
cat > "$APPS/dlwatch.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Download Watch
Comment=See what is downloading right now, with progress bars
Exec=$BIN dlwatch
Icon=folder-download
Terminal=true
Categories=Utility;System;Monitor;
Keywords=download;progress;curl;wget;steam;
DESK
echo "installed: $BIN (and cpumon, memmon, netmon, … dlwatch next to it); desktop entries in $APPS"
