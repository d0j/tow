import pytest

from tow.episodes import (
    expected_for_topic,
    parse_episode_coverage,
    parse_expected_count,
    parse_season_hint,
    resolve_episode_coverages,
    summarize_completion,
)


def test_episode_coverage_handles_common_range_forms_and_basename_only():
    assert [item.key for item in parse_episode_coverage("Season 1/Show.S01E03-E05.mkv")] == [
        "episode:s01e03",
        "episode:s01e04",
        "episode:s01e05",
    ]
    assert [item.key for item in parse_episode_coverage("04x01-03.mkv")] == [
        "episode:s04e01",
        "episode:s04e02",
        "episode:s04e03",
    ]
    assert parse_episode_coverage("Season.S04E01-22/cover.jpg") == ()
    assert [item.key for item in parse_episode_coverage("Show - S01E05 - 720p.mkv")] == ["episode:s01e05"]
    assert [item.key for item in parse_episode_coverage("Show - S01E05 - 1080p.mkv")] == ["episode:s01e05"]


def test_episode_coverage_accepts_common_forms_and_context_resolves_absolute_alias():
    ambiguous = parse_episode_coverage("Show.S03E02-E074.mkv")
    assert ambiguous[0].key == "episode:s03e02"
    assert ambiguous[-1].key == "episode:s03e74"
    assert [item.key for item in parse_episode_coverage("Show S1 - 01.mkv")] == ["episode:s01e01"]
    assert [item.key for item in parse_episode_coverage("Show.S01E01-02.mkv")] == [
        "episode:s01e01",
        "episode:s01e02",
    ]
    assert [item.key for item in parse_episode_coverage("Show.E05.mkv")] == ["episode:e05"]
    assert [item.key for item in parse_episode_coverage("5 серия.mkv")] == ["episode:e05"]
    assert [item.key for item in parse_episode_coverage("[05].mkv")] == ["episode:e05"]


def test_episode_coverage_accepts_spaced_dash_anime_release_numbers():
    assert [
        item.key for item in parse_episode_coverage("[Double-Raws] Some Show 3 - 13 RAW (WEB 1280x720 x264 AAC).mkv")
    ] == ["episode:e13"]
    assert [item.key for item in parse_episode_coverage("Show - 02 [1080p].ass")] == ["episode:e02"]
    assert parse_episode_coverage("Blade Runner 2049 - 1080p.mkv") == ()


def test_season_hint_requires_explicit_or_episode_corroborated_title_syntax():
    assert parse_season_hint("Show [S03] (2026)") == 3
    assert parse_season_hint("Show [03x01-13 из 14]") == 3
    assert parse_season_hint("Сериал В 3: Другой мир [13 из 14]") == 3
    assert parse_season_hint("Blade Runner 2049: The Movie") is None
    assert parse_season_hint("Show 31: Chapter [1 из 2]") is None


@pytest.mark.parametrize(
    ("title", "season"),
    [
        ("Сериал / Сезон: 2 / Серии: 1-8 из 10", 2),
        ("Сериал / Сезон №4 / Серии: 1-8 из 10", 4),
        ("Show Season #3 [1080p]", 3),
        ("Сериал (Сезон: 1-3) [WEB-DL]", None),
    ],
)
def test_season_hint_accepts_tracker_punctuation_after_the_season_word(title, season):
    # B6: rutracker-style "Сезон: 2" was not a season hint.
    assert parse_season_hint(title) == season


def test_file_set_collapses_heavily_overlapping_pseudo_ranges_only():
    dual = resolve_episode_coverages(["Show.S03E02-E74.mkv", "Show.S03E03-E75.mkv", "Show.S03E04-E76.mkv"])
    assert [[item.key for item in coverage] for coverage in dual] == [
        ["episode:s03e02"],
        ["episode:s03e03"],
        ["episode:s03e04"],
    ]
    genuine = resolve_episode_coverages(["Show.S01E01-E03.mkv", "Show.S01E04-E06.mkv"])
    assert [len(coverage) for coverage in genuine] == [3, 3]


def test_file_set_collapses_absolute_alias_when_only_first_range_crosses_digit_width():
    rows = ["Show.S04E01-E99.mkv"] + [f"Show.S04E{episode:02d}-E{episode + 98}.mkv" for episode in range(2, 6)]
    resolved = resolve_episode_coverages(rows)
    assert [[label.episode for label in coverage] for coverage in resolved] == [[1], [2], [3], [4], [5]]


def test_season_pack_title_is_not_misread_as_episode():
    assert parse_episode_coverage("Show Season 1-3 extras.mkv") == ()


def test_single_long_ambiguous_range_fails_closed_but_adjacent_boundary_is_kept():
    ambiguous = resolve_episode_coverages(["Show.S03E02-E074.mkv", "Show.nfo"])
    assert [[label.key for label in coverage] for coverage in ambiguous] == [
        ["episode:s03e02"],
        [],
    ]
    adjacent = resolve_episode_coverages(["Show.S01E99-E100.mkv"])
    assert [[label.key for label in coverage] for coverage in adjacent] == [["episode:s01e99", "episode:s01e100"]]


def test_multi_episode_lists_are_not_silently_truncated():
    assert [label.key for label in parse_episode_coverage("Show.S01E01&E02.mkv")] == [
        "episode:s01e01",
        "episode:s01e02",
    ]
    assert [label.key for label in parse_episode_coverage("Show.S01E01-E02-E03.mkv")] == [
        "episode:s01e01",
        "episode:s01e02",
        "episode:s01e03",
    ]


def test_fractional_special_does_not_collide_with_integer_episode():
    assert parse_episode_coverage("Show.S01E12.5.mkv") == ()


def test_bare_numeric_extras_years_and_resolutions_are_not_episodes():
    assert parse_episode_coverage("Extras/01.mkv") == ()
    assert parse_episode_coverage("Бонусы/01.mkv") == ()
    assert parse_episode_coverage("Доп. материалы/02.mkv") == ()
    assert parse_episode_coverage("NCOP/03.mkv") == ()
    assert parse_episode_coverage("Sample/04.mkv") == ()
    assert parse_episode_coverage("1080.mkv") == ()
    assert parse_episode_coverage("2026.mkv") == ()
    assert [label.key for label in parse_episode_coverage("13.mkv")] == ["episode:e13"]


def test_named_preview_video_does_not_duplicate_the_real_episode():
    names = (
        "Show.S01E01.mkv",
        "Show.S01E01.sample.mkv",
        "Show S01E01 Sample.mkv",
        "Show.S01E01 [Preview].mkv",
        "Show.S01E01-trailer.mp4",
        "[Preview] Show.S01E01.mkv",
        "Show.S01E02.mkv",
    )
    resolved = resolve_episode_coverages(names)
    assert [[label.key for label in coverage] for coverage in resolved] == [
        ["episode:s01e01"],
        [],
        [],
        [],
        [],
        [],
        ["episode:s01e02"],
    ]


def test_postposed_and_bracketed_numbers_reject_zero_years_and_resolutions():
    for name in ("0 серия.mkv", "2026 серия.mkv", "[0].mkv", "[2026].mkv", "[1080].mkv"):
        assert parse_episode_coverage(name) == (), name
    assert [label.key for label in parse_episode_coverage("5 серия.mkv")] == ["episode:e05"]
    assert [label.key for label in parse_episode_coverage("[05].mkv")] == ["episode:e05"]


def test_expected_count_rejects_part_season_and_volume_totals():
    assert parse_expected_count("Show [13 из 14]")["total"] == 14
    assert parse_expected_count("Show Part 2 of 3") is None
    assert parse_expected_count("Show Season 2 of 4") is None
    assert parse_expected_count("Show Volume 1 of 2") is None


def test_expected_count_prefers_episode_total_after_season_total():
    titles = (
        "Bleach (сезон 1 из 16, серии 1-20 из 20)",
        "1 сезон из 16, серии 1-20 из 20",
        "Сезон: 1 из 16 / Серии: 1-20 из 20",
        "[S01 из 16] [1-20 из 20]",
        "сезоны 1-2 из 16, серии 1-20 из 20",
        "сезон 1 из 16 серии 1-20 из 20",
        "серии 1-20 из 20, сезон 1 из 16",
        "Episodes 1-20 of 20",
    )
    assert [parse_expected_count(title)["total"] for title in titles] == [20] * len(titles)


def test_expected_count_accepts_episode_range_after_season_in_same_clause():
    for title in (
        "Сезон 2 серии 1-5 из 10",
        "Season 2 Episodes 1-5 of 10",
        "Show S02 01-05 of 10",
    ):
        assert parse_expected_count(title)["total"] == 10, title
    assert parse_expected_count("Show Season 2 of 4") is None
    assert parse_expected_count("Show Season 1-2 of 4") is None
    assert parse_expected_count("Episode 1 of 10 seasons") is None
    assert parse_expected_count("Show [2 из 4-х сезонов]") is None
    assert parse_expected_count("Show [2 из 4-x сезонов]") is None


def test_expected_count_rejects_prose_and_conflicting_or_invalid_totals():
    for title in (
        "Девочка из 5 класса",
        "Film (2004) 1 из 3",
        "Show [1-20 из 16]",
        "Show [1 из 12] [2 из 14]",
        "Show [1 из ??]",
        "сезон 2 из 5",
        "Show (1-16 из 16 сезонов)",
    ):
        assert parse_expected_count(title) is None
    assert parse_expected_count("Season 2 of 4 [5 of 10]")["total"] == 10


def test_current_episode_count_ignores_stale_or_extras_only_selection():
    topic = {
        "title": "Show [1 из 20]",
        "hash": "new",
        "selection_hash": "old",
        "selection_verified": True,
        "selection": {"mode": "all"},
        "selected_episode_keys": [f"episode:s01e{episode:02d}" for episode in range(1, 21)],
    }
    assert expected_for_topic(topic)["source"] == "title"
    topic.update(selection={"mode": "masks"}, selected_episode_keys=[])
    assert expected_for_topic(topic) is None


def test_numeric_episode_title_is_not_an_episode_range():
    for name in ("Show - S01E03 - 10 Things.mkv", "Show 1x03 - 24 Hours.mkv"):
        assert [label.key for label in parse_episode_coverage(name)] == ["episode:s01e03"]


def test_explicit_episode_pairs_and_common_anime_file_names():
    expected_pairs = (
        "Show.E01-E02.mkv",
        "Show.E01&E02.mkv",
        "Show Episode 01-02.mkv",
        "Show - 01-02 [1080p].mkv",
        "Show.S01E01E02.mkv",
        "Show.S01E01.S01E02.mkv",
    )
    for name in expected_pairs:
        assert [label.episode for label in parse_episode_coverage(name)] == [1, 2], name
    singles = (
        "Show - 01 - Title.mkv",
        "[Grp] Show 01 [1080p].mkv",
        "Show_-_01_[1080p].mkv",
        "01. Title.mkv",
        "[Grp] Show - 01v2 [1080p].mkv",
    )
    for name in singles:
        assert [label.episode for label in parse_episode_coverage(name)] == [1], name


def test_title_total_excludes_special_zero_and_year_from_completion():
    expected = expected_for_topic({"title": "Show [TV+OVA] [Серии 1-11 из 12]"})
    assert expected["total"] == 12
    items = {
        f"regular-{number}": {
            "episode_key": f"episode:s01e{number:02d}",
            "status": "completed",
        }
        for number in range(1, 12)
    }
    items.update(
        {
            "special": {"episode_key": "episode:s00e01", "status": "completed"},
            "zero": {"episode_key": "episode:s01e00", "status": "completed"},
            "year": {"episode_key": "episode:e2021", "status": "completed"},
        }
    )
    summary = summarize_completion(items, expected)
    assert summary["completed"] == 11
    assert summary["is_complete"] is False
    assert parse_episode_coverage("[Grp] Show - 2021 [BDRip].mkv") == ()
    assert parse_episode_coverage("Show.S01E00.mkv") == ()


def test_absolute_second_cour_title_uses_its_own_episode_window():
    expected = expected_for_topic({"title": "Show 2nd Season [Серии 13-24 из 24]"})
    assert expected["total"] == 12
    items = {str(number): {"episode_key": f"episode:e{number:02d}", "status": "completed"} for number in range(13, 25)}
    assert summarize_completion(items, expected)["is_complete"] is True


def test_season_folders_disambiguate_bare_episode_names():
    coverages = resolve_episode_coverages(["Season 1/01.mkv", "Season 2/01.mkv"])
    assert [[label.key for label in row] for row in coverages] == [
        ["episode:s01e01"],
        ["episode:s02e01"],
    ]


def test_resolution_after_episode_number_is_not_a_fractional_special():
    for resolution in ("720p", "1080p", "2160p"):
        name = f"Show.S01E05.{resolution}.WEB-DL.mkv"
        assert [label.key for label in parse_episode_coverage(name)] == ["episode:s01e05"]
    assert parse_episode_coverage("Show.S01E12.5.mkv") == ()


def test_multi_season_title_does_not_filter_completion_to_first_season():
    items = {
        f"s{season}e{episode}": {
            "episode_key": f"episode:s{season:02d}e{episode:02d}",
            "status": "completed",
        }
        for season in (1, 2)
        for episode in range(1, 13)
    }
    for title in (
        "Show S01-S02 [24 из 24]",
        "Show S01+S02 [24 из 24]",
        "Show S01 & S02 [24 из 24]",
        "Show Season 1, 2 [24 из 24]",
        "Show Season 1+2 [24 из 24]",
        "Шоу 1 и 2 сезон [24 из 24]",
        "Show 1 and 2 season [24 of 24]",
        "Show S01-02 [24 из 24]",
        "Show Season 1 & Season 2 [24 of 24]",
        "Show Season 1 to 2 [24 of 24]",
        "Show S01E01-S02E12 [24 из 24]",
        "Шоу 1 сезон и 2 сезон [24 из 24]",
        "Шоу 1 сезон + 2 сезон [24 из 24]",
        "Show S01 S02 [24 из 24]",
    ):
        assert parse_season_hint(title) is None, title
        expected = expected_for_topic({"title": title})
        assert summarize_completion(items, expected)["is_complete"] is True, title
    assert parse_season_hint("Сезон 1-2 / Серии 1-24 из 24") is None
    assert parse_season_hint("Show S02, 1-10 из 10") == 2
    assert parse_season_hint("Show Season 2 / 1-10 of 10") == 2
    assert parse_season_hint("Сезон 2 - 01-10 из 10") == 2


def test_three_episode_file_styles_keep_all_members():
    for name in (
        "Show.S01E01E02E03.mkv",
        "Show.S01E01.S01E02.S01E03.mkv",
        "Show.S01E01-02-03.mkv",
    ):
        assert [label.episode for label in parse_episode_coverage(name)] == [1, 2, 3], name


def test_specials_folder_overrides_episode_like_filename():
    coverages = resolve_episode_coverages(
        (
            "Show/Show - 01 [1080p].mkv",
            "Specials/[Grp] Show - 01 [1080p].mkv",
        )
    )
    assert [[label.key for label in row] for row in coverages] == [["episode:e01"], []]


@pytest.mark.parametrize("folder", ["Season 00", "Season_0", "S00", "Сезон 0", "0 сезон", "Season 00 (2026)"])
@pytest.mark.parametrize("name", ["01.mkv", "Show.S01E01.mkv", "01.ass"])
def test_zero_season_folder_is_special_even_with_regular_numbering(folder, name):
    assert resolve_episode_coverages([f"{folder}/{name}"]) == ((),)


@pytest.mark.parametrize(
    "name",
    [
        "Show.S00E01&E02.mkv",
        "Show.S01E01&E00.mkv",
        "Show.S00E01+E02+E03.mkv",
        "Show S0-Ep01.mkv",
        "Show Season 0 Episode 1.mkv",
        "Show Сезон 0 Серия 1.mkv",
    ],
)
def test_explicit_zero_numbering_never_becomes_regular_episode(name):
    assert parse_episode_coverage(name) == ()


def test_crc_audio_and_inline_ova_do_not_invent_regular_episodes():
    assert [label.key for label in parse_episode_coverage("[Grp] Show - 05 [E3A1B2C4].mkv")] == ["episode:e05"]
    assert parse_episode_coverage("Movie.2019.DD5.1x264-G.mkv") == ()
    assert parse_episode_coverage("Show.DTS-HD.MA.5.1x264.mkv") == ()
    assert resolve_episode_coverages(["Show OVA - 01.mkv"]) == ((),)


def test_inline_ona_with_explicit_season_episodes_is_not_a_special():
    names = (
        "Show/Show.ONA.S01E01.mkv",
        "Show/Show.ONA.S01E02.mkv",
    )
    assert [[label.key for label in row] for row in resolve_episode_coverages(names)] == [
        ["episode:s01e01"],
        ["episode:s01e02"],
    ]
    assert resolve_episode_coverages(["Show ONA - 01.mkv"]) == ((),)
    assert resolve_episode_coverages(["Show.ONA.S00E01.mkv"]) == ((),)
    assert resolve_episode_coverages(["Show OVA S01E01.mkv"]) == ((),)
    assert resolve_episode_coverages(["Specials/Show.ONA.S01E01.mkv"]) == ((),)
    dual = resolve_episode_coverages(
        [f"Show/Show.ONA.S03E{episode:02d}-E{episode + 72:03d}.mkv" for episode in range(2, 6)]
    )
    assert [[label.key for label in row] for row in dual] == [
        ["episode:s03e02"],
        ["episode:s03e03"],
        ["episode:s03e04"],
        ["episode:s03e05"],
    ]


def test_postposed_episode_word_gives_a_safe_growing_total():
    expected = parse_expected_count("Show (1-2 сезоны: 1-31 серии из 52)")
    assert expected is not None
    assert expected["total"] == 52
    assert expected["episode_min"] == 1
    # A later-starting mixed-season pack needs the global-to-season mapping
    # before its title range can be used as an exact completion target.
    assert parse_expected_count("Show (73-176 серии из 176)") is None


def test_russian_plural_episode_range_and_extended_season_folders():
    assert [label.episode for label in parse_episode_coverage("Show 1-5 серии.mkv")] == [1, 2, 3, 4, 5]
    coverages = resolve_episode_coverages(("Season 02 (2020)/01.mkv", "2 сезон/02.mkv"))
    assert [[label.key for label in row] for row in coverages] == [
        ["episode:s02e01"],
        ["episode:s02e02"],
    ]


def test_codec_and_audio_suffixes_cannot_expand_one_episode_into_many():
    cases = (
        "Show.S01E01-10bit.mkv",
        "Show.S01E01-8bit.mkv",
        "Show.S01E01-4K.mkv",
        "Show.S01E01-5.1.mkv",
    )
    for name in cases:
        assert [label.key for label in parse_episode_coverage(name)] == ["episode:s01e01"], name
    assert [label.key for label in parse_episode_coverage("Show.2x01-10bit.mkv")] == ["episode:s02e01"]
    assert [label.key for label in parse_episode_coverage("Show.S01E01-02.mkv")] == ["episode:s01e01", "episode:s01e02"]
    assert [label.key for label in parse_episode_coverage("Show.E05.AAC2.0x264.mkv")] == ["episode:e05"]


def test_reversed_multi_season_title_does_not_pick_last_season():
    assert parse_season_hint("Шоу 1-2 сезон, 1-20 из 20") is None


def test_episode_prefix_and_repeated_e_quality_suffixes_do_not_expand_ranges():
    cases = (
        ("Show.Ep01-10bit.mkv", "episode:e01"),
        ("Show E01-10bit.mkv", "episode:e01"),
        ("Show.S01E01-10-bit.mkv", "episode:s01e01"),
        ("Show.S01E01-10 bit.mkv", "episode:s01e01"),
        ("Show.E01-6.1x264.mkv", "episode:e01"),
        ("Show Ep 01 - 5.1 AAC.mkv", "episode:e01"),
        ("Show.Ep01-4K.mkv", "episode:e01"),
        ("Show.S01E01.E2.0.mkv", "episode:s01e01"),
        ("Show.S01E01.E10bit.mkv", "episode:s01e01"),
    )
    for name, key in cases:
        assert [label.key for label in parse_episode_coverage(name)] == [key], name
