"""Entry point:  python3 -m qcy [--dump] [--debug]"""
from __future__ import annotations

import argparse
import logging
import os
import threading
import time

from . import __version__
from . import protocol as P


def describe(st: P.State) -> str:
    def b(x: P.Battery) -> str:
        return ("–" if not x.known else f"{x.level}%") + (" (charging)" if x.charging else "")

    lines = [f"{st.name} — {st.status}"]
    if st.proto is None:
        return "\n".join(lines)
    caps = st.caps
    scene = next((s.name for s in caps.scenes if s.id == st.anc_scene), f"scene {st.anc_scene}")
    mode = {P.AncMode.OFF: "off", P.AncMode.ANC: f"ANC, {scene}, level {st.anc_level}",
            P.AncMode.TRANSPARENCY: f"transparency, level {st.trans_level}"}[st.anc_mode]
    lines += [
        f"protocol   {st.proto.value}   firmware {st.firmware or '?'}",
        f"battery    left {b(st.left)}, right {b(st.right)}, case {b(st.case)}"
        + ("" if st.case_is_live else " (last known)"),
        f"noise      {mode}",
        f"balance    {st.balance}",
    ]
    if caps.eq == "n65":
        name = "Custom" if st.eq_index == P.N65_CUSTOM_ID else next(
            (p.name for p in P.N65_PRESETS if p.id == st.eq_index), f"#{st.eq_index}")
        lines.append(f"eq         {name}" + (f" {st.eq}" if st.eq_index == P.N65_CUSTOM_ID else "")
                     + f", dynamic EQ {'on' if st.dynamic_eq else 'off'}")
        lines.append(f"spatial    {({1: 'off', 2: 'fixed', 3: 'head tracking'}).get(st.spatial, st.spatial)}")
        lines.append(f"ldac       {'on' if st.ldac else 'off'}   multi-device {'on' if st.multi_device else 'off'}")
    else:
        lines.append(f"eq         {st.eq}   gaming mode {'on' if st.game_mode else 'off'}")
    funcs = dict(caps.key_functions)
    keys = ", ".join(f"{name}: {funcs.get(st.keymap.get(slot, 0), hex(st.keymap.get(slot, 0)))}"
                     for slot, name in caps.key_slots)
    lines.append(f"touch      {keys}")
    return "\n".join(lines)


def dump(timeout: float) -> int:
    from .device import QCYDevice

    latest: list[P.State] = []
    ready = threading.Event()

    def on_change(st: P.State) -> None:
        latest[:] = [st]
        if st.connected and st.firmware and st.keymap:
            ready.set()

    dev = QCYDevice(on_change)
    dev.start()
    ok = ready.wait(timeout)
    if ok:
        time.sleep(2)  # let the remaining replies arrive
    text = describe(latest[0]) if latest else "No state received."
    dev.stop()
    print(text)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="qcy-control", description="Tray app for QCY earbuds (MeloBuds N65, Pro).")
    ap.add_argument("--dump", action="store_true", help="connect, print the earbuds' current settings and exit (no GUI)")
    ap.add_argument("--timeout", type=float, default=40, help="seconds to wait in --dump (default 40)")
    ap.add_argument("--debug", action="store_true", help="log every Bluetooth frame (or set QCY_DEBUG=1)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args()

    debug = args.debug or os.environ.get("QCY_DEBUG") == "1"
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if debug:
        logging.getLogger("bleak").setLevel(logging.INFO)

    if args.dump:
        return dump(args.timeout)
    from .tray import run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
