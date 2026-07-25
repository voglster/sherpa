"""Unit tests for the youtube tool's VTT parsing and rendering.

Run: uv run --with pytest pytest tests/test_youtube.py
No network required — all fixtures are inline.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "youtube", Path(__file__).resolve().parent.parent / "tools" / "youtube.py"
)
youtube = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(youtube)


ROLLING_AUTO_VTT = """WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.630 align:start position:0%

Hi<00:00:00.320><c> everyone.</c><00:00:01.200><c> Welcome</c><00:00:01.600><c> to</c><00:00:01.920><c> this</c><00:00:02.360><c> video,</c>

00:00:02.630 --> 00:00:02.640 align:start position:0%
Hi everyone. Welcome to this video,


00:00:02.640 --> 00:00:04.750 align:start position:0%
Hi everyone. Welcome to this video,
this<00:00:02.880><c> is</c><00:00:03.040><c> a</c><00:00:03.400><c> walkthrough</c>

00:00:04.750 --> 00:00:04.760 align:start position:0%
this is a walkthrough

"""

CLEAN_MANUAL_VTT = """WEBVTT
Kind: captions
Language: en

1
00:00:00.000 --> 00:00:01.099
Hi everyone.

2
00:00:01.099 --> 00:00:02.433
Welcome to this video

3
00:01:05.000 --> 00:01:07.000
and here is the second paragraph.
"""


def test_rolling_auto_captions_keep_each_phrase_once():
    assert youtube.parse_vtt(ROLLING_AUTO_VTT) == [
        (0.0, "Hi everyone. Welcome to this video,"),
        (2.64, "this is a walkthrough"),
    ]


def test_inline_word_timing_tags_are_stripped():
    text = youtube.parse_vtt(ROLLING_AUTO_VTT)[0][1]
    assert "<" not in text and "</c>" not in text


def test_numbered_cues_do_not_leak_into_text():
    assert youtube.parse_vtt(CLEAN_MANUAL_VTT) == [
        (0.0, "Hi everyone."),
        (1.099, "Welcome to this video"),
        (65.0, "and here is the second paragraph."),
    ]


def test_empty_vtt_yields_no_cues():
    assert youtube.parse_vtt("WEBVTT\n\n") == []


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0:00"), (9, "0:09"), (65, "1:05"), (2746, "45:46"), (3661, "1:01:01")],
)
def test_timestamps_grow_an_hours_field_only_when_needed(seconds, expected):
    assert youtube.format_timestamp(seconds) == expected


def test_manual_english_track_beats_auto_generated():
    assert youtube.pick_track({"en": []}, {"en": [], "en-orig": []}) == ("en", False)


def test_original_auto_track_beats_translated_when_no_manual_track():
    assert youtube.pick_track({}, {"en": [], "en-orig": []}) == ("en-orig", True)


def test_regional_english_is_acceptable():
    assert youtube.pick_track({"en-GB": []}, {}) == ("en-GB", False)


def test_non_english_tracks_are_never_picked():
    assert youtube.pick_track({"de": []}, {"fr": []}) is None


def test_paragraphs_are_bucketed_by_interval_and_timestamped():
    body = youtube.render_transcript(youtube.parse_vtt(CLEAN_MANUAL_VTT), interval=30)
    assert body == (
        "[0:00] Hi everyone. Welcome to this video\n\n"
        "[1:05] and here is the second paragraph."
    )


def test_frontmatter_carries_identifying_metadata():
    doc = youtube.render_document(
        {
            "id": "abc123",
            "title": "A Talk",
            "channel": "Someone",
            "duration": 2746,
            "upload_date": "20260701",
            "webpage_url": "https://youtu.be/abc123",
            "chapters": [{"start_time": 0, "title": "Intro"}],
        },
        track=("en", False),
        cues=[(0.0, "Hello.")],
    )
    assert doc.startswith("---\n")
    assert 'title: "A Talk"' in doc
    assert "duration: 45:46" in doc
    assert "captions: en (manual)" in doc
    assert "- [0:00] Intro" in doc
    assert "[0:00] Hello." in doc


def test_chapter_section_is_omitted_when_video_has_no_chapters():
    doc = youtube.render_document(
        {"id": "x", "title": "T", "duration": 10, "chapters": None},
        track=("en", True),
        cues=[(0.0, "Hi.")],
    )
    assert "## Chapters" not in doc


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: unable to extract nsig extraction failed",
        "WARNING: Signature extraction failed: Some formats may be missing",
        "ERROR: [youtube] abc: Unable to extract player response",
    ],
)
def test_known_breakage_signatures_are_recognized_as_stale(stderr):
    assert youtube.looks_stale(stderr)


def test_missing_captions_is_not_mistaken_for_staleness():
    assert not youtube.looks_stale("ERROR: no subtitles available for this video")
