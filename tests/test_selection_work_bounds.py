from __future__ import annotations

import fnmatch
import random
import subprocess
import sys
from pathlib import Path

import pytest

import tow.selection
from tow.selection import SelectionError, SelectionPendingError, normalize_policy, resolve_selection
from tow.torrent import TorrentFile


@pytest.mark.allow_system
@pytest.mark.parametrize("scenario", ["repeated", "overlapping", "future", "metadata-limit"])
def test_large_episode_rules_finish_in_a_bounded_isolated_process(scenario):
    # A deadline terminates only this read-only probe, not a client or service.
    # No network, files, state or media operations; use the locked interpreter.
    script = """
import sys
from pathlib import Path
import tow.selection as selection
from tow.torrent import MAX_FILES, TorrentFile

assert Path(selection.__file__).resolve() == Path(sys.argv[1])
files = tuple(TorrentFile(i, f'Show.S01E{i+1:04d}.mkv', 1) for i in range(1000))
scenario = sys.argv[2]
if scenario == 'metadata-limit':
    files = tuple(TorrentFile(i, f'Copy {i//1000}/Show.S01E{i%1000+1:04d}.mkv', 1) for i in range(MAX_FILES))
if scenario in {'repeated', 'metadata-limit'}:
    expression = ','.join(['1-1000'] * 300)
elif scenario == 'overlapping':
    expression = ','.join(f'{n}-{n+600}' for n in range(1, 451))
else:
    expression = ','.join(['S01E2000-E3000'] * 300)
policy = selection.normalize_policy('episodes', expression)
if scenario == 'future':
    try:
        selection.resolve_selection(files, policy)
    except selection.SelectionPendingError as error:
        assert error.code == 'selection.not_out_yet'
        assert error.params['episodes'] == ', '.join(f'S01E{n}' for n in range(2000, 2010))
    else:
        raise AssertionError('future episodes cannot be selected yet')
else:
    plan = selection.resolve_selection(files, policy)
    assert plan.selected_indices == tuple(range(len(files)))
    assert len(plan.selected_episode_keys) == 1000
    assert plan.expression == expression
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(Path(tow.selection.__file__).resolve()), scenario],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize(
    "expression",
    [
        "*.MKV",
        "*.mkv,*.MKV,*.mkv",
        "Season*/*",
        "*S??E[0-9][!x]*.*",
        "*.srt",
        "*[[]*",
        "*straße*",
        "*日本語*",
        "missing*",
        "*|*,*.mkv",
        "*.srt,*[[]*",
        "[!a]*,*.MKV",
        "[],[!],*.mkv",
        "*?*?*,*straße*",
        "*日本語*,*straße*,*日本語*",
    ],
)
def test_prepared_file_matchers_preserve_path_basename_and_wildcard_semantics(expression):
    paths = (
        "Show.S01E01.mkv",
        "Season 2/Show.S02E03.MKV",
        "Season 2/Show.S02E03.srt",
        "Extras/[interview].mp4",
        "Straße.mkv",
        "日本語.txt",
        ".hidden.bin",
    )
    files = tuple(TorrentFile(i, path, 1) for i, path in enumerate(paths))
    patterns = [pattern.casefold() for pattern in expression.split(",")]
    expected = tuple(
        row.index
        for row in files
        if any(
            fnmatch.fnmatchcase(row.path.casefold(), pattern)
            or fnmatch.fnmatchcase(row.path.rsplit("/", 1)[-1].casefold(), pattern)
            for pattern in patterns
        )
    )
    policy = normalize_policy("files", expression)
    if not expected:
        with pytest.raises(SelectionError) as caught:
            resolve_selection(files, policy)
        assert caught.value.code == "selection.nothing_matched"
    else:
        plan = resolve_selection(files, policy)
        assert plan.selected_indices == expected
        assert plan.expression == expression


def test_distinct_file_masks_compile_bounded_groups_without_changing_the_rule(monkeypatch):
    original = tow.selection.re.compile
    compiled = []

    def record(pattern, *args, **kwargs):
        compiled.append(pattern)
        return original(pattern, *args, **kwargs)

    expression = "missing*,*.MKV,*[[]*,*.mkv"
    files = (TorrentFile(0, "Season 1/Show.mkv", 1), TorrentFile(1, "Extras/[Interview].mp4", 1))
    monkeypatch.setattr(tow.selection.re, "compile", record)
    plan = resolve_selection(files, normalize_policy("files", expression))
    assert plan.selected_indices == (0, 1)
    assert plan.expression == expression
    # One literal filter, one exact guarded union, one bracket-syntax union;
    # never a Python matcher dispatch for every file/pattern pair.
    assert len(compiled) == 3


@pytest.mark.parametrize(
    "patterns",
    [
        ("*", "?*?"),
        ("*.mkv", "*absent*"),
        ("*[[]*", "[!a]*", "[z-a]", "[!z-a]"),
        ("[]", "[!]", "[", "[]a]", "[[:alpha:]]", "[a&&b]", "[a--b]"),
        ("*literal*later*", "?literal?", "literal*", "*later", "**literal**"),
        ("*|*", "*.*", "*straße*", "*日本語*", "*\n*"),
        ("*same*.mkv", "*same*.srt", "*same*.bin", "[!a]*", "*?*?*"),
    ],
)
def test_literal_prefilter_matches_standard_globs_on_generated_candidates(patterns):
    rng = random.Random(261008)
    normalized = tuple(pattern.casefold() for pattern in patterns)
    matches = tow.selection._file_mask_matcher(normalized)
    candidates = [
        "",
        "Show.mkv",
        "Folder/Show.MKV",
        "literal",
        "literal/later",
        "same.mkv",
        "[]",
        "[!]",
        "[",
        "]",
        "a",
        "b",
        "Straße",
        "日本語",
        "|",
        ".hidden",
        "\n",
    ]
    candidates.extend("".join(rng.choices("ab[]!*?-.|/\n日本語ß", k=rng.randrange(1, 50))) for _ in range(1000))
    for candidate in candidates:
        path = candidate.casefold()
        assert matches(path) == any(fnmatch.fnmatchcase(path, pattern) for pattern in normalized), (path, patterns)


def test_literal_filter_never_accepts_a_wrong_exact_match_or_hides_bracket_matches():
    matches = tow.selection._file_mask_matcher(("a*middle*z", "?fixed?", "*[[]*"))
    assert not matches("middle")
    assert not matches("fixed")
    assert matches("amiddlez")
    assert matches("afixedz")
    assert matches("only[a bracket]")


def test_file_mask_groups_are_bounded_at_the_maximum_rule_count(monkeypatch):
    original = tow.selection.re.compile
    compiled = []

    def record(pattern, *args, **kwargs):
        compiled.append(pattern)
        return original(pattern, *args, **kwargs)

    monkeypatch.setattr(tow.selection.re, "compile", record)
    patterns = [f"*absent-{index:03d}*" for index in range(499)] + ["*[[]*"]
    matches = tow.selection._file_mask_matcher(patterns)
    assert len(compiled) == 3
    assert not matches("ordinary.bin")
    assert matches("[extra].bin")


def test_repeated_unavailable_rules_keep_the_original_diagnostic_order():
    files = (TorrentFile(0, "Show.S01E07.mkv", 1),)
    with pytest.raises(SelectionPendingError) as caught:
        resolve_selection(files, normalize_policy("episodes", "S02E08,S02E08,8"))
    assert caught.value.params["episodes"] == "S02E08, S02E08, 8"


def test_subtitle_season_still_makes_an_absent_bare_number_ambiguous():
    files = (TorrentFile(0, "Show.S01E01.mkv", 1), TorrentFile(1, "Show.S02E99.srt", 1))
    with pytest.raises(SelectionError) as caught:
        resolve_selection(files, normalize_policy("episodes", "2,2"))
    assert caught.value.code == "selection.ambiguous"
    assert caught.value.params["episode"] == 2


def test_a_seasonless_video_prevents_guessing_that_another_season_is_in_the_future():
    files = (TorrentFile(0, "Show - 01.mkv", 1), TorrentFile(1, "Show.S02E01.mkv", 1))
    with pytest.raises(SelectionError) as caught:
        resolve_selection(files, normalize_policy("episodes", "S03E01,S03E01"))
    assert not isinstance(caught.value, SelectionPendingError)
    assert caught.value.code == "selection.absent"
    assert caught.value.params["episodes"] == "S03E01, S03E01"


@pytest.mark.allow_system
@pytest.mark.parametrize("scenario", ["repeated", "distinct"])
def test_large_file_rules_finish_in_a_bounded_isolated_process(scenario):
    script = """
import sys
from pathlib import Path
import tow.selection as selection
from tow.torrent import MAX_FILES, TorrentFile

assert Path(selection.__file__).resolve() == Path(sys.argv[1])
files = tuple(TorrentFile(i, f'Asset {i:05d}.bin', 1) for i in range(MAX_FILES))
patterns = ['*never-matches*' if sys.argv[2] == 'repeated' else f'*absent-{i:03d}*' for i in range(500)]
expression = ','.join(patterns)
policy = selection.normalize_policy('files', expression)
assert policy['value'] == expression
try:
    selection.resolve_selection(files, policy)
except selection.SelectionError as error:
    assert error.code == 'selection.nothing_matched'
else:
    raise AssertionError('unmatched masks cannot select files')
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(Path(tow.selection.__file__).resolve()), scenario],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == "ok"


@pytest.mark.allow_system
@pytest.mark.parametrize("nested", [False, True])
def test_parsed_large_torrents_with_long_paths_finish_file_selection(nested):
    script = """
import sys
from pathlib import Path
import tow.selection as selection
from tow.torrent import MAX_FILES, MAX_TORRENT_BYTES, parse_torrent_metadata

assert Path(selection.__file__).resolve() == Path(sys.argv[1])
def encode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, list):
        return b'l' + b''.join(encode(item) for item in value) + b'e'
    return b'd' + b''.join(encode(key) + encode(value[key]) for key in sorted(value)) + b'e'

entries = []
for index in range(MAX_FILES):
    path = [('a' * 240 + f'{index:05d}.bin').encode()]
    if sys.argv[2] == 'True':
        path = [b'b' * 200, b'c' * 200, *path]
    entries.append({b'length': 1, b'path': path})
torrent = encode({b'info': {b'name': b'Fixture', b'piece length': 16384,
                           b'pieces': b'x' * 40, b'files': entries}})
assert len(torrent) < MAX_TORRENT_BYTES
metadata = parse_torrent_metadata(torrent)
assert len(metadata.files) == MAX_FILES
expression = ','.join(f'*absent-{index:03d}*' for index in range(500))
policy = selection.normalize_policy('files', expression)
try:
    selection.resolve_selection(metadata.files, policy)
except selection.SelectionError as error:
    assert error.code == 'selection.nothing_matched'
else:
    raise AssertionError('Absent patterns cannot select files')
assert policy['value'] == expression
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(Path(tow.selection.__file__).resolve()), str(nested)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "ok"
