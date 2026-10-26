"""
Unit tests for DriftScript's Time Synchronization Engine and Acoustic Boundary Tightener.
"""

import unittest
import numpy as np

from driftscript.sync_engine import (
    compute_keep_and_deleted_ranges,
    detect_cut_words_from_timeline_clips,
    source_to_timeline,
    timeline_to_source,
)
from driftscript.transcriber import (
    _build_silence_group,
    detect_and_split_stretched_words,
    tighten_word_bounds,
)


class TestSyncEngine(unittest.TestCase):
    def test_source_and_timeline_bidirectional_mapping(self):
        # Simulate Drift Track 1 with a cut between 5.689s and 6.536s (0.847s removed)
        clips = [
            {"id": "c1", "start": 0.0, "duration": 5.689, "inPoint": 0.0, "outPoint": 5.689, "speed": 1.0},
            {"id": "c2", "start": 5.689, "duration": 1104.959, "inPoint": 6.536, "outPoint": 1111.495, "speed": 1.0},
        ]

        # Word at source 2.0s -> timeline 2.0s
        tl, inside, cid = source_to_timeline(2.0, clips)
        self.assertAlmostEqual(tl, 2.0, places=3)
        self.assertTrue(inside)
        self.assertEqual(cid, "c1")

        # Word at source 10.000s -> timeline 10.000 - 0.847 = 9.153s
        tl2, inside2, cid2 = source_to_timeline(10.0, clips)
        self.assertAlmostEqual(tl2, 9.153, places=3)
        self.assertTrue(inside2)
        self.assertEqual(cid2, "c2")

        # Reverse mapping: timeline 9.153s -> source 10.000s
        src2, rcid2 = timeline_to_source(9.153, clips)
        self.assertAlmostEqual(src2, 10.0, places=3)
        self.assertEqual(rcid2, "c2")

        # Word inside the cut gap (e.g. "Davi" at 6.121s) -> snaps to cut junction at 5.689s
        tl_cut, inside_cut, _ = source_to_timeline(6.121, clips)
        self.assertAlmostEqual(tl_cut, 5.689, places=3)
        self.assertFalse(inside_cut)

    def test_detect_cut_words_from_timeline_clips(self):
        clips = [
            {"id": "c1", "start": 0.0, "duration": 5.689, "inPoint": 0.0, "outPoint": 5.689, "speed": 1.0},
            {"id": "c2", "start": 5.689, "duration": 100.0, "inPoint": 6.536, "outPoint": 106.536, "speed": 1.0},
        ]
        words = [
            {"index": 0, "word": "Eu", "start": 5.73, "end": 5.81},
            {"index": 1, "word": "me", "start": 5.82, "end": 5.88},
            {"index": 2, "word": "chamo", "start": 5.92, "end": 6.08},
            {"index": 3, "word": "Davi", "start": 6.12, "end": 6.52},
            {"index": 4, "word": "e", "start": 6.56, "end": 6.60},
            {"index": 5, "word": "vou", "start": 6.70, "end": 6.78},
        ]
        cut = detect_cut_words_from_timeline_clips(words, clips)
        self.assertEqual(cut, [0, 1, 2, 3])

    def test_compute_keep_and_deleted_ranges_merges_consecutive_and_protects_neighbors(self):
        words = [
            {"index": 0, "word": "Olá", "start": 1.00, "end": 1.40, "deleted": False},
            {"index": 1, "word": "hum", "start": 1.50, "end": 1.80, "deleted": True},
            {"index": 2, "word": "tipo", "start": 1.90, "end": 2.20, "deleted": True},
            {"index": 3, "word": "pessoal", "start": 2.30, "end": 2.80, "deleted": False},
        ]
        res = compute_keep_and_deleted_ranges(words, source_duration=5.0, padding_sec=0.04)
        self.assertEqual(len(res["deleted_ranges"]), 1)
        cut = res["deleted_ranges"][0]
        # Must protect word 0 end (1.40 + 0.04 = 1.44) and word 3 start (2.30 - 0.04 = 2.26)
        self.assertGreaterEqual(cut["start"], 1.44)
        self.assertLessEqual(cut["end"], 2.26)
        self.assertEqual(len(res["keep_ranges"]), 2)
        self.assertAlmostEqual(res["keep_ranges"][0]["start"], 0.0)
        self.assertAlmostEqual(res["keep_ranges"][0]["end"], cut["start"])
        self.assertAlmostEqual(res["keep_ranges"][1]["start"], cut["end"])
        self.assertAlmostEqual(res["keep_ranges"][1]["end"], 5.0)

    def test_tighten_word_bounds_removes_trailing_silence(self):
        # Create 3 seconds of 10ms RMS frames (300 frames): speech only from 0.50s to 0.90s
        rms = np.full(300, 0.0002, dtype=np.float32)
        rms[50:90] = 0.25
        words = [{"index": 0, "word": "câmera.", "start": 0.48, "end": 2.80}]
        tight, count = tighten_word_bounds(words, rms)
        self.assertEqual(count, 1)
        self.assertAlmostEqual(tight[0]["start"], 0.48, places=2)
        self.assertLessEqual(tight[0]["end"], 0.98)

    def test_tighten_word_bounds_preserves_multisyllable_words(self):
        # Simulate "ca-pa-ci-tor" from 1.00s to 1.62s with quiet plosive dips in between syllables
        rms = np.full(300, 0.0002, dtype=np.float32)
        rms[102:112] = 0.025  # "ca"
        rms[116:128] = 0.030  # "pa"
        rms[132:142] = 0.020  # "ci"
        rms[146:160] = 0.180  # "tor" (stressed)
        words = [{"index": 0, "word": "capacitor", "start": 1.00, "end": 1.62}]
        tight, count = tighten_word_bounds(words, rms)
        # Must NOT clip the first syllables "ca-pa-ci-"
        self.assertEqual(count, 0)
        self.assertAlmostEqual(tight[0]["start"], 1.00, places=2)
        self.assertAlmostEqual(tight[0]["end"], 1.62, places=2)

    def test_pause_cut_preserves_natural_cadence_cushion(self):
        words = [
            {"index": 0, "word": "conexão", "kind": "word", "start": 1.00, "end": 1.50, "deleted": False},
            {"index": 1, "word": "·· 0.6s", "kind": "pause", "start": 1.50, "end": 2.10, "deleted": True},
            {"index": 2, "word": "capacitor", "kind": "word", "start": 2.10, "end": 2.70, "deleted": False},
        ]
        res = compute_keep_and_deleted_ranges(words, source_duration=4.0)
        self.assertEqual(len(res["deleted_ranges"]), 1)
        cut = res["deleted_ranges"][0]
        # Must leave >= 90ms after word 0 (1.59s) and >= 90ms before word 2 (2.01s)
        self.assertGreaterEqual(cut["start"], 1.59)
        self.assertLessEqual(cut["end"], 2.01)

    def test_granular_silence_dots_dashes_and_time_rules(self):
        # 11 dots (0.55s) -> 11 "•" tokens (no dash yet)
        g11 = _build_silence_group(1.0, 1.55, group_id=1, dot_unit_sec=0.05)
        self.assertEqual([t["word"] for t in g11], ["•"] * 11)

        # 12 dots (0.60s) -> "•", "—", "•" (first and last are "•", middle is 1 dash = 10 dots)
        g12 = _build_silence_group(1.0, 1.60, group_id=2, dot_unit_sec=0.05)
        self.assertEqual([t["word"] for t in g12], ["•", "—", "•"])

        # 42 dots (2.10s) -> "•", "—", "—", "—", "—", "•" (4 dashes)
        g42 = _build_silence_group(1.0, 3.10, group_id=3, dot_unit_sec=0.05)
        self.assertEqual([t["word"] for t in g42], ["•", "—", "—", "—", "—", "•"])

        # > 4 dashes (e.g. 3.0s = 60 dots) -> "•", "(3.0s)", "•"
        g60 = _build_silence_group(1.0, 4.0, group_id=4, dot_unit_sec=0.05)
        self.assertEqual([t["word"] for t in g60], ["•", "(3.0s)", "•"])

    def test_detect_and_split_stretched_words(self):
        rms = np.full(300, 0.15, dtype=np.float32)
        words = [{"index": 0, "word": "é...", "start": 1.00, "end": 1.70}]
        split = detect_and_split_stretched_words(words, rms)
        self.assertEqual(len(split), 2)
        self.assertEqual(split[0]["kind"], "word")
        self.assertEqual(split[1]["kind"], "stretch")
        self.assertTrue(split[1]["word"].startswith("〰"))


if __name__ == "__main__":
    unittest.main()
