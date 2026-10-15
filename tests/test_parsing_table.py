"""File-name, title and site-link parsing, one table per rule: every form a release names
episodes with, and the controls that must keep their answer (a fraction, an audio channel
count, a codec, a numbered film, a season pack)."""

from __future__ import annotations

import pytest

from tow.episodes import parse_episode_coverage


def _keys(name: str) -> list[str]:
    return [label.key for label in parse_episode_coverage(name)]


S01E01_02 = ["episode:s01e01", "episode:s01e02"]


@pytest.mark.parametrize(
    ("name", "keys"),
    [
        # A multi-episode marker followed by a quality, a bit depth or a year (".1080p" was read
        # as the start of a fraction: only the first episode was counted).
        ("Show.S01E01-E02.1080p.mkv", S01E01_02),
        ("Show.S01E01E02.1080p.mkv", S01E01_02),
        ("Show.S01E01.S01E02.1080p.mkv", S01E01_02),
        ("Show.S01E01-02.1080p.mkv", S01E01_02),
        ("Show.E01-E02.1080p.mkv", ["episode:e01", "episode:e02"]),
        ("Show.1x01-1x02.1080p.mkv", S01E01_02),
        ("Show.S01E01-E02.720p.mkv", S01E01_02),
        ("Show.S01E01-E02.2160p.mkv", S01E01_02),
        ("Show.S01E01-E02.10bit.mkv", S01E01_02),
        ("Show.S01E01-E02.4K.mkv", S01E01_02),
        ("Show.S01E01-E02.2019.mkv", S01E01_02),
        ("Show.S01E01E02.10bit.mkv", S01E01_02),
        # Controls: a fraction, an audio channel count or a codec after the number stays out.
        ("Show.S01E01-E02.mkv", S01E01_02),
        ("Show.S01E05.720p.WEB-DL.mkv", ["episode:s01e05"]),
        ("Show.S01E05.5.mkv", []),
        ("Show.S01E12.5.mkv", []),
        ("Show.S01E01-E02.5.mkv", ["episode:s01e01"]),
        ("Show.S01E01-5.1.mkv", ["episode:s01e01"]),
        ("Show.S01E01-10bit.mkv", ["episode:s01e01"]),
        ("Show.E01-6.1x264.mkv", ["episode:e01"]),
        ("Show Ep 01 - 5.1 AAC.mkv", ["episode:e01"]),
        ("Show.S01E01.E2.0.mkv", ["episode:s01e01"]),
        ("Show.S01E01.E10bit.mkv", ["episode:s01e01"]),
        ("Show.2x01-10bit.mkv", ["episode:s02e01"]),
    ],
)
def test_a_multi_episode_marker_before_a_quality_tag(name, keys):
    assert _keys(name) == keys


@pytest.mark.parametrize(
    ("name", "keys"),
    [
        # "1 сезон 01 серия": the season named before the episode belongs to it.
        ("1 сезон 01 серия.mkv", ["episode:s01e01"]),
        ("2 сезон 03 серия.mkv", ["episode:s02e03"]),
        ("Show 2 сезон 3 серия.mkv", ["episode:s02e03"]),
        ("Show 0 сезон 01 серия.mkv", []),
        # Controls: the word order that already worked, and a season range names no season.
        ("Сезон 1 Серия 5.mkv", ["episode:s01e05"]),
        ("5 серия.mkv", ["episode:e05"]),
        ("Show 1-2 сезон 05 серия.mkv", ["episode:e05"]),
    ],
)
def test_a_season_number_before_its_word(name, keys):
    assert _keys(name) == keys
