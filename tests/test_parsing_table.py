"""File-name, title and site-link parsing, one table per rule: every form a release names
episodes with, and the controls that must keep their answer (a fraction, an audio channel
count, a codec, a numbered film, a season pack)."""

from __future__ import annotations

import re

import pytest

from tow.episodes import parse_episode_coverage, parse_season_hint, resolve_episode_coverages


def _keys(name: str) -> list[str]:
    return [label.key for label in parse_episode_coverage(name)]


def _resolved(name: str) -> list[str]:
    (coverage,) = resolve_episode_coverages([name])
    return [label.key for label in coverage]


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


@pytest.mark.parametrize(
    ("name", "keys"),
    [
        # A numbered film, part or volume before a release tag is no episode.
        ("Movie 2 [1080p].mkv", []),
        ("Part 2 [1080p].mkv", []),
        ("Film 2 [1080p].mkv", []),
        ("Vol 2 [1080p].mkv", []),
        ("Vol. 2 [1080p].mkv", []),
        ("Фильм 2 [1080p].mkv", []),
        # Controls: an episode number before a release tag.
        ("Show 13 [1080p].mkv", ["episode:e13"]),
        ("[Grp] Show 01 [1080p].mkv", ["episode:e01"]),
        ("Rampart 2 [1080p].mkv", ["episode:e02"]),
    ],
)
def test_a_numbered_film_before_a_release_tag(name, keys):
    assert _keys(name) == keys


@pytest.mark.parametrize(
    ("name", "keys"),
    [
        # An episode titled with a special word after its SxxEyy marker is a regular episode.
        ("Show.S01E05.Special.Guest.mkv", ["episode:s01e05"]),
        ("Show.S01E05.The.Special.mkv", ["episode:s01e05"]),
        ("Show.S01E05.OVA.Story.mkv", ["episode:s01e05"]),
        ("Show.S01E05.Trailer.Park.mkv", ["episode:s01e05"]),
        ("Show.S01E05.Preview.Night.mkv", ["episode:s01e05"]),
        # Controls: the word names the file a special, a preview or a creditless opening.
        ("Show OVA S01E01.mkv", []),
        ("Show.OVA.01.mkv", []),
        ("Show OVA - 01.mkv", []),
        ("Show.Special.01.mkv", []),
        ("Show.S00E01.Special.mkv", []),
        ("Show.S01E01.NCOP.mkv", []),
        ("Show.S01E01.NCED.mkv", []),
        ("Show.S01E01.sample.mkv", []),
        ("Show.S01E01 [Preview].mkv", []),
        ("Show.S01E01-trailer.mp4", []),
        ("[Preview] Show.S01E01.mkv", []),
        ("Specials/Show.S01E01.Special.mkv", []),
    ],
)
def test_an_episode_titled_with_a_special_word(name, keys):
    assert _resolved(name) == keys


@pytest.mark.parametrize(
    ("title", "season"),
    [
        # One season with an episode range: not several seasons.
        ("Шоу (3 сезон: 1-8 серии из 10)", 3),
        ("Шоу 2 сезон 1-8 серии из 8", 2),
        ("Шоу / Сезон 1 / 12 серий из 12", 1),
        ("Шоу 3 сезон: 1-8 из 10", 3),
        ("Show Season 2: 1-8 episodes of 10", 2),
        # A possessive "'s" is no season.
        ("Ocean's 11", None),
        ("Ocean’s 11 (2001)", None),
        # Controls.
        ("Шоу / Сезон 3 / Серии 1-8 из 10", 3),
        ("Сериал / Сезон: 2 / Серии: 1-8 из 10", 2),
        ("Show [S03] (2026)", 3),
        ("Show S02, 1-10 из 10", 2),
        ("Show Season 2 / 1-10 of 10", 2),
        ("Сезон 2 - 01-10 из 10", 2),
        ("Ocean's Eleven S01", 1),
        ("Сериал (Сезон: 1-3) [WEB-DL]", None),
        ("Сезон 1-2 / Серии 1-24 из 24", None),
        ("Шоу 1-2 сезон, 1-20 из 20", None),
        ("Show S01-02 [24 из 24]", None),
        ("Show Season 1, 2 [24 из 24]", None),
        ("Show Season 1 & Season 2 [24 of 24]", None),
        ("Шоу 1 сезон и 2 сезон [24 из 24]", None),
    ],
)
def test_season_hint_of_a_title(title, season):
    assert parse_season_hint(title) == season


def _id(guessed: dict, url: str) -> str | None:
    found = re.match(guessed["url_regex"], url)
    return found.group(1) if found else None


@pytest.mark.parametrize(
    ("url", "topic_id", "download_path"),
    [
        # phpBB3 writes the forum first; the guess matched only "?t=" right after the page.
        ("https://forum.example.org/viewtopic.php?f=3&t=55", "55", "/download.php?id={id}"),
        ("https://forum.example.org/phpbb/viewtopic.php?f=3&t=55#p9", "55", "/phpbb/download.php?id={id}"),
        # A tracker in a folder keeps the folder.
        ("https://site.example.org/tracker/details.php?id=5", "5", "/tracker/download.php?id={id}"),
        # The topic number in a deeper path is found where it is.
        ("https://site.example.org/torrents/torrent/55", "55", "/download/{id}"),
        # The id parameter is the one replaced, not the same digits elsewhere.
        ("https://x.example.org/view.php?id=12&x=5123", "12", "/view.php?id={id}&x=5123"),
        ("https://x.example.org/view?cat=500&id=98765", "98765", "/view?cat=500&id={id}"),
        # Controls.
        ("https://forum.example.org/forum/viewtopic.php?t=7", "7", "/forum/download.php?id={id}"),
        ("https://rutracker.org/forum/viewtopic.php?t=7654321", "7654321", "/forum/dl.php?t={id}"),
        ("http://rutor.info/torrent/1234567/x", "1234567", "/download/{id}"),
        ("https://serials.example.org/2024/serial-12345.html", "12345", "/2024/serial-{id}.html"),
    ],
)
def test_a_guessed_site_matches_the_link_it_was_guessed_from(url, topic_id, download_path):
    from tow.guess import guess_from_url

    guessed = guess_from_url(url)
    assert _id(guessed, url) == topic_id
    assert guessed["download_path"] == download_path


def test_a_guess_that_misses_its_own_link_falls_back_to_the_number(monkeypatch):
    from tow import guess

    def wrong(parts):
        return parts.spec(url_regex=parts.rx(r"/elsewhere/(\d+)"), download_path="/x/{id}")

    monkeypatch.setattr(guess, "_path_guess", wrong)
    guessed = guess.guess_from_url("https://site.example.org/item/12345")
    assert _id(guessed, "https://site.example.org/item/12345") == "12345"
    assert guessed["download_path"] == "/item/{id}"
