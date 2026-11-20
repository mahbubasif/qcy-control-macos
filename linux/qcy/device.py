"""Bluetooth side: finds the earbuds, keeps the BLE control link up, sends paced commands, keeps a State current.

Runs its own asyncio loop in a background thread. Every public method is thread-safe (the GTK UI calls them);
state changes are reported through ``on_change(snapshot)`` from the Bluetooth thread.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import sys
import threading
import time
from typing import Callable

from bleak import BleakClient, BleakScanner

try:  # bleak >= 3.1: query devices the OS is already connected to, without scanning
    from bleak import BleakAdapter
except ImportError:  # pragma: no cover
    BleakAdapter = None

from . import protocol as P

log = logging.getLogger("qcy")

SEND_GAP = 0.15      # a burst of back-to-back writes gets partly dropped by the earbuds
ANC_GAP = 2.0        # the N65 ignores a noise-mode change that arrives while it is still switching
POLL_EVERY = 15.0    # battery changes are not always pushed
RETRY_EVERY = 3.0
IS_LINUX = sys.platform.startswith("linux")


class QCYDevice:
    def __init__(self, on_change: Callable[[P.State], None], *, check_audio: bool = IS_LINUX):
        self.state = P.State()
        self._on_change = on_change
        self._check_audio = check_audio
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="qcy-ble", daemon=True)
        self._client: BleakClient | None = None
        self._chars: dict[str, object] = {}
        self._outbox: asyncio.Queue | None = None
        self._seq = 0
        self._last_anc = 0.0
        self._pending_anc: asyncio.TimerHandle | None = None
        self._stopping = False
        self._kick: asyncio.Event | None = None
        self._bus = None  # D-Bus system bus (Linux), opened lazily

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if not self._thread.is_alive():
            return
        fut = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            fut.result(timeout)
        except Exception:  # noqa: BLE001 — best effort on quit
            pass

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _shutdown(self) -> None:
        self._stopping = True
        if self._kick:
            self._kick.set()
        client = self._client
        if client is not None and client.is_connected:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ public API (thread-safe)

    def _call(self, fn, *args) -> None:
        self._loop.call_soon_threadsafe(fn, *args)

    def refresh(self) -> None: self._call(self._refresh)
    def set_anc(self, mode: P.AncMode) -> None: self._call(self._set_anc, mode)
    def set_scene(self, scene_id: int, level: int) -> None: self._call(self._set_scene, scene_id, level)
    def set_transparency(self, level: int) -> None: self._call(self._set_transparency, level)
    def set_balance(self, value: int) -> None: self._call(self._set_simple, "balance", 0x16, int(value))
    def set_eq_preset(self, preset_id: int) -> None: self._call(self._set_eq_preset, preset_id)
    def set_custom_eq(self, gains: list[int]) -> None: self._call(self._set_custom_eq, list(gains))
    def set_graphic_eq(self, gains: list[int]) -> None: self._call(self._set_graphic_eq, list(gains))
    def set_dynamic_eq(self, on: bool) -> None: self._call(self._set_flag, "dynamic_eq", 0x27, on, 0)
    def set_spatial(self, value: int) -> None: self._call(self._set_simple, "spatial", 0x5B, int(value))
    def set_multi_device(self, on: bool) -> None: self._call(self._set_flag, "multi_device", 0x24, on, 0)
    def set_game(self, on: bool) -> None: self._call(self._set_flag, "game_mode", 0x09, on, 2)
    def set_ldac(self, on: bool) -> None: self._call(self._set_ldac, on)
    def set_key(self, slot: int, fn: int) -> None: self._call(self._set_key, slot, fn)

    # ------------------------------------------------------------------ main loop

    async def _main(self) -> None:
        self._kick = asyncio.Event()
        while not self._stopping:
            try:
                await self._cycle()
            except Exception as e:  # noqa: BLE001 — keep the loop alive whatever BlueZ throws
                log.warning("connection attempt failed: %r", e)
                self._set_status("Couldn't connect — retrying")
            if self._stopping:
                break
            try:
                await asyncio.wait_for(self._kick.wait(), RETRY_EVERY)
            except asyncio.TimeoutError:
                pass
            self._kick.clear()
        if self._bus is not None:
            self._bus.disconnect()

    async def _cycle(self) -> None:
        audio, name = await self._audio_status()
        changed = audio != self.state.audio_connected or (name and name != self.state.name)
        self.state.audio_connected = audio
        if name:
            self.state.name = name
        if not audio:
            self._set_status("Earbuds not connected", force=changed)
            return
        dev = await self._find()
        if dev is None:
            self._set_status("Searching for earbuds… (open the case)", force=changed)
            return
        await self._session(dev)

    async def _audio_status(self) -> tuple[bool, str | None]:
        """Is a QCY audio device (Classic Bluetooth) connected? The tray icon is only shown while it is.

        Ignores the separate BLE control link some models add ("QCY MeloBuds N65-APP"), which comes and goes on
        its own (e.g. when Spatial Audio changes). Off Linux (testing) we assume yes."""
        if not self._check_audio:
            return True, None
        try:
            objects = await self._bluez_objects()
        except Exception as e:  # noqa: BLE001
            log.debug("BlueZ query failed: %r", e)
            return True, None
        for _path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            name = _v(dev.get("Alias")) or _v(dev.get("Name")) or ""
            if "QCY" in name.upper() and not name.upper().endswith("-APP") and _v(dev.get("Connected")):
                return True, name
        return False, None

    async def _bluez_objects(self) -> dict:
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        if self._bus is None or not self._bus.connected:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        reply = await self._bus.call(Message(destination="org.bluez", path="/",
                                             interface="org.freedesktop.DBus.ObjectManager",
                                             member="GetManagedObjects"))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body}")
        return reply.body[0]

    async def _find(self):
        # 1) the control link may already be up (BlueZ / macOS keep it while any app uses it)
        if BleakAdapter is not None:
            try:
                adapter = await BleakAdapter.get()
                for d in await adapter.get_connected_devices(["1800", "1801", P.SVC_A001, P.SVC_A002]):
                    props = (d.details or {}).get("props", {}) if isinstance(d.details, dict) else {}
                    uuids = {str(u).lower() for u in props.get("UUIDs", [])}
                    if "QCY" in (d.name or "").upper() or uuids & {P.SVC_A001, P.SVC_A002}:
                        log.debug("already connected: %s %s", d.name, d.address)
                        return d
            except Exception as e:  # noqa: BLE001
                log.debug("get_connected_devices failed: %r", e)
        # 2) otherwise it advertises
        self._set_status("Searching for earbuds… (open the case)")
        return await BleakScanner.find_device_by_filter(self._adv_match, timeout=8.0)

    def _adv_match(self, d, adv) -> bool:
        md = adv.manufacturer_data.get(P.QCY_COMPANY_ID)
        name = (adv.local_name or d.name or "").upper()
        uuids = {u.lower() for u in adv.service_uuids}
        hit = md is not None or bool(uuids & {P.SVC_A001, P.SVC_A002}) or ("QCY" in name and name.endswith("-APP"))
        if hit and md is not None and len(md) >= 8:  # A001-style advertisement carries battery levels
            self.state.left, self.state.right, self.state.case = (P.Battery.decode(x) for x in md[5:8])
        return hit

    async def _session(self, dev) -> None:
        disconnected = asyncio.Event()
        client = BleakClient(dev, disconnected_callback=lambda _c: self._loop.call_soon_threadsafe(disconnected.set))
        self._set_status("Connecting…")
        await client.connect(timeout=20)
        tasks: list[asyncio.Task] = []
        try:
            svc_a001 = client.services.get_service(P.SVC_A001)
            svc_a002 = client.services.get_service(P.SVC_A002)
            if svc_a001 is not None:
                proto, svc = P.Proto.A001, svc_a001
                wanted = [P.CHR_CMD, P.CHR_NOTIFY, P.CHR_BATTERY, P.CHR_EQ, P.CHR_KEYS]
            elif svc_a002 is not None:
                proto, svc = P.Proto.JIELI, svc_a002
                wanted = [P.CHR_JL_WRITE, P.CHR_JL_NOTIFY]
            else:
                self._set_status("QCY control service not found")
                await asyncio.sleep(30)  # not ours; don't hammer it
                return
            self._chars = {u: c for u in wanted if (c := svc.get_characteristic(u)) is not None}
            self._client, self._outbox, self.state.proto = client, asyncio.Queue(), proto
            if proto == P.Proto.JIELI:
                self.state.anc_scene = 5
            if self.state.name == P.State.name and dev.name:  # no audio-device name yet (e.g. not on Linux)
                self.state.name = dev.name[:-4] if dev.name.upper().endswith("-APP") else dev.name
            log.debug("connected %s via %s, mtu %s", dev.address, proto.value, _mtu(client))

            if proto == P.Proto.A001:
                await client.start_notify(self._chars[P.CHR_NOTIFY], self._on_notify)
                bat = self._chars.get(P.CHR_BATTERY)
                if bat is not None and "notify" in bat.properties:
                    await client.start_notify(bat, self._on_notify)
                for u in (P.CHR_BATTERY, P.CHR_EQ, P.CHR_KEYS):
                    if u in self._chars:
                        P.apply_direct(self.state, u, bytes(await client.read_gatt_char(self._chars[u])))
            else:
                await client.start_notify(self._chars[P.CHR_JL_NOTIFY], self._on_notify)

            self.state.connected = True
            self._set_status("Connected", force=True)
            tasks.append(asyncio.create_task(self._writer()))
            await asyncio.sleep(0.3)
            if proto == P.Proto.JIELI:
                self._send(0xFE, [0x30])  # the first command after subscribing is sometimes dropped
            self._refresh()
            self._loop.call_later(4, self._refresh)  # once more after the earbuds settle
            tasks.append(asyncio.create_task(self._poll()))

            while not disconnected.is_set() and not self._stopping:
                try:
                    await asyncio.wait_for(disconnected.wait(), RETRY_EVERY)
                except asyncio.TimeoutError:
                    if self._check_audio and not (await self._audio_status())[0]:
                        self.state.audio_connected = False
                        break
        finally:
            for t in tasks:
                t.cancel()
            if self._pending_anc:
                self._pending_anc.cancel()
                self._pending_anc = None
            self._client, self._chars, self._outbox = None, {}, None
            if client.is_connected:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self.state.connected = False
            self._set_status("Earbuds not connected" if not self.state.audio_connected else "Connection lost — reconnecting",
                             force=True)

    # ------------------------------------------------------------------ incoming

    def _on_notify(self, ch, data: bytearray) -> None:
        data, uuid = bytes(data), str(ch.uuid).lower()
        if uuid == P.CHR_JL_NOTIFY and self.state.proto == P.Proto.JIELI:
            tlvs = P.parse_jieli(data)
        elif uuid == P.CHR_NOTIFY:
            tlvs = P.parse_a001(data)
        else:
            P.apply_direct(self.state, uuid, data)
            self._changed()
            return
        for cmd, p in tlvs:
            log.debug("rx %02X: %s", cmd, p.hex(" "))
            P.apply(self.state, cmd, p)
        if tlvs:
            self._changed()

    # ------------------------------------------------------------------ outgoing

    def _send(self, cmd: int, params) -> None:
        if self._outbox is None:
            return
        if self.state.proto == P.Proto.JIELI:
            self._seq = (self._seq + 1) & 0xFF
            self._outbox.put_nowait((P.frame_jieli(self._seq, cmd, params), P.CHR_JL_WRITE))
        else:
            self._outbox.put_nowait((P.frame_a001(cmd, params), P.CHR_CMD))

    def _send_direct(self, char_uuid: str, data: bytes) -> None:
        if self._outbox is not None:
            self._outbox.put_nowait((data, char_uuid))

    async def _writer(self) -> None:
        while True:
            data, uuid = await self._outbox.get()
            ch, client = self._chars.get(uuid), self._client
            if ch is not None and client is not None and client.is_connected:
                try:
                    await client.write_gatt_char(ch, data, response=False)
                    log.debug("tx %s", data.hex(" "))
                except Exception as e:  # noqa: BLE001
                    log.warning("write of %d bytes failed: %r", len(data), e)
                    self._set_status(f"A setting couldn't be sent ({type(e).__name__})", force=True)
            await asyncio.sleep(SEND_GAP)

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(POLL_EVERY)
            if self.state.proto == P.Proto.JIELI:
                self._send(0xFE, [0x2F])
            elif P.CHR_BATTERY in self._chars and self._client is not None:
                try:
                    d = await self._client.read_gatt_char(self._chars[P.CHR_BATTERY])
                    P.apply_direct(self.state, P.CHR_BATTERY, bytes(d))
                    self._changed()
                except Exception as e:  # noqa: BLE001
                    log.debug("battery read failed: %r", e)

    def _refresh(self) -> None:
        if self.state.proto is None or self._client is None:
            return
        for cid in P.REFRESH_IDS[self.state.proto]:
            self._send(0xFE, [cid])

    def _send_anc(self, params: bytes) -> None:
        """At least ANC_GAP after the previous ANC write; only the latest pending change is kept."""
        if self._pending_anc:
            self._pending_anc.cancel()
        delay = max(0.0, ANC_GAP - (time.monotonic() - self._last_anc))
        self._pending_anc = self._loop.call_later(delay, self._fire_anc, params)

    def _fire_anc(self, params: bytes) -> None:
        self._pending_anc = None
        self._last_anc = time.monotonic()
        self._send(0x17, params)
        self._loop.call_later(2.5, self._send, 0xFE, [0x17])  # re-read the real state

    # ------------------------------------------------------------------ setters (Bluetooth thread)

    def _anc_now(self) -> None:
        st = self.state
        self._send_anc(P.anc_params(st.anc_mode, st.caps.scenes, st.anc_scene, st.anc_level, st.trans_level))
        self._changed()

    def _set_anc(self, mode: P.AncMode) -> None:
        self.state.anc_mode = mode
        self._anc_now()

    def _set_scene(self, scene_id: int, level: int) -> None:
        self.state.anc_mode, self.state.anc_scene, self.state.anc_level = P.AncMode.ANC, scene_id, level
        self._anc_now()

    def _set_transparency(self, level: int) -> None:
        self.state.anc_mode, self.state.trans_level = P.AncMode.TRANSPARENCY, level
        self._anc_now()

    def _set_simple(self, attr: str, cmd: int, value: int) -> None:
        setattr(self.state, attr, value)
        self._send(cmd, [value])
        self._changed()

    def _set_flag(self, attr: str, cmd: int, on: bool, off_value: int) -> None:
        setattr(self.state, attr, on)
        self._send(cmd, [1 if on else off_value])
        self._changed()

    def _set_ldac(self, on: bool) -> None:
        off = self.state.caps.ldac_off
        if off is not None:
            self._set_flag("ldac", 0x23, on, off)

    def _set_eq_preset(self, preset_id: int) -> None:
        if preset_id == P.N65_CUSTOM_ID:
            self._set_custom_eq(self.state.eq)
            return
        preset = next((p for p in P.N65_PRESETS if p.id == preset_id), None)
        if preset is None:
            return
        self.state.eq_index, self.state.dynamic_eq = preset_id, False  # writing an EQ turns Dynamic EQ off
        self._send(0x22, P.peq_params(preset_id, preset.bands))
        self._changed()

    def _set_custom_eq(self, gains: list[int]) -> None:
        self.state.eq, self.state.eq_index, self.state.dynamic_eq = list(gains), P.N65_CUSTOM_ID, False
        self._send(0x22, P.peq_params(P.N65_CUSTOM_ID, P.n65_custom_bands(gains)))
        self._changed()

    def _set_graphic_eq(self, gains: list[int]) -> None:
        self.state.eq = list(gains)
        self._send_direct(P.CHR_EQ, P.graphic_eq_bytes(self.state.eq_index, gains))
        self._changed()

    def _set_key(self, slot: int, fn: int) -> None:
        self.state.keymap[slot] = fn
        if self.state.proto == P.Proto.JIELI:
            self._send(0x2B, [slot, fn])
        else:
            self.state.key_raw = P.a001_keymap_with(self.state.key_raw, slot, fn)
            self._send_direct(P.CHR_KEYS, self.state.key_raw)
        self._changed()

    # ------------------------------------------------------------------ reporting

    def _set_status(self, text: str, force: bool = False) -> None:
        if text != self.state.status or force:
            self.state.status = text
            self._changed()

    def _changed(self) -> None:
        try:
            self._on_change(copy.deepcopy(self.state))
        except Exception:  # noqa: BLE001
            log.exception("on_change handler failed")


def _v(variant):
    """dbus-fast returns Variants for a{sv} values."""
    return getattr(variant, "value", variant)


def _mtu(client: BleakClient) -> str:
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return str(client.mtu_size)
    except Exception:  # noqa: BLE001
        return "?"
