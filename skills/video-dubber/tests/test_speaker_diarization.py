from types import SimpleNamespace
import unittest

from core.speaker_diarization import assign_speakers


def _sub(start, end):
    return SimpleNamespace(start=start, end=end)


class SpeakerAssignmentTests(unittest.TestCase):
    def test_uses_dominant_overlap_and_first_seen_names(self):
        turns = [
            {"start": 0.0, "end": 4.0, "label": 8},
            {"start": 4.0, "end": 5.0, "label": 3},
            {"start": 5.0, "end": 10.0, "label": 3},
        ]
        assignments, purity = assign_speakers(
            [_sub(0, 4000), _sub(3500, 6000), _sub(6000, 9000)],
            turns,
        )
        self.assertEqual(assignments, {
            "0": "speaker_00",
            "1": "speaker_01",
            "2": "speaker_01",
        })
        self.assertEqual(purity["0"], 1.0)
        self.assertEqual(purity["1"], 0.8)
        self.assertEqual(purity["2"], 1.0)


if __name__ == "__main__":
    unittest.main()
