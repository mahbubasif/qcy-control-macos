"""QCY earbuds BLE protocol: framing, parsing and per-model data.

Pure Python, no I/O — shared by the Linux app and its tests. Mirrors app/QCYControl.swift.

Two transports carry the same QCY commands (``<cmd> <paramLen> <params…>``):

* **A001** (MeloBuds Pro, …): framed ``FF <len> <cmd><plen><params>…`` on 1001, notifications on 1002,
  plus direct characteristics for battery (0008), EQ (000B) and key map (000D).
* **A002 / JieLi RCSP** (MeloBuds N65): written to 0001, notified on 0002, wrapped as
  ``FE DC BA <flag> FF <lenH><lenL> [status] <seq> <cmd><plen><params>… EF``.
"""
from __future__ import annotations

import enum
import struct
from dataclasses import dataclass, field


def uuid16(short: str) -> str:
    return f"0000{short.lower()}-0000-1000-8000-00805f9b34fb"


SVC_A001 = uuid16("a001")
CHR_CMD = uuid16("1001")
CHR_NOTIFY = uuid16("1002")
CHR_BATTERY = uuid16("0008")
CHR_EQ = uuid16("000b")
CHR_KEYS = uuid16("000d")

SVC_A002 = uuid16("a002")
CHR_JL_WRITE = uuid16("0001")
CHR_JL_NOTIFY = uuid16("0002")

QCY_COMPANY_ID = 0x521C


class Proto(enum.Enum):
    A001 = "a001"
    JIELI = "jieli"


# --------------------------------------------------------------------------- framing

def frame_a001(cmd: int, params: bytes | list[int]) -> bytes:
    body = bytes([cmd, len(params)]) + bytes(params)
    return bytes([0xFF, len(body)]) + body


def frame_jieli(seq: int, cmd: int, params: bytes | list[int]) -> bytes:
    """Flag C0 = command that wants a reply; opcode FF = vendor (QCY) data."""
    payload = bytes([seq & 0xFF, cmd, len(params)]) + bytes(params)
    return b"\xFE\xDC\xBA\xC0\xFF" + struct.pack(">H", len(payload)) + payload + b"\xEF"


def parse_tlvs(data: bytes) -> list[tuple[int, bytes]]:
    out: list[tuple[int, bytes]] = []
    i = 0
    while i + 1 < len(data):
        cmd, n = data[i], data[i + 1]
        if i + 2 + n > len(data):
            break
        out.append((cmd, bytes(data[i + 2:i + 2 + n])))
        i += 2 + n
    return out


def parse_a001(data: bytes) -> list[tuple[int, bytes]]:
    if len(data) < 2 or data[0] != 0xFF:
        return []
    return parse_tlvs(data[2:])


def parse_jieli(data: bytes) -> list[tuple[int, bytes]]:
    """Flag bit 7 set = pushed by the earbuds (no status byte); clear = reply to us (status 0 = OK).
    Frames with another opcode (JieLi system messages) are ignored."""
    if (len(data) < 9 or data[:3] != b"\xFE\xDC\xBA" or data[4] != 0xFF or data[-1] != 0xEF):
        return []
    push = data[3] & 0x80 != 0
    if not push and data[7] != 0:
        return []
    start = 8 if push else 9
    return parse_tlvs(data[start:-1]) if start < len(data) - 1 else []


# --------------------------------------------------------------------------- model data

@dataclass(frozen=True)
class Battery:
    level: int = -1          # -1 = unknown
    charging: bool = False

    @staticmethod
    def decode(b: int) -> "Battery":
        return Battery(b & 0x7F, b & 0x80 != 0)

    @property
    def known(self) -> bool:
        """0 % without charging means "no report" (bud off / out of range), not an empty battery."""
        return self.level > 0 or self.charging


class AncMode(enum.IntEnum):
    ANC = 1
    OFF = 2
    TRANSPARENCY = 3


@dataclass(frozen=True)
class Scene:
    id: int
    name: str
    levels: int


A001_SCENES = [Scene(5, "Adaptive", 1), Scene(1, "Indoor", 3), Scene(2, "Commuting", 3),
               Scene(3, "Noisy", 3), Scene(4, "Anti-wind", 1)]
# From QCY's official control panel for the N65 (modelId 32891).
N65_SCENES = [Scene(5, "Adaptive", 1), Scene(1, "Noisy", 3), Scene(2, "Commuting", 3),
              Scene(3, "Indoor", 3), Scene(4, "Anti-wind", 1)]


def anc_params(mode: AncMode, scenes: list[Scene], scene_id: int, level: int, trans_level: int) -> bytes:
    if mode == AncMode.OFF:
        return bytes([2, 0, 0])
    if mode == AncMode.TRANSPARENCY:
        v = max(0, min(6, trans_level - 1))
        # Firmware quirk (MeloBuds Pro and N65): level 1 is (3,2,0), not (3,1,0).
        return bytes([3, 2, 0]) if v == 0 else bytes([3, 1, v])
    sc = next((s for s in scenes if s.id == scene_id), scenes[0])
    return bytes([1, sc.id, max(0, min(sc.levels - 1, level - 1))])


# Parametric EQ (N65, cmd 0x22): [presetId, masterGain×100 i16, n × (freq u16, gain×100 i16, Q×100 u16, type u8)], LE.
@dataclass(frozen=True)
class PEQBand:
    freq: int
    gain: float
    q: float
    type: int


@dataclass(frozen=True)
class PEQPreset:
    id: int
    name: str
    bands: tuple[PEQBand, ...]


def _peq(freq, gain, q, typ) -> tuple[PEQBand, ...]:
    return tuple(PEQBand(*b) for b in zip(freq, gain, q, typ))


_F8 = [20, 150, 750, 320, 2900, 2000, 10000, 6000]
_F9H = [20, 150, 750, 320, 2900, 8000, 10000, 6000, 50]
_T9 = [2, 2, 2, 2, 2, 2, 5, 2, 2]
_Q9 = [0.8, 0.4, 1.5, 1, 0.8, 2, 1.5, 3, 0.7]

N65_MASTER_GAIN = -12.0
# Official N65 presets (QCY server "sys_eq"). Each includes the earbuds' base tuning.
N65_PRESETS = [
    PEQPreset(1, "Default", _peq(_F8, [-11, -12.5, -1, -2, 9.5, 1, 6, -3], _Q9[:8], _T9[:8])),
    PEQPreset(2, "Pop", _peq(_F8 + [50], [-14, -15, -1, -2, 9.5, 1, 6, -3, -2.5], _Q9, _T9)),
    PEQPreset(3, "Heavy Bass", _peq(_F9H, [-8, -11, -1, -2, 7, 4, 6, -3, 3], _Q9, _T9)),
    PEQPreset(4, "Rock", _peq(_F9H, [-8, -15, -2, -3, 9.5, 4, 6, -3, 3], [0.8, 0.6, 1.5, 1, 0.8, 2, 1.5, 3, 0.7], _T9)),
    PEQPreset(5, "Soft", _peq(_F8 + [50], [-13, -15, 0, -2, 6.5, 2, 6, -3, -1.5], _Q9, _T9)),
    PEQPreset(6, "Classic", _peq(_F8 + [50], [-8, -11, -1, -2, 9.5, 1, 6, -3, 3], _Q9, _T9)),
    PEQPreset(13, "Game", _peq(_F8 + [50], [-11, -14, -1, -2, 11, 2, 6, -1, -1.5], [0.8, 0.4, 1.5, 1, 0.8, 1, 1.5, 3, 1], _T9)),
]
N65_CUSTOM_ID = 0
EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
EQ_LABELS = ["31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]


def peq_params(index: int, bands, master: float = N65_MASTER_GAIN) -> bytes:
    def i16(v: float) -> int:
        return max(-32768, min(32767, round(v * 100)))
    out = bytes([index]) + struct.pack("<h", i16(master))
    for b in bands:
        out += struct.pack("<HhHB", b.freq, i16(b.gain), round(b.q * 100), b.type)
    return out


def n65_custom_bands(gains: list[int]) -> tuple[PEQBand, ...]:
    """Custom EQ = Default base tuning (8 bands) + the user's 10 graphic bands (Q 0.7, peaking) = 18 bands."""
    user = tuple(PEQBand(f, float(max(-8, min(8, g))), 0.7, 2) for f, g in zip(EQ_FREQS, gains))
    return N65_PRESETS[0].bands + user


def decode_custom_gains(p: bytes) -> list[int] | None:
    """User gains back out of a 0x22 reply, if it is our 18-band custom EQ."""
    n = (len(p) - 3) // 7
    if len(p) < 3 or p[0] != N65_CUSTOM_ID or n != 18:
        return None
    return [round(struct.unpack_from("<h", p, 3 + i * 7 + 2)[0] / 100) for i in range(8, 18)]


# Graphic EQ (A001, direct write to 000B): [presetIndex, 10 × int8 gain]
GRAPHIC_PRESETS = [
    ("Flat", [0] * 10), ("Bass boost", [6, 5, 4, 2, 0, 0, 0, 0, 0, 0]), ("Pop", [-1, 1, 3, 4, 3, 0, -1, -1, 1, 2]),
    ("Rock", [4, 3, 1, -1, -2, -1, 1, 3, 4, 4]), ("Soft", [1, 1, 0, 0, 0, 0, -1, -2, -3, -3]),
    ("Classical", [3, 2, 1, 0, 0, 0, 0, 1, 2, 3]), ("Vocal", [-3, -2, 0, 2, 3, 3, 2, 1, 0, -1]),
]


def graphic_eq_bytes(index: int, gains: list[int]) -> bytes:
    return bytes([index]) + bytes((max(-8, min(8, g)) & 0xFF) for g in gains)


A001_KEY_SLOTS = [(1, "Left single"), (2, "Right single"), (3, "Left double"), (4, "Right double"),
                  (5, "Left triple"), (6, "Right triple")]
A001_KEY_FUNCTIONS = [(0, "None"), (1, "Play/Pause"), (2, "Previous"), (3, "Next"), (4, "Voice assistant"),
                      (5, "Volume +"), (6, "Volume −"), (7, "Gaming mode"), (8, "ANC mode")]
N65_KEY_SLOTS = A001_KEY_SLOTS + [(9, "Left hold"), (10, "Right hold")]
N65_KEY_FUNCTIONS = [(0, "None"), (1, "Play/Pause"), (2, "Previous"), (3, "Next"), (4, "Voice assistant"),
                     (5, "Volume +"), (6, "Volume −"), (0x0B, "Noise control")]


def a001_keymap_with(raw: bytes, slot: int, fn: int) -> bytes:
    """A001 key map (000D) is the whole [keyId, funcId] list; replace or append one pair."""
    d = bytearray(raw) if raw else bytearray(b for s, _ in A001_KEY_SLOTS for b in (s, 0))
    for i in range(0, len(d) - 1, 2):
        if d[i] == slot:
            d[i + 1] = fn
            return bytes(d)
    return bytes(d + bytes([slot, fn]))


@dataclass(frozen=True)
class Caps:
    scenes: list[Scene]
    eq: str                  # "graphic10" | "n65"
    game_mode: bool
    ldac_off: int | None     # parameter that turns LDAC off; None hides the switch
    n65_extras: bool         # Dynamic EQ (0x27), Spatial Audio (0x5B), multi-device (0x24)
    key_slots: list[tuple[int, str]]
    key_functions: list[tuple[int, str]]


CAPS = {
    Proto.A001: Caps(A001_SCENES, "graphic10", True, 2, False, A001_KEY_SLOTS, A001_KEY_FUNCTIONS),
    # N65: game mode (0x4A) left out — QCY's app warns it reboots the earbuds.
    Proto.JIELI: Caps(N65_SCENES, "n65", False, 0, True, N65_KEY_SLOTS, N65_KEY_FUNCTIONS),
}

# Queried on refresh, per transport.
REFRESH_IDS = {
    Proto.A001: [0x17, 0x09, 0x16, 0x23, 0x2F],
    Proto.JIELI: [0x17, 0x16, 0x2F, 0x2B, 0x22, 0x27, 0x5B, 0x23, 0x24],
}


# --------------------------------------------------------------------------- state

@dataclass
class State:
    status: str = "Starting…"
    audio_connected: bool = False
    connected: bool = False
    name: str = "QCY earbuds"
    proto: Proto | None = None
    firmware: str = ""
    left: Battery = field(default_factory=Battery)
    right: Battery = field(default_factory=Battery)
    case: Battery = field(default_factory=Battery)
    anc_mode: AncMode = AncMode.OFF
    anc_scene: int = 5
    anc_level: int = 1
    trans_level: int = 4
    game_mode: bool = False
    ldac: bool = False
    balance: int = 50
    eq_index: int = 1
    eq: list[int] = field(default_factory=lambda: [0] * 10)
    dynamic_eq: bool = False
    spatial: int = 1         # 1 off, 2 fixed, 3 head tracking
    multi_device: bool = False
    keymap: dict[int, int] = field(default_factory=dict)
    key_raw: bytes = b""

    @property
    def caps(self) -> Caps:
        return CAPS[self.proto or Proto.A001]

    @property
    def case_is_live(self) -> bool:
        """The case reports only through a bud sitting in it (that bud shows as charging)."""
        return self.left.charging or self.right.charging or self.case.charging


def apply(st: State, cmd: int, p: bytes) -> bool:
    """Update state from one decoded command. Returns True if it was understood."""
    if cmd == 0x17 and len(p) >= 3:
        if p[0] == 1:
            st.anc_mode, st.anc_scene, st.anc_level = AncMode.ANC, p[1], p[2] + 1
        elif p[0] == 3:
            st.anc_mode, st.trans_level = AncMode.TRANSPARENCY, p[2] + 1
        else:
            st.anc_mode = AncMode.OFF
    elif cmd == 0x0C and len(p) >= 1:
        if p[0] == 0:
            st.anc_mode = AncMode.OFF
    elif cmd == 0x09 and p:
        st.game_mode = p[0] == 1
    elif cmd == 0x23 and p:
        st.ldac = p[0] == 1
    elif cmd == 0x16 and p:
        st.balance = p[0]
    elif cmd == 0x2F and len(p) >= 3:
        st.left, st.right, st.case = (Battery.decode(x) for x in p[:3])
    elif cmd == 0x30 and len(p) >= 3:
        st.firmware = ".".join(str(x) for x in p)
    elif cmd == 0x22 and len(p) >= 3 and st.proto == Proto.JIELI:
        st.eq_index = p[0]
        gains = decode_custom_gains(p)
        if gains is not None:
            st.eq = gains
    elif cmd == 0x27 and p:
        st.dynamic_eq = p[0] == 1
    elif cmd == 0x5B and p:
        st.spatial = p[0]
    elif cmd == 0x24 and p:
        st.multi_device = p[0] == 1
    elif cmd == 0x2B and len(p) >= 2:
        # [keyId, funcId] pairs; ids >= 0x41 are not touch gestures
        st.keymap = {p[i]: p[i + 1] for i in range(0, len(p) - 1, 2) if 0 < p[i] < 0x41}
    else:
        return False
    return True


def apply_direct(st: State, char_uuid: str, d: bytes) -> None:
    """A001 direct characteristics (battery / EQ / key map)."""
    if char_uuid == CHR_BATTERY and len(d) >= 3:
        st.left, st.right, st.case = (Battery.decode(x) for x in d[:3])
    elif char_uuid == CHR_EQ and len(d) >= 11:
        st.eq_index = d[0]
        st.eq = [struct.unpack("b", bytes([x]))[0] for x in d[1:11]]
    elif char_uuid == CHR_KEYS:
        st.key_raw = bytes(d)
        st.keymap = {d[i]: d[i + 1] for i in range(0, len(d) - 1, 2) if d[i] != 0}
