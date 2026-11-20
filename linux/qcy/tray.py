"""GNOME tray UI (AppIndicator menu + settings window). GTK runs on the main thread; Bluetooth in qcy.device."""
from __future__ import annotations

import fcntl
import importlib
import os
import signal
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from . import protocol as P  # noqa: E402
from .device import QCYDevice  # noqa: E402

ICON = "audio-headphones-symbolic"
SPATIAL = [(1, "Off"), (2, "Fixed"), (3, "Head tracking")]


def _load_indicator():
    """Ubuntu ships Ayatana AppIndicator; older/other distros the original AppIndicator3."""
    for name in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(name, "0.1")
            return importlib.import_module(f"gi.repository.{name}")
        except (ValueError, ImportError):
            continue
    return None


def battery_text(st: P.State) -> str:
    def b(x: P.Battery) -> str:
        return "–" if not x.known else f"{x.level}%" + (" ⚡" if x.charging else "")
    case = b(st.case) + ("" if st.case_is_live or not st.case.known else " (last known)")
    return f"L {b(st.left)}    R {b(st.right)}    Case {case}"


def scene_entries(caps: P.Caps) -> list[tuple[tuple[int, int], str]]:
    out = []
    for s in caps.scenes:
        if s.levels > 1:
            out += [((s.id, lvl), f"{s.name} {lvl}") for lvl in range(1, s.levels + 1)]
        else:
            out.append(((s.id, 1), s.name))
    return out


class App:
    def __init__(self, ai):
        self.ai = ai
        self.state = P.State()
        self.window: SettingsWindow | None = None
        self.updating = False
        self.menu_proto: object = object()  # proto the current menu was built for
        self.ind = ai.Indicator.new("qcy-control", ICON, ai.IndicatorCategory.HARDWARE)
        self.ind.set_title("QCY Control")
        self.ind.set_status(ai.IndicatorStatus.PASSIVE)
        self.dev = QCYDevice(lambda st: GLib.idle_add(self._on_state, st))
        self._build_menu(self.state)
        self._update_menu(self.state)

    # ---------------------------------------------------------------- state

    def _on_state(self, st: P.State) -> bool:
        self.state = st
        if st.proto != self.menu_proto:
            self._build_menu(st)
        self._update_menu(st)
        if self.window is not None:
            self.window.update(st)
        visible = st.audio_connected or st.connected
        self.ind.set_status(self.ai.IndicatorStatus.ACTIVE if visible else self.ai.IndicatorStatus.PASSIVE)
        if not visible and self.window is not None:
            self.window.hide()
        return False  # one-shot idle callback

    # ---------------------------------------------------------------- menu

    def _item(self, menu: Gtk.Menu, label: str, on_activate=None, sensitive=True) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(sensitive)
        if on_activate:
            item.connect("activate", lambda _w: on_activate())
        menu.append(item)
        return item

    def _radio(self, menu: Gtk.Menu, group, label: str, on_pick) -> Gtk.RadioMenuItem:
        item = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
        item.connect("toggled", lambda w: None if self.updating or not w.get_active() else on_pick())
        menu.append(item)
        return item

    def _submenu(self, menu: Gtk.Menu, label: str) -> tuple[Gtk.MenuItem, Gtk.Menu]:
        parent, sub = Gtk.MenuItem(label=label), Gtk.Menu()
        parent.set_submenu(sub)
        menu.append(parent)
        return parent, sub

    def _radios(self, menu: Gtk.Menu, entries, on_pick) -> dict:
        items, group = {}, None
        for key, label in entries:
            group = items[key] = self._radio(menu, group, label, lambda k=key: on_pick(k))
        return items

    def _build_menu(self, st: P.State) -> None:
        self.updating = True
        menu = Gtk.Menu()
        self.m_name = self._item(menu, st.name, sensitive=False)
        self.m_status = self._item(menu, st.status, sensitive=False)
        self.m_battery = self._item(menu, battery_text(st), sensitive=False)
        self.m_controls: list[Gtk.MenuItem] = []
        self.m_mode, self.m_scene, self.m_trans, self.m_eq, self.m_spatial = {}, {}, {}, {}, {}

        if st.proto is not None:
            caps, dev = st.caps, self.dev
            menu.append(Gtk.SeparatorMenuItem())
            self.m_mode = self._radios(menu, [(P.AncMode.OFF, "Noise control off"),
                                              (P.AncMode.ANC, "Noise cancelling"),
                                              (P.AncMode.TRANSPARENCY, "Transparency")], dev.set_anc)
            self.m_controls += self.m_mode.values()

            parent, sub = self._submenu(menu, "Noise cancelling scene")
            self.m_scene = self._radios(sub, scene_entries(caps), lambda k: dev.set_scene(*k))
            self.m_controls.append(parent)

            parent, sub = self._submenu(menu, "Transparency level")
            self.m_trans = self._radios(sub, [(n, str(n)) for n in range(1, 8)], dev.set_transparency)
            self.m_controls.append(parent)

            parent, sub = self._submenu(menu, "Equalizer")
            if caps.eq == "n65":
                self.m_eq = self._radios(sub, [(p.id, p.name) for p in P.N65_PRESETS] + [(P.N65_CUSTOM_ID, "Custom")],
                                         dev.set_eq_preset)
            else:
                for name, gains in P.GRAPHIC_PRESETS:
                    self._item(sub, name, lambda g=gains: dev.set_graphic_eq(g))
            self.m_controls.append(parent)

            if caps.n65_extras:
                parent, sub = self._submenu(menu, "Spatial audio")
                self.m_spatial = self._radios(sub, SPATIAL, dev.set_spatial)
                self.m_controls.append(parent)

        menu.append(Gtk.SeparatorMenuItem())
        self.m_settings = self._item(menu, "Settings…", self.open_settings)
        self._item(menu, "Refresh", self.dev.refresh)
        self._item(menu, "Quit", self.quit)
        menu.show_all()
        self.ind.set_menu(menu)
        self.menu = menu
        self.menu_proto = st.proto
        self.updating = False

    def _update_menu(self, st: P.State) -> None:
        self.updating = True
        try:
            self.m_name.set_label(st.name)
            self.m_status.set_label(st.status + (f"  ·  firmware {st.firmware}" if st.connected and st.firmware else ""))
            self.m_battery.set_label(battery_text(st))
            for item in self.m_controls:
                item.set_sensitive(st.connected)
            self.m_settings.set_sensitive(st.proto is not None)
            _activate(self.m_mode, st.anc_mode)
            scene = next((s for s in st.caps.scenes if s.id == st.anc_scene), None)
            _activate(self.m_scene, (st.anc_scene, st.anc_level if scene and scene.levels > 1 else 1))
            _activate(self.m_trans, st.trans_level)
            _activate(self.m_eq, st.eq_index)
            _activate(self.m_spatial, st.spatial)
        finally:
            self.updating = False

    # ---------------------------------------------------------------- actions

    def open_settings(self) -> None:
        if self.window is None:
            self.window = SettingsWindow(self.dev)
        self.window.update(self.state)
        self.window.show_all()
        self.window.present()
        self.dev.refresh()

    def quit(self) -> bool:
        self.dev.stop()
        Gtk.main_quit()
        return False


def _activate(items: dict, key) -> None:
    item = items.get(key)
    if item is not None and not item.get_active():
        item.set_active(True)


class SettingsWindow(Gtk.Window):
    def __init__(self, dev: QCYDevice):
        super().__init__(title="QCY Control")
        self.dev = dev
        self.proto: object = object()
        self.updating = False
        self._timers: dict[str, int] = {}
        self._touched: dict[str, float] = {}
        self.set_icon_name(ICON)
        self.set_border_width(14)
        self.set_default_size(480, -1)
        self.set_resizable(False)
        self.connect("delete-event", lambda *_a: self.hide() or True)

        self.outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(self.outer)
        head = Gtk.Box(spacing=8)
        self.title = Gtk.Label(xalign=0)
        self.title.set_markup("<b>QCY earbuds</b>")
        self.subtitle = Gtk.Label(xalign=0)
        self.subtitle.get_style_context().add_class("dim-label")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.pack_start(self.title, False, False, 0)
        texts.pack_start(self.subtitle, False, False, 0)
        head.pack_start(Gtk.Image.new_from_icon_name(ICON, Gtk.IconSize.DIALOG), False, False, 0)
        head.pack_start(texts, True, True, 0)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Refresh")
        refresh.connect("clicked", lambda _b: dev.refresh())
        head.pack_end(refresh, False, False, 0)
        self.outer.pack_start(head, False, False, 0)
        self.battery = Gtk.Label(xalign=0)
        self.outer.pack_start(self.battery, False, False, 0)
        self.notebook: Gtk.Notebook | None = None

    # ---------------------------------------------------------------- helpers

    def _debounce(self, key: str, ms: int, fn) -> None:
        if key in self._timers:
            GLib.source_remove(self._timers.pop(key))

        def fire() -> bool:
            self._timers.pop(key, None)
            fn()
            return False
        self._timers[key] = GLib.timeout_add(ms, fire)
        self._touched[key] = time.monotonic()

    def _busy(self, key: str) -> bool:
        """Don't overwrite a control the user is moving with an older value from the earbuds."""
        return key in self._timers or time.monotonic() - self._touched.get(key, 0) < 2.5

    @staticmethod
    def _grid() -> Gtk.Grid:
        g = Gtk.Grid(row_spacing=10, column_spacing=12, border_width=12)
        return g

    @staticmethod
    def _label(text: str) -> Gtk.Label:
        return Gtk.Label(label=text, xalign=0)

    def _switch(self, on_change) -> Gtk.Switch:
        sw = Gtk.Switch(halign=Gtk.Align.START)
        sw.connect("notify::active", lambda w, _p: None if self.updating else on_change(w.get_active()))
        return sw

    # ---------------------------------------------------------------- build

    def _rebuild(self, st: P.State) -> None:
        if self.notebook is not None:
            self.outer.remove(self.notebook)
        caps, dev = st.caps, self.dev
        nb = Gtk.Notebook()

        # Sound
        g, r = self._grid(), 0
        self.balance = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.balance.set_hexpand(True)
        self.balance.set_draw_value(False)
        self.balance.add_mark(50, Gtk.PositionType.BOTTOM, None)
        self.balance.connect("value-changed", lambda s: None if self.updating else self._debounce(
            "balance", 300, lambda: dev.set_balance(int(round(s.get_value())))))
        center = Gtk.Button(label="Center")
        center.connect("clicked", lambda _b: self.balance.set_value(50))
        bal = Gtk.Box(spacing=6)
        bal.pack_start(self._label("L"), False, False, 0)
        bal.pack_start(self.balance, True, True, 0)
        bal.pack_start(self._label("R"), False, False, 0)
        bal.pack_start(center, False, False, 0)
        g.attach(self._label("Balance"), 0, r, 1, 1)
        g.attach(bal, 1, r, 1, 1)
        r += 1
        self.game = self.ldac = self.multi = None
        if caps.game_mode:
            self.game = self._switch(dev.set_game)
            g.attach(self._label("Gaming mode (low latency)"), 0, r, 1, 1)
            g.attach(self.game, 1, r, 1, 1)
            r += 1
        if caps.ldac_off is not None:
            self.ldac = self._switch(dev.set_ldac)
            g.attach(self._label("LDAC"), 0, r, 1, 1)
            g.attach(self.ldac, 1, r, 1, 1)
            r += 1
        if caps.n65_extras:
            self.multi = self._switch(dev.set_multi_device)
            g.attach(self._label("Multi-device connection"), 0, r, 1, 1)
            g.attach(self.multi, 1, r, 1, 1)
            r += 1
            note = self._label("Changing LDAC or multi-device restarts the earbuds. LDAC turns off multi-device "
                               "and spatial audio, and Linux audio doesn't use LDAC by default.")
            note.set_line_wrap(True)
            note.set_max_width_chars(52)
            note.get_style_context().add_class("dim-label")
            g.attach(note, 0, r, 2, 1)
        nb.append_page(g, Gtk.Label(label="Sound"))

        # Equalizer
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, border_width=12)
        top = Gtk.Box(spacing=10)
        self.eq_combo = Gtk.ComboBoxText()
        self.dyn = None
        if caps.eq == "n65":
            for p in P.N65_PRESETS:
                self.eq_combo.append(str(p.id), p.name)
            self.eq_combo.append(str(P.N65_CUSTOM_ID), "Custom")
            self.eq_combo.connect("changed", lambda c: None if self.updating or c.get_active_id() is None
                                  else dev.set_eq_preset(int(c.get_active_id())))
            top.pack_start(self._label("Preset"), False, False, 0)
            top.pack_start(self.eq_combo, False, False, 0)
            self.dyn = self._switch(dev.set_dynamic_eq)
            top.pack_end(self.dyn, False, False, 0)
            top.pack_end(self._label("Dynamic EQ"), False, False, 0)
        else:
            for i, (name, _g) in enumerate(P.GRAPHIC_PRESETS):
                self.eq_combo.append(str(i), name)
            self.eq_combo.connect("changed", self._graphic_preset)
            top.pack_start(self._label("Presets"), False, False, 0)
            top.pack_start(self.eq_combo, False, False, 0)
        box.pack_start(top, False, False, 0)
        self.eq_note = self._label("Custom adds these bands on top of the Default tuning." if caps.eq == "n65"
                                   else "±8 dB")
        self.eq_note.get_style_context().add_class("dim-label")
        box.pack_start(self.eq_note, False, False, 0)
        sliders = Gtk.Box(spacing=4, homogeneous=True)
        self.eq_scales: list[Gtk.Scale] = []
        for label in P.EQ_LABELS:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            s = Gtk.Scale.new_with_range(Gtk.Orientation.VERTICAL, -8, 8, 1)
            s.set_inverted(True)
            s.set_digits(0)
            s.set_value_pos(Gtk.PositionType.TOP)
            s.add_mark(0, Gtk.PositionType.LEFT, None)
            s.set_size_request(-1, 150)
            s.connect("value-changed", lambda _s: None if self.updating else self._debounce("eq", 500, self._send_eq))
            col.pack_start(s, True, True, 0)
            col.pack_start(self._label(label), False, False, 0)
            sliders.pack_start(col, True, True, 0)
            self.eq_scales.append(s)
        box.pack_start(sliders, True, True, 0)
        nb.append_page(box, Gtk.Label(label="Equalizer"))

        # Touch
        g = self._grid()
        self.key_combos: dict[int, Gtk.ComboBoxText] = {}
        for r, (slot, name) in enumerate(caps.key_slots):
            c = Gtk.ComboBoxText()
            for fid, fname in caps.key_functions:
                c.append(str(fid), fname)
            c.connect("changed", lambda w, s=slot: None if self.updating or w.get_active_id() is None
                      else dev.set_key(s, int(w.get_active_id())))
            g.attach(self._label(name), 0, r, 1, 1)
            g.attach(c, 1, r, 1, 1)
            self.key_combos[slot] = c
        nb.append_page(g, Gtk.Label(label="Touch"))

        self.outer.pack_start(nb, True, True, 0)
        self.notebook = nb
        self.proto = st.proto
        nb.show_all()

    def _send_eq(self) -> None:
        gains = [int(round(s.get_value())) for s in self.eq_scales]
        if self.proto == P.Proto.JIELI:
            self.dev.set_custom_eq(gains)
        else:
            self.dev.set_graphic_eq(gains)

    def _graphic_preset(self, combo: Gtk.ComboBoxText) -> None:
        if self.updating or combo.get_active_id() is None:
            return
        gains = P.GRAPHIC_PRESETS[int(combo.get_active_id())][1]
        self.updating = True
        for s, v in zip(self.eq_scales, gains):
            s.set_value(v)
        self.updating = False
        self.dev.set_graphic_eq(gains)

    # ---------------------------------------------------------------- update

    def update(self, st: P.State) -> None:
        self.title.set_markup(f"<b>{GLib.markup_escape_text(st.name)}</b>")
        self.subtitle.set_text(st.status + (f"  ·  firmware {st.firmware}" if st.connected and st.firmware else ""))
        self.battery.set_text(battery_text(st))
        if st.proto is None:
            return
        if st.proto != self.proto:
            self._rebuild(st)
        self.updating = True
        try:
            self.notebook.set_sensitive(st.connected)
            if not self._busy("balance"):
                self.balance.set_value(st.balance)
            for sw, val in ((self.game, st.game_mode), (self.ldac, st.ldac), (self.multi, st.multi_device),
                            (self.dyn, st.dynamic_eq)):
                if sw is not None and sw.get_active() != val:
                    sw.set_active(val)
            if st.caps.eq == "n65":
                if not self.eq_combo.set_active_id(str(st.eq_index)):
                    self.eq_combo.set_active(-1)
                custom = st.eq_index == P.N65_CUSTOM_ID
                for s in self.eq_scales:
                    s.set_sensitive(custom)
            if not self._busy("eq"):
                for s, v in zip(self.eq_scales, st.eq):
                    s.set_value(v)
            for slot, combo in self.key_combos.items():
                fid = st.keymap.get(slot, 0)
                if not combo.set_active_id(str(fid)):  # a function this app doesn't know: keep it visible
                    combo.append(str(fid), f"Other (0x{fid:02X})")
                    combo.set_active_id(str(fid))
        finally:
            self.updating = False


def _single_instance():
    path = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "qcy-control.lock")
    f = open(path, "w")  # noqa: SIM115 — held open for the life of the process
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def run() -> int:
    ai = _load_indicator()
    if ai is None:
        print("AppIndicator support is missing. Install it with:\n"
              "  sudo apt install gir1.2-ayatanaappindicator3-0.1\n"
              "and make sure the 'Ubuntu AppIndicators' GNOME extension is enabled.", file=sys.stderr)
        return 1
    lock = _single_instance()
    if lock is None:
        print("QCY Control is already running.", file=sys.stderr)
        return 0
    app = App(ai)
    app.dev.start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, app.quit)
    Gtk.main()
    return 0
