from __future__ import annotations

import fnmatch
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
import selection_work_probe

import tow.selection
from tow.selection import SelectionError, SelectionPendingError, normalize_policy, resolve_selection
from tow.torrent import TorrentFile


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
    # The unguarded rule accepts both files; no absent literal group is compiled.
    assert len(compiled) == 2


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
    assert max(tow.selection._mask_parts(pattern)[0], key=len, default="") == literal


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
                parts, _ = tow.selection._mask_parts(pattern)
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
        parts, _ = tow.selection._mask_parts(pattern)
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

        return SimpleNamespace(match=match, search=compiled.search, findall=compiled.findall)

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


@pytest.mark.parametrize(("last_pattern", "expected_groups"), [("*[[]*", 1), ("[!o]*", 2)])
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
    assert len(compiled) == 2


@pytest.mark.parametrize(
    ("patterns", "path"),
    [
        (("abc?", "*ab*"), "abc"),
        (("*ab?", "*bc"), "abc"),
        (("*日本語?", "*日本*"), "日本語"),
        (("*a*wrong*", "*[ab]*"), "a"),
        (("*[!a]*", "*a[xy]*"), "bbb"),
        (("*[z-a]*", "*[!z-a]*"), "a"),
        (("*abc[xy]*", "*bc[de]*"), "abcde"),
    ],
)
def test_group_filter_keeps_shorter_prefixes_overlaps_and_unguarded_matches(patterns, path):
    assert tow.selection._file_mask_matcher(patterns)(path) == any(fnmatch.fnmatchcase(path, rule) for rule in patterns)


def test_candidate_groups_compile_only_on_hits_and_once_per_call(monkeypatch):
    original = tow.selection.re.compile
    compiled = []

    def record(pattern, *args, **kwargs):
        compiled.append(pattern)
        return original(pattern, *args, **kwargs)

    monkeypatch.setattr(tow.selection.re, "compile", record)
    patterns = [f"*item-{index:03d}*" for index in range(500)]
    matches = tow.selection._file_mask_matcher(patterns)
    assert len(compiled) == 1
    assert not matches("ordinary.bin")
    assert len(compiled) == 1
    for index in range(500):
        path = f"item-{index:03d}.bin"
        assert matches(path)
        assert matches(path)
        assert len(compiled) == index + 2
    # Per-call state cannot hide a hit on the next path or share compiled groups
    # with an unrelated request. Bounds apply to preparation, not accepted inputs.
    other = tow.selection._file_mask_matcher(("*item-000?",))
    assert not other("item-000")
    assert other("item-000x")


def test_common_class_filter_is_bounded_and_never_accepts_by_itself(monkeypatch):
    original = tow.selection.re.compile
    compiled = []

    def record(pattern, *args, **kwargs):
        compiled.append(pattern)
        return original(pattern, *args, **kwargs)

    monkeypatch.setattr(tow.selection.re, "compile", record)
    pattern = "*" + "*".join(f"[a{chr(0x1000 + index)}]" for index in range(20)) + "*[xy]"
    matches = tow.selection._file_mask_matcher((pattern,))
    assert len(compiled) == 9  # eight necessary classes plus the exact union
    assert not matches("aaaaaaaaaaaaaaaaaaaa")
    assert matches("a" * 20 + "x")


def test_shared_class_permutations_cannot_exceed_the_eight_filter_bound(monkeypatch):
    original = tow.selection.re.compile
    compiled = []

    def record(pattern, *args, **kwargs):
        compiled.append(pattern)
        return original(pattern, *args, **kwargs)

    monkeypatch.setattr(tow.selection.re, "compile", record)
    classes = tuple(f"[a{chr(0x1000 + index)}]" for index in range(20))
    patterns = ("*".join(classes), "*".join(reversed(classes)))
    matches = tow.selection._file_mask_matcher(patterns)
    assert len(compiled) == 9
    assert matches("a" * 20)
    assert not matches("a" * 19)
    assert len(compiled) == 9


@pytest.mark.parametrize("scenario", ["long-literal", "terminal-chain", "branch-chain"])
def test_maximum_text_prefix_filters_preserve_exact_globs_without_recursion(scenario):
    patterns = (
        ("a" * 8192,)
        if scenario == "long-literal"
        else tuple("a" * size + ("b" if scenario == "branch-chain" else "") for size in range(1, 125))
    )
    policy = normalize_policy("files", ",".join(patterns))
    assert len(policy["value"]) <= 8192
    matches = tow.selection._file_mask_matcher(patterns)
    for path in ("a" * 8192, "a" * 125, "a" * 124 + "b", "a" * 123 + "b", "ordinary"):
        assert matches(path) == any(fnmatch.fnmatchcase(path, rule) for rule in patterns)


def test_literal_prefix_union_retains_longest_and_overlapping_metacharacter_hits():
    literals = ("ab", "abc", "bc", "b[", "[", "|", ".", "日本", "日本語", "\n")
    compiled = tow.selection.re.compile("(?=(" + tow.selection._literal_union(literals) + "))")
    for path in ("abc", "b[|.", "日本語\n日本", "ordinary"):
        expected = [
            (position, max((part for part in literals if path.startswith(part, position)), key=len))
            for position in range(len(path))
            if any(path.startswith(part, position) for part in literals)
        ]
        assert [(match.start(), match.group(1)) for match in compiled.finditer(path)] == expected


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


# The large scenarios run in one isolated interpreter (tests/selection_work_probe.py): read-only,
# no network, files, state or client. Each is bounded by the work it took, counted as the calls
# tow.selection made - the same on a busy or an idle machine. Measured: at most 1.1 million for
# episode rules, 1.5 million for file masks; a matcher that tried each of 500 masks on each of
# 20,000 files makes ten million or more. The deadline only stops a probe that hangs.
WORK_BUDGET = {"episodes": 2_500_000, "files": 4_000_000, "paths": 4_000_000}
PROBE_DEADLINE_SEC = 600
# All 45 scenarios take one to two minutes. The gate runs the costliest of each kind; the weekly
# installers workflow runs every one (TOW_SELECTION_WORK=all).
REPRESENTATIVE = ("episodes-metadata-limit", "files-distinct", "paths-nested-multi-class")
SCENARIOS = tuple(selection_work_probe.SCENARIOS) if os.environ.get("TOW_SELECTION_WORK") == "all" else REPRESENTATIVE


def test_the_representative_scenarios_are_scenarios_of_every_kind():
    assert set(REPRESENTATIVE) <= set(selection_work_probe.SCENARIOS)
    assert {name.split("-", 1)[0] for name in REPRESENTATIVE} == set(WORK_BUDGET)


@pytest.fixture(scope="module")
def probe_results() -> dict[str, dict]:
    probe = Path(__file__).with_name("selection_work_probe.py")
    selection_file = str(Path(tow.selection.__file__).resolve())
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(probe), selection_file, *SCENARIOS],
            capture_output=True,
            text=True,
            timeout=PROBE_DEADLINE_SEC,
            check=False,
        )
        output, failure = result.stdout, result.stderr.strip() if result.returncode else ""
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        failure = f"the probe did not finish within {PROBE_DEADLINE_SEC} s"
    results = {row["name"]: row for row in map(json.loads, output.splitlines())}
    for name in SCENARIOS:
        results.setdefault(name, {"ok": False, "error": failure or "not run", "calls": None})
    return results


# One probe for the module: with pytest-xdist all of its tests go to one worker (--dist loadgroup).
@pytest.mark.xdist_group("selection-work")
@pytest.mark.allow_system
@pytest.mark.parametrize("name", SCENARIOS)
def test_large_rules_and_torrents_finish_within_a_counted_work_bound(probe_results, name):
    result = probe_results[name]
    assert result["ok"], result["error"]
    assert result["calls"] <= WORK_BUDGET[name.split("-", 1)[0]], result["calls"]
