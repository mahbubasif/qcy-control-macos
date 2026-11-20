#!/usr/bin/env bash
# Install QCY Control for the current user (Ubuntu 22.04+ with GNOME).
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove everything this script added (system packages are left alone)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
PREFIX="$DATA/qcy-control"
BIN="$HOME/.local/bin/qcy-control"
DESKTOP="$DATA/applications/qcy-control.desktop"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/qcy-control.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    pkill -f "python -m qcy" 2>/dev/null || true
    rm -rf "$PREFIX" "$BIN" "$DESKTOP" "$AUTOSTART"
    echo "QCY Control removed."
    exit 0
fi

echo "==> Installing system packages (asks for your password)"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-gi gir1.2-gtk-3.0 \
    gir1.2-ayatanaappindicator3-0.1 gnome-shell-extension-appindicator

echo "==> Creating a private Python environment in $PREFIX"
mkdir -p "$PREFIX"
# --system-site-packages: use Ubuntu's PyGObject (GTK bindings) instead of building it with pip
python3 -m venv --system-site-packages "$PREFIX/venv"
"$PREFIX/venv/bin/python" -m pip install --quiet --upgrade pip
"$PREFIX/venv/bin/python" -m pip install --quiet "bleak>=3.1,<4"

echo "==> Copying the app"
rm -rf "$PREFIX/qcy"
cp -r "$HERE/qcy" "$PREFIX/qcy"
find "$PREFIX/qcy" -name __pycache__ -prune -exec rm -rf {} +

mkdir -p "$(dirname "$BIN")"
cat > "$BIN" <<EOF
#!/bin/sh
PYTHONPATH="$PREFIX" exec "$PREFIX/venv/bin/python" -m qcy "\$@"
EOF
chmod +x "$BIN"

entry() {
    cat <<EOF
[Desktop Entry]
Type=Application
Name=QCY Control
Comment=Noise cancelling, EQ and battery for QCY earbuds
Exec=$BIN
Icon=audio-headphones
Terminal=false
Categories=AudioVideo;Audio;Settings;
StartupNotify=false
EOF
}
mkdir -p "$(dirname "$DESKTOP")" "$(dirname "$AUTOSTART")"
entry > "$DESKTOP"
{ entry; echo "X-GNOME-Autostart-enabled=true"; echo "X-GNOME-Autostart-Delay=5"; } > "$AUTOSTART"

echo
echo "Installed. It starts automatically when you log in."
if command -v gnome-extensions >/dev/null 2>&1 && ! gnome-extensions list --enabled 2>/dev/null | grep -qi appindicator; then
    echo
    echo "!! The AppIndicator GNOME extension isn't enabled, so the tray icon won't show."
    echo "   Enable it with:  gnome-extensions enable ubuntu-appindicators@ubuntu.com"
    echo "   (or log out and back in first if it was just installed)"
fi
echo
echo "Start it now:        qcy-control &"
echo "Check the earbuds:   qcy-control --dump"
echo "Troubleshooting:     qcy-control --debug"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
    echo
    echo "Note: ~/.local/bin isn't on your PATH yet; log out and back in, or run $BIN directly." ;;
esac
