"""Protocol tests built from real frames captured from a QCY MeloBuds N65 (fw 2.0.11).

Run from linux/:  python3 -m unittest discover -s tests
"""
import unittest

from qcy import protocol as P


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


class Framing(unittest.TestCase):
    def test_jieli_query_frame_matches_capture(self):
        # what we sent to read the ANC state; the N65 answered it
        self.assertEqual(P.frame_jieli(1, 0xFE, [0x17]), h("fe dc ba c0 ff 00 04 01 fe 01 17 ef"))

    def test_jieli_anc_off_frame(self):
        self.assertEqual(P.frame_jieli(1, 0x17, [2, 0, 0]), h("fe dc ba c0 ff 00 06 01 17 03 02 00 00 ef"))

    def test_seq_wraps(self):
        self.assertEqual(P.frame_jieli(256 + 5, 0xFE, [0x30])[7], 5)

    def test_a001_frame(self):
        self.assertEqual(P.frame_a001(0x17, [1, 1, 2]), h("ff 05 17 03 01 01 02"))


class ParseJieLi(unittest.TestCase):
    def test_reply(self):
        self.assertEqual(P.parse_jieli(h("fe dc ba 00 ff 00 07 00 01 17 03 01 05 00 ef")), [(0x17, h("01 05 00"))])

    def test_push(self):
        self.assertEqual(P.parse_jieli(h("fe dc ba 80 ff 00 06 39 17 03 02 00 00 ef")), [(0x17, h("02 00 00"))])

    def test_push_unknown_cmd_kept_as_tlv(self):
        self.assertEqual(P.parse_jieli(h("fe dc ba 80 ff 00 0a 3e 4f 07 c6 3b ea 76 8a 00 03 ef")),
                         [(0x4F, h("c6 3b ea 76 8a 00 03"))])

    def test_empty_ack(self):
        self.assertEqual(P.parse_jieli(h("fe dc ba 00 ff 00 02 00 62 ef")), [])

    def test_error_status_ignored(self):
        self.assertEqual(P.parse_jieli(h("fe dc ba 00 ff 00 07 01 01 17 03 01 05 00 ef")), [])

    def test_other_opcode_ignored(self):
        self.assertEqual(P.parse_jieli(h("fe dc ba 80 0f 00 02 00 00 ef")), [])

    def test_garbage(self):
        for d in (b"", h("ff 02 0f 00"), h("04"), h("fe dc ba 00 ff 00 07 00 01 17 03 01 05 00")):
            self.assertEqual(P.parse_jieli(d), [])

    def test_a001_parse(self):
        self.assertEqual(P.parse_a001(h("ff 05 17 03 03 01 04")), [(0x17, h("03 01 04"))])


class ApplyState(unittest.TestCase):
    def setUp(self):
        self.st = P.State(proto=P.Proto.JIELI)

    def feed(self, frame: str):
        for cmd, p in P.parse_jieli(h(frame)):
            P.apply(self.st, cmd, p)

    def test_anc_adaptive(self):
        self.feed("fe dc ba 00 ff 00 07 00 01 17 03 01 05 00 ef")
        self.assertEqual((self.st.anc_mode, self.st.anc_scene, self.st.anc_level), (P.AncMode.ANC, 5, 1))

    def test_transparency_level_7(self):
        self.feed("fe dc ba 80 ff 00 06 3b 17 03 03 01 06 ef")
        self.assertEqual((self.st.anc_mode, self.st.trans_level), (P.AncMode.TRANSPARENCY, 7))

    def test_transparency_level_1_quirk(self):
        self.st.trans_level = 5
        P.apply(self.st, 0x17, bytes([3, 2, 0]))
        self.assertEqual(self.st.trans_level, 1)

    def test_battery(self):
        self.feed("fe dc ba 00 ff 00 07 00 25 2f 03 46 46 4b ef")
        self.assertEqual((self.st.left, self.st.right, self.st.case),
                         (P.Battery(70, False), P.Battery(70, False), P.Battery(75, False)))
        self.assertFalse(self.st.case_is_live)

    def test_battery_bud_in_case(self):
        P.apply(self.st, 0x2F, h("bc 46 64"))  # captured when the left bud went into the case
        self.assertEqual(self.st.left, P.Battery(60, True))
        self.assertTrue(self.st.case_is_live)

    def test_battery_zero_is_unknown(self):
        self.assertFalse(P.Battery.decode(0).known)
        self.assertTrue(P.Battery.decode(0x80).known)

    def test_keymap(self):
        P.apply(self.st, 0x2B, h("01 06 02 05 03 01 04 01 05 02 06 03 07 00 08 00 09 04 0a 0b"
                                  " 41 80 42 80 43 80 44 80 45 80 46 80 47 80 48 80 49 80 4a 80"))
        self.assertEqual(self.st.keymap, {1: 6, 2: 5, 3: 1, 4: 1, 5: 2, 6: 3, 7: 0, 8: 0, 9: 4, 10: 0x0B})

    def test_version(self):
        P.apply(self.st, 0x30, h("02 00 0b"))
        self.assertEqual(self.st.firmware, "2.0.11")

    def test_misc(self):
        for cmd, p in [(0x16, "32"), (0x27, "01"), (0x5B, "02"), (0x23, "00"), (0x24, "01")]:
            P.apply(self.st, cmd, h(p))
        self.assertEqual((self.st.balance, self.st.dynamic_eq, self.st.spatial, self.st.ldac, self.st.multi_device),
                         (50, True, 2, False, True))

    def test_unknown(self):
        self.assertFalse(P.apply(self.st, 0x54, h("04 00 00 00 00 00")))


class Anc(unittest.TestCase):
    def test_params(self):
        sc = P.N65_SCENES
        self.assertEqual(P.anc_params(P.AncMode.OFF, sc, 5, 1, 4), h("02 00 00"))
        self.assertEqual(P.anc_params(P.AncMode.ANC, sc, 5, 1, 4), h("01 05 00"))
        self.assertEqual(P.anc_params(P.AncMode.ANC, sc, 1, 2, 4), h("01 01 01"))   # Noisy level 2
        self.assertEqual(P.anc_params(P.AncMode.ANC, sc, 4, 3, 4), h("01 04 00"))   # Anti-wind has 1 level
        self.assertEqual(P.anc_params(P.AncMode.TRANSPARENCY, sc, 5, 1, 1), h("03 02 00"))
        self.assertEqual(P.anc_params(P.AncMode.TRANSPARENCY, sc, 5, 1, 7), h("03 01 06"))


class Eq(unittest.TestCase):
    # The Pop preset exactly as written to the N65 and echoed back by it.
    POP = ("02 50 fb 14 00 88 fa 50 00 02 96 00 24 fa 28 00 02 ee 02 9c ff 96 00 02 40 01 38 ff 64 00 02 54 0b b6 03"
           " 50 00 02 d0 07 64 00 c8 00 02 10 27 58 02 96 00 05 70 17 d4 fe 2c 01 02 32 00 06 ff 46 00 02")

    def test_pop_preset_matches_capture(self):
        pop = next(p for p in P.N65_PRESETS if p.name == "Pop")
        self.assertEqual(P.peq_params(pop.id, pop.bands), h(self.POP))

    def test_custom_roundtrip(self):
        gains = [3, -2, 0, 8, -8, 1, 0, 0, 5, -1]
        p = P.peq_params(P.N65_CUSTOM_ID, P.n65_custom_bands(gains))
        self.assertEqual(len(p), 3 + 18 * 7)
        st = P.State(proto=P.Proto.JIELI)
        P.apply(st, 0x22, p)
        self.assertEqual((st.eq_index, st.eq), (0, gains))

    def test_custom_clamped(self):
        bands = P.n65_custom_bands([20, -20] + [0] * 8)
        self.assertEqual((bands[8].gain, bands[9].gain), (8.0, -8.0))

    def test_factory_eq_is_not_custom(self):
        factory = h("01 88 fa 14 00 b4 fb 50 00 02 a0 00 24 fa 3c 00 02 ee 02 d4 fe 64 00 02 5e 01 38 ff 64 00 02"
                    " 54 0b 14 05 50 00 02 40 1f 2c 01 2c 01 02 10 27 58 02 96 00 05 b0 36 00 00 46 00 02 ce 27 00"
                    " 00 46 00 02 5f 54 00 00 46 00 02 28 00 00 00 46 00 07 50 46 00 00 46 00 08")
        st = P.State(proto=P.Proto.JIELI)
        P.apply(st, 0x22, factory)
        self.assertEqual((st.eq_index, st.eq), (1, [0] * 10))

    def test_graphic_eq(self):
        self.assertEqual(P.graphic_eq_bytes(1, [-8, 8, 0, 0, 0, 0, 0, 0, 0, 9]), h("01 f8 08 00 00 00 00 00 00 00 08"))
        st = P.State(proto=P.Proto.A001)
        P.apply_direct(st, P.CHR_EQ, h("01 f8 08 00 00 00 00 00 00 00 08"))
        self.assertEqual(st.eq, [-8, 8, 0, 0, 0, 0, 0, 0, 0, 8])


class Keys(unittest.TestCase):
    def test_a001_keymap_replace_and_append(self):
        raw = h("01 01 02 01")
        self.assertEqual(P.a001_keymap_with(raw, 2, 5), h("01 01 02 05"))
        self.assertEqual(P.a001_keymap_with(raw, 3, 4), h("01 01 02 01 03 04"))
        self.assertEqual(len(P.a001_keymap_with(b"", 1, 1)), 12)


if __name__ == "__main__":
    unittest.main()
