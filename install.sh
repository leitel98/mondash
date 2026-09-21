#!/usr/bin/env bash
# Symlink the launchers into ~/.local/bin and add desktop entries. Safe to re-run. `./install.sh --uninstall` removes them.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
LAUNCHERS=(mondash dlwatch cpumon memmon netmon diskmon procmon gpumon tempmon powermon sysmon dlmon cmdmon hwmon gifmon)

if [[ "${1:-}" == "--uninstall" ]]; then
  for l in "${LAUNCHERS[@]}"; do [[ -L "$BIN/$l" ]] && rm -f "$BIN/$l"; done
  rm -f "$APPS/mondash.desktop" "$APPS/dlwatch.desktop"
  echo "removed launchers from $BIN and desktop entries from $APPS"; exit 0
fi

python3 - <<'PY' || { echo "missing python packages: pip install --user rich psutil pillow"; exit 1; }
import rich, psutil
PY
mkdir -p "$BIN" "$APPS"
for l in "${LAUNCHERS[@]}"; do ln -sfn "$HERE/$l" "$BIN/$l"; done
cat > "$APPS/mondash.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=System Dashboard (mondash)
Comment=Lego terminal dashboard: cpu, gpu, memory, network, disks, processes, temps, power, downloads
Exec=$HERE/mondash
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
Exec=$HERE/dlwatch
Icon=folder-download
Terminal=true
Categories=Utility;System;Monitor;
Keywords=download;progress;curl;wget;steam;
DESK
echo "linked ${#LAUNCHERS[@]} launchers into $BIN and wrote desktop entries to $APPS"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: $BIN is not on your PATH";; esac
