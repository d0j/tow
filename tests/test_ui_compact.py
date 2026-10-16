"""1.32.1 shared look: fields that stand out in grey cards, one checkbox, one disclosure, intros as
wide as the fields, the Log in the page's font and a sign-in card that fits a phone (measured in
a browser at 360-1440 px, light and dark)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
CSS = (ROOT / "src" / "tow" / "static" / "app.css").read_text(encoding="utf-8")


def _rules(css: str = CSS) -> dict[str, str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    rules: dict[str, str] = {}
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector = " ".join(match.group(1).split())
        if not selector.startswith("@"):
            rules.setdefault(selector, match.group(2))
    return rules


def _block(opening: str) -> str:
    start = CSS.index(opening) + len(opening)
    depth = 1
    for end in range(start, len(CSS)):
        depth += {"{": 1, "}": -1}.get(CSS[end], 0)
        if depth == 0:
            return CSS[start:end]
    raise AssertionError(opening)


def test_a_field_in_a_grey_card_is_white():
    """V1: a field in a client or messenger card or a row's edit panel had the card's own grey
    (#eef1f6 on #eef1f6), only its border to show it. White cards keep their grey fields."""
    selector = (
        ':is(.integration-card, .edit-panel) :is(input:not([type="checkbox"]):not([type="radio"])'
        ':not([type="file"]), select, textarea)'
    )
    assert _rules()[selector].strip() == "background: var(--panel);"
    assert "auth-card" not in selector
    assert "credential-prompt" not in selector


def test_one_checkbox_whose_label_is_a_24_px_target():
    """CB1: a Settings box was 17.6 px, a Home one the browser's 13 px. One 16 px box in the accent
    colour; a check field's label keeps its look and is at least 24 px to hit."""
    box = _rules()['input[type="checkbox"]']
    for declaration in ("width: 1rem", "height: 1rem", "accent-color: var(--acc)"):
        assert declaration in box
    assert "width" not in _rules()[".check-field input"]
    label = _rules()[".check-field label"]
    padding = re.search(r"padding: \.(\d+)rem 0", label)
    margin = re.search(r"margin: -\.(\d+)rem 0", label)
    assert padding
    assert margin
    assert padding.group(1) == margin.group(1)
    assert 12.5 * 1.45 + 2 * 16 * float("." + padding.group(1)) >= 24
    assert "min-height: 1.5rem" in _rules()[".check-label"]


def test_one_disclosure_with_a_28_px_summary_and_one_chevron():
    """A1: summaries were 18-21 px, with three markers (the browser's triangles and two chevrons).
    One chevron rule for every disclosure, cards and Settings sections included."""
    simple = ":is(.technical-details, details.advanced, .backup-inventory, .backup-location) > summary"
    assert "min-height: 1.75rem" in _rules()[simple]
    assert "list-style: none" in _rules()[simple]
    assert "min-height: 1.75rem" in _rules()[":is(.notifier-card, .client-card, .protection-card) > summary"]
    chevrons = [selector for selector, body in _rules().items() if "rotate(-45deg)" in body]
    one = (
        f"{simple}::before, :is(.notifier-card, .client-card, .protection-card) > summary h2::before, "
        ".settings-accordion > summary::before"
    )
    assert chevrons == [one]


def test_an_intro_that_is_a_hint_runs_as_wide_as_the_fields():
    """B1: `.settings-panel :is(.action-hint, ...)` (68ch) outranked `.settings-panel > p` (36 rem):
    an intro of class action-hint stopped at 440 px in two lines."""
    rules = _rules()
    assert rules[".settings-panel > p:is(.action-hint, .support-note)"].strip() == "max-width: 36rem;"
    assert CSS.index(".settings-panel > p:is(.action-hint, .support-note)") > CSS.index(
        ".settings-panel :is(.field-hint, .action-hint, .section-note, .check-field p, .notifier-steps ol)"
    )


def test_the_log_reads_in_the_pages_font():
    """H6: the Log and History were monospace throughout; only raw text (code) stays monospace and
    the times keep even digits."""
    box = _rules()[".log-box"]
    assert "Segoe UI" in box
    assert "monospace" not in box
    assert "tabular-nums" in _rules()[".log-line time"]


def test_the_sign_in_card_fits_a_phone_and_its_field_is_a_standard_one():
    """AU1: at 360 px the card left 290 px for its content. A refusal about one field goes under
    it. 1.32.2: its password is the standard 240 px of every password (1.32.1 made it fill the
    card, the one password of another width)."""
    assert not [selector for selector in _rules() if ".auth-form .secret" in selector]
    small = _block("@media (max-width: 420px) {")
    small_rules = _rules(small)
    assert small_rules["main.auth-shell"].strip() == "padding: .5rem;"
    assert small_rules[".card.auth-card"].strip() == "padding: .9rem;"
    assert "color: var(--bad)" in _rules()[".field-error"]
