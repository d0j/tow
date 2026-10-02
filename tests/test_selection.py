import pytest
from helpers import raises_code

from tow.selection import SelectionPendingError, normalize_policy, resolve_selection
from tow.torrent import TorrentFile

FILES = (
    TorrentFile(0, "Show.S01E01.mkv", 100),
    TorrentFile(1, "Show.S01E01.en.srt", 2),
    TorrentFile(2, "Show.S01E02-E03.mkv", 200),
    TorrentFile(3, "Extras/interview.mp4", 50),
    TorrentFile(4, ".pad/16", 16, True),
)


def test_all_selects_every_non_padding_file():
    plan = resolve_selection(FILES, normalize_policy("all"))
    assert plan.selected_indices == (0, 1, 2, 3)
    assert plan.ignored_pad_files == 1


def test_episode_range_selects_video_and_subtitle_occurrences():
    plan = resolve_selection(FILES, normalize_policy("episodes", "S01E01-E02"))
    assert plan.selected_indices == (0, 1, 2)
    assert plan.selected_episode_keys == ("episode:s01e01", "episode:s01e02")


def test_episode_star_selects_only_recognized_episode_files():
    plan = resolve_selection(FILES, normalize_policy("episodes", "*"))
    assert plan.selected_indices == (0, 1, 2)
    assert "Extras/interview.mp4" not in plan.selected_files


def test_file_masks_match_path_or_basename_case_insensitively():
    plan = resolve_selection(FILES, normalize_policy("files", "*.SRT, extras/*.MP4"))
    assert plan.selected_indices == (1, 3)


@pytest.mark.parametrize("value", ["../*.mkv", "/tmp/*", "C:/*", "foo//bar"])
def test_unsafe_masks_fail_closed(value):
    with raises_code("selection.unsafe_mask", ValueError):
        resolve_selection(FILES, normalize_policy("files", value))


def test_zero_match_and_missing_episode_fail_closed():
    with raises_code("selection.nothing_matched", ValueError):
        resolve_selection(FILES, normalize_policy("files", "*.iso"))
    with raises_code("selection.not_out_yet", ValueError):
        resolve_selection(FILES, normalize_policy("episodes", "S01E99"))


def test_bare_episode_is_rejected_when_multiple_seasons_are_ambiguous():
    files = (*FILES, TorrentFile(5, "Show.S02E01.mkv", 100))
    with raises_code("selection.ambiguous", ValueError):
        resolve_selection(files, normalize_policy("episodes", "1"))


def test_season_folders_make_bare_episode_selector_ambiguous():
    files = (
        TorrentFile(0, "Season 1/01.mkv", 100),
        TorrentFile(1, "Season 2/01.mkv", 100),
    )
    with raises_code("selection.ambiguous", ValueError):
        resolve_selection(files, normalize_policy("episodes", "1"))
    plan = resolve_selection(files, normalize_policy("episodes", "S02E01"))
    assert plan.selected_indices == (1,)


def test_subtitle_only_file_selection_is_not_an_episode_selection():
    files = (TorrentFile(0, "Show.S01E01.ass", 2),)
    plan = resolve_selection(files, normalize_policy("files", "*.ass"))
    assert plan.selected_indices == (0,)
    assert plan.selected_episode_keys == ()
    with raises_code("selection.no_episodes", ValueError):
        resolve_selection(files, normalize_policy("episodes", "*"))


def test_numeric_episode_title_cannot_select_nonexistent_future_episodes():
    files = (TorrentFile(0, "Show - S01E03 - 10 Things.mkv", 100),)
    with raises_code("selection.not_out_yet", ValueError):
        resolve_selection(files, normalize_policy("episodes", "S01E05-S01E06"))


def test_three_episode_file_can_be_selected_by_its_third_episode():
    files = (TorrentFile(0, "Show.S01E01E02E03.mkv", 100),)
    plan = resolve_selection(files, normalize_policy("episodes", "S01E03"))
    assert plan.selected_indices == (0,)
    assert plan.selected_episode_keys == ("episode:s01e03",)


def test_special_video_folder_is_not_selected_as_regular_episode():
    files = (
        TorrentFile(0, "Show/Show - 01 [1080p].mkv", 100),
        TorrentFile(1, "Specials/[Grp] Show - 01 [1080p].mkv", 100),
    )
    plan = resolve_selection(files, normalize_policy("episodes", "1"))
    assert plan.selected_indices == (0,)


def test_seasonless_video_does_not_inherit_other_video_season():
    files = (
        TorrentFile(0, "Show - 01 [1080p].mkv", 100),
        TorrentFile(1, "Show S2 - 01.mkv", 100),
    )
    all_plan = resolve_selection(files, normalize_policy("all"))
    assert all_plan.selected_episode_keys == ("episode:e01", "episode:s02e01")
    with raises_code("selection.ambiguous", ValueError):
        resolve_selection(files, normalize_policy("episodes", "1"))
    season_two = resolve_selection(files, normalize_policy("episodes", "S02E01"))
    assert season_two.selected_indices == (1,)


def test_seasonless_subtitle_is_merged_into_the_only_explicit_season():
    files = (
        TorrentFile(0, "Show.S01E05.mkv", 100),
        TorrentFile(1, "Subs/05.ass", 2),
    )
    plan = resolve_selection(files, normalize_policy("episodes", "S01E05"))
    assert plan.selected_indices == (0, 1)
    assert plan.selected_episode_keys == ("episode:s01e05",)


def test_dual_numbering_does_not_select_false_range_members():
    files = (
        TorrentFile(0, "Show.S03E02-E74.mkv", 100),
        TorrentFile(1, "Show.S03E03-E75.mkv", 100),
    )
    plan = resolve_selection(files, normalize_policy("episodes", "S03E02"))
    assert plan.selected_indices == (0,)
    with raises_code("selection.not_out_yet", ValueError):
        resolve_selection(files, normalize_policy("episodes", "S03E10"))


def test_season_dash_episode_form_is_selectable():
    files = tuple(TorrentFile(index, f"Show S1 - {index + 1:02d}.mkv", 100) for index in range(3))
    plan = resolve_selection(files, normalize_policy("episodes", "S01E02"))
    assert plan.selected_indices == (1,)


def test_crossing_digit_width_dual_numbering_selects_only_requested_file():
    files = tuple(
        TorrentFile(index, f"Show.S04E{episode:02d}-E{episode + 98}.mkv", 100)
        for index, episode in enumerate(range(1, 6))
    )
    plan = resolve_selection(files, normalize_policy("episodes", "S04E03"))
    assert plan.selected_indices == (2,)


def test_preferred_season_keeps_selection_and_progress_identity_canonical():
    files = (TorrentFile(0, "Season 1/01.mkv", 100),)
    plan = resolve_selection(
        files,
        normalize_policy("episodes", "S01E01"),
        preferred_season=1,
    )
    assert plan.selected_indices == (0,)
    assert plan.selected_episode_keys == ("episode:s01e01",)


def test_numeric_extra_does_not_join_episode_selection():
    files = (
        TorrentFile(0, "Show.S01E01.mkv", 100),
        TorrentFile(1, "Extras/01.mkv", 100),
    )
    plan = resolve_selection(files, normalize_policy("episodes", "S01E01"))
    assert plan.selected_indices == (0,)


def test_sample_clip_does_not_join_episode_selection():
    files = (
        TorrentFile(0, "Show.S01E01.mkv", 100),
        TorrentFile(1, "Show.S01E01.sample.mkv", 10),
    )
    plan = resolve_selection(files, normalize_policy("episodes", "S01E01"))
    assert plan.selected_indices == (0,)


@pytest.mark.parametrize("value", ["1-10", "S01E01-S01E10", "S1E1-10"])
def test_episode_ranges_reaching_past_released_episodes_select_what_exists(value):
    plan = resolve_selection(FILES, normalize_policy("episodes", value))
    assert plan.selected_indices == (0, 1, 2)
    assert set(plan.selected_episode_keys) == {"episode:s01e01", "episode:s01e02", "episode:s01e03"}


@pytest.mark.parametrize("value", ["5", "S01E05", "S01E05-S01E07"])
def test_episode_selection_with_nothing_released_yet_fails_closed(value):
    # Still refused (a ValueError); the check treats it as "waiting" for watch topics (B8).
    with raises_code("selection.not_out_yet", SelectionPendingError):
        resolve_selection(FILES, normalize_policy("episodes", value))


@pytest.mark.parametrize("expression", ["S01E11-20", "11-20", "S02E01-05"])
def test_a_range_entirely_after_the_torrent_is_pending_not_absent(expression):
    files = [TorrentFile(i, f"Show.S01E{i + 1:02d}.mkv", 1) for i in range(10)]

    with raises_code("selection.not_out_yet", SelectionPendingError):
        resolve_selection(files, normalize_policy("episodes", expression))


def test_a_missing_episode_inside_the_aired_range_is_still_an_error():
    files = [TorrentFile(i, f"Show.S01E{n:02d}.mkv", 1) for i, n in enumerate((1, 2, 4, 5))]

    with raises_code("selection.absent", ValueError) as caught:
        resolve_selection(files, normalize_policy("episodes", "S01E03"))
    assert not isinstance(caught.value, SelectionPendingError)
