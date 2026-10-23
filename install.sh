#!/bin/sh
set -eu
src=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
/usr/bin/python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw"
command -v owly >/dev/null || { echo 'Owly CLI is missing from PATH.' >&2; exit 1; }
app="$HOME/.local/share/owly-native"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$app" "$apps"
if [ -f "$app/owly_gui.py" ]; then
    cp -p "$app/owly_gui.py" "$app/owly_gui.py.backup-$(date +%Y%m%d-%H%M%S)"
fi
install -m 755 "$src/owly_gui.py" "$app/owly_gui.py"
# Desktop Exec syntax requires escaping backslashes, quotes, backticks, dollars and percent signs.
/usr/bin/python3 - "$app/owly_gui.py" "$apps/owly-native.desktop" <<'PY'
import pathlib, sys
path = sys.argv[1]
for old, new in [('\\', '\\\\'), ('"', '\\"'), ('`', '\\`'), ('$', '\\$'), ('%', '%%')]:
    path = path.replace(old, new)
pathlib.Path(sys.argv[2]).write_text('[Desktop Entry]\nType=Application\nName=Owly AirDrop\nComment=Send and receive with the installed Owly CLI\nExec=/usr/bin/python3 "' + path + '"\nIcon=network-wireless\nTerminal=false\nCategories=Network;FileTransfer;\nStartupNotify=true\n')
PY
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$apps" || true
fi
printf '%s\n' 'Installed. Find “Owly AirDrop” in Applications.' 'Your existing Zenity script has been left in place.'
