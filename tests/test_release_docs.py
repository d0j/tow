"""CONTRIBUTING.md's release section: short, before the licence line; the details live in --help."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_release_section_comes_before_the_licence_line():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert text.index("## Publishing a release") < text.index("By contributing you agree")
    assert text.rstrip().endswith("[MIT License](LICENSE).")
    section = text.split("## Publishing a release", 1)[1]
    for phrase in ("release: vX.Y.Z - <summary>", "TOW X.Y.Z", "publish-release.py vX.Y.Z", "history-scan.py"):
        assert phrase in section
    assert "`--help`" in section
    assert len(section.splitlines()) < 40


def test_the_history_scan_help_carries_what_it_covers(capsys):
    spec = importlib.util.spec_from_file_location("history_scan_help", ROOT / "scripts" / "history-scan.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit) as done:
        module.main(["--help"])
    assert done.value.code == 0
    text = capsys.readouterr().out
    for phrase in (
        "separate complete clone",
        "Exit 0: complete, no candidates; 1: complete, review candidates; 2: incomplete/refused.",
        "not proof",
        "--max-blob-bytes",
        "5,000,000",
        "two seconds",
        "120 seconds",
        "Git 2.36",
        "reflog-only",
    ):
        assert phrase in text
