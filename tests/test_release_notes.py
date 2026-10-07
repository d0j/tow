"""scripts/release-notes.py: what to download in both languages, then the tag's changelog sections."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release-notes.py"


def _module():
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_section_is_the_body_of_its_version_only(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [1.2.0] — x\n\n- new\n\n## [1.1.0] — y\n\n- old\n", encoding="utf-8")
    assert _module().section(changelog, "1.2.0") == "- new"
    assert _module().section(changelog, "9.9.9") == ""


def test_a_wrapped_item_becomes_one_line_so_github_does_not_break_it():
    # GitHub turns every line break of a release body into <br>: an item the changelog wraps at
    # 120 columns broke mid-sentence on the release page.
    text = (
        "### Changed\n\n"
        "- The first item, wrapped\n  over two lines\n  and a third.\n"
        "- A second one\n  - with a nested item\n    that wraps too\n\n"
        "A paragraph.\n"
    )
    assert _module().unwrap(text) == (
        "### Changed\n\n"
        "- The first item, wrapped over two lines and a third.\n"
        "- A second one\n  - with a nested item that wraps too\n\n"
        "A paragraph."
    )


def test_the_notes_carry_the_changelog_items_unwrapped():
    text = _module().notes("v1.25.0", "d0j/tow")
    assert (
        "- **Adopt into TOW**: a topic whose torrent is already in the client without TOW's mark can be taken" in text
    )
    assert not any(line.startswith("  ") and not line.lstrip().startswith("- ") for line in text.splitlines())


def test_notes_say_what_to_download_in_both_languages():
    text = _module().notes("v1.22.0", "d0j/tow")
    assert text.startswith("## Download")
    for name in (
        "TOW-windows-x64.zip",
        "Start TOW.cmd",
        "install.ps1",
        "install.sh",
        "tow-source.tar.gz",
        "SHA256SUMS",
    ):
        assert name in text
    assert "Что скачать" in text
    assert "По-русски" in text
    assert "https://github.com/d0j/tow/releases/latest/download/install.sh" in text
