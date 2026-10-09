"""The README and the guides use the words of CONTRIBUTING.md's term table, as the interface does."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGLISH = [ROOT / "README.md", ROOT / "docs" / "guide.md", ROOT / "docs" / "install.md"]
RUSSIAN = [ROOT / "README.ru.md", ROOT / "docs" / "ru" / "guide.md", ROOT / "docs" / "ru" / "install.md"]

# Word -> the term the table gives for it.
_ENGLISH_WRONG = {
    r"`\.towx` files?": "TOW file (`.towx`)",
    r"\blog(?:ged)? in\b": "sign in",
}
_RUSSIAN_WRONG = {
    r"файл(?:ы|ов|ам)? `\.towx`": "файл TOW (`.towx`)",
    r"автозагрузк": "автозапуск",
    r"авторизац": "вход",
    r"секрет": "пароли и токены",
    r"\bхэш": "хеш",
    r"\bкэш": "кеш",
    r"процесс обновления": "программа обновления",
}


def _found(path: Path, words: dict[str, str]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [
        f"{path.relative_to(ROOT)}:{text[: match.start()].count(chr(10)) + 1}: {match.group(0)!r} -> {term}"
        for pattern, term in words.items()
        for match in re.finditer(pattern, text, re.IGNORECASE)
    ]


@pytest.mark.parametrize("path", ENGLISH, ids=lambda path: path.name)
def test_english_docs_use_the_terms(path):
    assert _found(path, _ENGLISH_WRONG) == []


@pytest.mark.parametrize("path", RUSSIAN, ids=lambda path: f"ru-{path.name}")
def test_russian_docs_use_the_terms(path):
    assert _found(path, _RUSSIAN_WRONG) == []


def test_a_wrong_word_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr("test_docs_terms.ROOT", tmp_path)
    doc = tmp_path / "x.md"
    doc.write_text("Save a `.towx` file.\nВключите автозагрузку.\n", encoding="utf-8")
    assert len(_found(doc, _ENGLISH_WRONG)) == 1
    assert _found(doc, _RUSSIAN_WRONG) == ["x.md:2: 'автозагрузк' -> автозапуск"]
