# QCY Control for Linux

A GNOME tray app for QCY earbuds — the Linux counterpart of the macOS menu bar app in [`../app`](../app).
Built for **Ubuntu 22.04+ (GNOME)**; tested protocol: **QCY MeloBuds N65**. MeloBuds Pro (A001) support is ported
from the macOS app but untested on Linux.

**Tray menu:** battery (left / right / case), noise control off / noise cancelling / transparency, ANC scene and
level, transparency level 1–7, EQ presets, spatial audio.
**Settings window:** L/R balance, custom 10-band EQ, Dynamic EQ, LDAC and multi-device switches, touch controls.

The icon only shows while the earbuds are connected to the computer.

## Install

```bash
git clone https://github.com/mahbubasif/qcy-control-macos
cd qcy-control-macos/linux
./install.sh
qcy-control &
```

The installer:
- installs Ubuntu packages: `python3-venv python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gnome-shell-extension-appindicator`
- creates a private Python environment in `~/.local/share/qcy-control` with [bleak](https://github.com/hbldh/bleak) ≥ 3.1 (nothing is pip-installed system-wide)
- adds `~/.local/bin/qcy-control`, an app-menu entry and a login autostart entry

Remove it with `./install.sh --uninstall`.

Pair and connect the earbuds normally in **Settings › Bluetooth** first — the app uses that audio connection to know
when to appear, and talks to the earbuds over a separate BLE link (`QCY MeloBuds N65-APP`) that it opens itself.
No pairing is needed for that link.

## Troubleshooting

| | |
|---|---|
| `qcy-control --dump` | connects without the GUI and prints everything the earbuds report |
| `qcy-control --debug` | runs the tray and logs every Bluetooth frame to the terminal |
| No tray icon | the **Ubuntu AppIndicators** extension must be on: `gnome-extensions enable ubuntu-appindicators@ubuntu.com` (log out/in after installing it) |
| "Searching for earbuds…" | open the case / take the buds out; make sure no phone app is holding the BLE link |
| "A setting couldn't be sent" | run with `--debug` and look at the `write … failed` line (large EQ writes need a BLE MTU above ~150) |

## Notes

- LDAC doesn't do anything for Linux audio unless your PipeWire setup supports it; turning LDAC on also disables
  multi-device and spatial audio on the N65. Changing LDAC or multi-device restarts the earbuds.
- The case battery is only live while a bud sits in the case; otherwise it's the last value the buds heard.
- Protocol details are in the main [README](../README.md#melobuds-n65-jieli-chip).

## Development

```bash
cd linux
python3 -m unittest discover -s tests       # protocol tests (real N65 frames), no Bluetooth needed
python3 -m qcy --dump                        # needs: pip install "bleak>=3.1" (works on macOS too)
```

Layout: `qcy/protocol.py` (framing, parsing, model data — pure Python), `qcy/device.py` (bleak, background
asyncio thread), `qcy/tray.py` (GTK 3 + AppIndicator UI).
