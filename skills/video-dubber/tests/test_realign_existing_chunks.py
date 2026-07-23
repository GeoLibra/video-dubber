from scripts.realign_existing_chunks import build_groups, speed_summary


class Sub:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


def test_grouping_requires_explicit_single_speaker_assumption():
    subs = [Sub(0, 1200, "Hello!"), Sub(1250, 3000, "Next sentence.")]
    assert build_groups(
        subs, assume_single_speaker=False, min_group_ms=3200, max_group_ms=9000, max_gap_ms=300
    ) == [[0], [1]]


def test_single_speaker_fragments_can_share_a_semantic_window():
    subs = [
        Sub(0, 1200, "This is episode two,"),
        Sub(1250, 6000, "and the final episode."),
    ]
    assert build_groups(
        subs, assume_single_speaker=True, min_group_ms=3200, max_group_ms=9000, max_gap_ms=300
    ) == [[0, 1]]


def test_speed_summary_reports_tiers_and_abrupt_changes():
    items = [
        {"group_index": 0, "source_indexes": [0], "start_ms": 0, "end_ms": 1000, "atempo_ratio": 1.0},
        {"group_index": 1, "source_indexes": [1], "start_ms": 1000, "end_ms": 2000, "atempo_ratio": 1.4},
    ]
    counts, notices, abrupt = speed_summary(items)
    assert counts["obvious"] == 1
    assert notices[0]["start_ms"] == 1000
    assert len(abrupt) == 1
