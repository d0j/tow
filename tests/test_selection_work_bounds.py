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

    expression = "missing*,*.MKV,*[[]*,*.mkv,[!a]*"
    files = (TorrentFile(0, "Season 1/Show.mkv", 1), TorrentFile(1, "Extras/[Interview].mp4", 1))
    monkeypatch.setattr(tow.selection.re, "compile", record)
    plan = resolve_selection(files, normalize_policy("files", expression))
    assert plan.selected_indices == (0, 1)
    assert plan.expression == expression
    # One literal filter, one exact guarded union, one union without fixed text;
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
        ("alpha[bc]omega", "*[a]middle[b]*", "*[!]tail", "*[]]end*"),
        ("*[[]literal*", "*head[", "*[z-a]tail*", "*[!z-a]tail*"),
        ("*a[?*]tail*", "*a[!?*]tail*", "*a[[]tail*", "*[!]]tail*"),
        ("*Straße[ab]日本語*", "*日本語[ß]tail*", "*[]literal*", "*[][]tail*"),
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


@pytest.mark.parametrize(
    ("pattern", "literal"),
    [
        ("*z000*[.]bin", "z000"),
        ("*[a]z000*", "az000"),
        ("*[a]middle[b]*", "amiddleb"),
        ("*head[", "head["),
        ("*[]literal*", "[]literal"),
        ("*[!]tail*", "[!]tail"),
        ("*[]]tail*", "]tail"),
        ("*[!]]tail*", "tail"),
        ("*[[]tail*", "[tail"),
        ("*[?*]tail*", "tail"),
        ("*[!?*]tail*", "tail"),
        ("*[z-a]tail*", "tail"),
        ("*[!z-a]tail*", "tail"),
        ("*[a&&b]tail*", "tail"),
        ("*[a--b]tail*", "tail"),
        ("*[a]*", "a"),
        ("*[?]tail*", "?tail"),
        ("*[*]tail*", "*tail"),
        ("*[-]tail*", "-tail"),
        ("*[^]tail*", "^tail"),
        ("*[\\]tail*", "\\tail"),
        ("*a[bc]tail*", "tail"),
        ("*?*", ""),
    ],
)
def test_mandatory_literals_preserve_singletons_ranges_and_malformed_classes(pattern, literal):
    assert max(tow.selection._mandatory_parts(pattern), key=len, default="") == literal


def test_bracket_literal_guards_preserve_generated_positive_and_negative_matches():
    runs = ("", "a", "literal", "tail", "Straße", "日本語", "\n", "]", "[]", "!")
    boundaries = (
        "*",
        "?",
        "[ab]",
        "[!a]",
        "[]]",
        "[!]]",
        "[[]",
        "[z-a]",
        "[!z-a]",
        "[?*]",
        "[!?*]",
        "[a&&b]",
        "[a--b]",
        "[",
        "[]",
        "[!]",
        "[[]]",
    )
    values = ("", "a", "b", "]", "[", "?", "*", "!", "x", "日本語", "ß", "\n", "[!]", "[]")
    comparisons = positive = 0
    for prefix in runs:
        for suffix in runs:
            for boundary in boundaries:
                pattern = (prefix + boundary + suffix).casefold()
                parts = tow.selection._mandatory_parts(pattern)
                matches = tow.selection._file_mask_matcher((pattern,))
                for candidate in (pattern, *(prefix + value + suffix for value in values)):
                    path = candidate.casefold()
                    expected = fnmatch.fnmatchcase(path, pattern)
                    assert matches(path) == expected, (path, pattern, parts)
                    if expected:
                        assert all(part in path for part in parts), (path, pattern, parts)
                        positive += 1
                    comparisons += 1
    assert comparisons == 25500
    assert positive > 5000


def test_single_character_classes_preserve_standard_matches_including_metacharacters():
    characters = (*(chr(code) for code in range(128)), "日本語", "ß", "İ", "é", "λ")
    for character in characters:
        pattern = ("prefix[" + character + "]suffix").casefold()
        parts = tow.selection._mandatory_parts(pattern)
        matches = tow.selection._file_mask_matcher((pattern,))
        for candidate in ("prefix" + value + "suffix" for value in (*characters, "[!]", "[]", "[", "")):
            candidate = candidate.casefold()
            expected = fnmatch.fnmatchcase(candidate, pattern)
            assert matches(candidate) == expected, (candidate, pattern)
            if expected:
                assert all(part in candidate for part in parts), (candidate, pattern, parts)


@pytest.mark.parametrize("sparse", [False, True])
def test_shared_title_and_sparse_literal_hits_do_not_force_exact_union(monkeypatch, sparse):
    from types import SimpleNamespace

    original = tow.selection.re.compile
    exact_calls = []

    def record(pattern, *args, **kwargs):
        compiled = original(pattern, *args, **kwargs)

        def match(path):
            exact_calls.append(path)
            return compiled.match(path)

        return SimpleNamespace(match=match, search=compiled.search)

    monkeypatch.setattr(tow.selection.re, "compile", record)
    template = "*aaaaaa*z{index:03d}*x*" if sparse else "*aaaaaaaa*z{index:03d}*"
    matches = tow.selection._file_mask_matcher(template.format(index=index) for index in range(500))
    path = "a" * 240 + ("z000" if sparse else "") + "00000.bin"
    assert not matches(path)
    assert exact_calls == []


def test_common_literal_checks_are_bounded_and_never_decide_success():
    checks = []

    class ObservedPath(str):
        def __contains__(self, part):
            checks.append(part)
            return super().__contains__(part)

    parts = [f"part{index:02d}" for index in range(20)]
    matches = tow.selection._file_mask_matcher(("*" + "*".join(parts) + "*Z*",))
    assert not matches(ObservedPath("-".join(parts)))
    assert len(checks) == 8
    assert matches(ObservedPath("-".join(parts) + "-Z"))


@pytest.mark.parametrize(("last_pattern", "expected_groups"), [("*[[]*", 2), ("[!o]*", 3)])
def test_file_mask_groups_are_bounded_at_the_maximum_rule_count(monkeypatch, last_pattern, expected_groups):
    original = tow.selection.re.compile
    compiled = []

    def record(pattern, *args, **kwargs):
        compiled.append(pattern)
        return original(pattern, *args, **kwargs)

    monkeypatch.setattr(tow.selection.re, "compile", record)
    patterns = [f"*absent-{index:03d}*" for index in range(499)] + [last_pattern]
    matches = tow.selection._file_mask_matcher(patterns)
    assert len(compiled) == expected_groups
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
@pytest.mark.parametrize("scenario", ["long", "nested", "deep"])
@pytest.mark.parametrize(
    "kind", ["literal", "prefix", "suffix", "middle", "malformed", "present", "sparse", "sparse-middle"]
)
def test_parsed_large_torrents_with_long_paths_finish_file_selection(scenario, kind):
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
scenario, kind = sys.argv[2:4]
file_count = 7000 if scenario == 'deep' else MAX_FILES
for index in range(file_count):
    base = 'a' * 240 + ('z000' if kind.startswith('sparse') else '') + f'{index:05d}.bin'
    path = [base.encode()]
    if scenario == 'nested':
        path = [b'b' * 200, b'c' * 200, *path]
    elif scenario == 'deep':
        path = [b'b' * 250 for _ in range(15)] + path
    entries.append({b'length': 1, b'path': path})
torrent = encode({b'info': {b'name': b'Fixture', b'piece length': 16384,
                           b'pieces': b'x' * (20 * ((file_count + 16383) // 16384)), b'files': entries}})
assert len(torrent) < MAX_TORRENT_BYTES
metadata = parse_torrent_metadata(torrent)
assert len(metadata.files) == file_count
templates = {'literal': '*absent-{index:03d}*', 'prefix': '*z{index:03d}*[.]bin',
             'suffix': '*[a]z{index:03d}*', 'middle': '*[a]z{index:03d}[b]*',
             'malformed': '*z{index:03d}[', 'present': '*aaaaaaaa*z{index:03d}*',
             'sparse': '*aaaaaa*z{index:03d}*x*', 'sparse-middle': '*[a]z{index:03d}[b]*'}
expression = ','.join(templates[kind].format(index=index) for index in range(500))
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
        [sys.executable, "-I", "-B", "-c", script, str(Path(tow.selection.__file__).resolve()), scenario, kind],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "ok"
