from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import tow.episodes
from tow.episodes import parse_expected_count, parse_season_hint


@pytest.mark.allow_system
def test_untrusted_title_whitespace_finishes_in_a_bounded_isolated_process():
    # A subprocess deadline stops a regressed backtracking pattern safely. It
    # imports the locked interpreter's source module, writes nothing and has no
    # client, network or runtime state operations.
    module_path = str(Path(tow.episodes.__file__).resolve())
    script = """
import importlib.util
import sys
spec = importlib.util.spec_from_file_location('tow.episodes_title_probe', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
gap = ' ' * 40000
assert module.parse_expected_count('Show 3: 1' + gap + '!') is None
assert module.parse_expected_count('Show 1 of 14' + gap + '!')['total'] == 14
assert module.parse_season_hint('Show Season' + gap + '3 [1 of 14]') == 3
assert module.parse_expected_count('Show ' + '[1 of 14] ' * 12000)['total'] == 14
print('ok')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, module_path],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize("gap", [" ", "\t", "\n", "\u00a0", "\u2003"])
@pytest.mark.parametrize("episode_word", ["", "серии", "episodes", "eps"])
def test_title_whitespace_preserves_count_and_corroborated_season(gap, episode_word):
    text = f"Show 3: Chapter [1{gap}{episode_word}{gap}of{gap}14]"
    assert parse_expected_count(text)["total"] == 14
    assert parse_season_hint(text) == 3


@pytest.mark.parametrize("tail", ["seasons", "- x seasons", "x parts", "— х тома"])
def test_title_whitespace_still_excludes_non_episode_totals(tail):
    assert parse_expected_count("Show 1 of 14\t\n" + tail) is None
