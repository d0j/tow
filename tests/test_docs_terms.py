"""The README and the guides use the words of CONTRIBUTING.md's term table, as the interface does."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGLISH = [
    ROOT / "README.md",
    ROOT / "docs" / "guide.md",
    ROOT / "docs" / "install.md",
    ROOT / "docs" / "PROTECTION.md",
    ROOT / "config.example.yaml",
]
RUSSIAN = [
    ROOT / "README.ru.md",
    ROOT / "docs" / "ru" / "guide.md",
    ROOT / "docs" / "ru" / "install.md",
    ROOT / "docs" / "ru" / "protection.md",
]

# Word -> the term the table gives for it.
_ENGLISH_WRONG = {
    r"`\.towx` files?": "TOW file (`.towx`)",
    r"\blog(?:ged)? in\b": "sign in",
    r"\bnight(?:ly)?[ -]cop(?:y|ies)\b": "nightly backup",
    r"\brevisions?\b": "version",
}
_RUSSIAN_WRONG = {
    r"файл(?:ы|ов|ам)? `\.towx`": "файл TOW (`.towx`)",
    r"автозагрузк": "автозапуск",
    r"авторизац": "вход",
    r"секрет": "пароли и токены",
    r"\bхэш": "хеш",
    r"\bкэш": "кеш",
    r"процесс обновления": "программа обновления",
    r"\bмагнет": "magnet-ссылка",
    r"\bсесси": "вход",
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


def _texts(node: object, key: str = "") -> list[tuple[str, str]]:
    """Every text of a catalog with its key; "q" keys are search words, which list the other
    words people may type on purpose."""
    if isinstance(node, dict):
        return [item for name, value in node.items() for item in _texts(value, f"{key}.{name}".lstrip("."))]
    return [] if key.rsplit(".", 1)[-1] == "q" or not isinstance(node, str) else [(key, node)]


@pytest.mark.parametrize(("language", "words"), [("en", _ENGLISH_WRONG), ("ru", _RUSSIAN_WRONG)])
def test_the_catalogs_use_the_terms(language, words):
    # Before: "The night copies folder is on another disk..." where Settings says Nightly backups.
    import json

    catalog = json.loads((ROOT / "src" / "tow" / "locales" / f"{language}.json").read_text(encoding="utf-8"))
    found = [
        f"{key}: {match.group(0)!r} -> {term}"
        for key, text in _texts(catalog)
        for pattern, term in words.items()
        for match in re.finditer(pattern, text, re.IGNORECASE)
    ]
    assert found == []


def test_a_wrong_word_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr("test_docs_terms.ROOT", tmp_path)
    doc = tmp_path / "x.md"
    doc.write_text("Save a `.towx` file.\nВключите автозагрузку.\n", encoding="utf-8")
    assert len(_found(doc, _ENGLISH_WRONG)) == 1
    assert _found(doc, _RUSSIAN_WRONG) == ["x.md:2: 'автозагрузк' -> автозапуск"]
