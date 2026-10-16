"""One spacing scale for the whole interface (AGENTS.md, "UI standard").

3 px attached (a label above its field, a hint under it, a heading's subtitle, list items), 6 px related
(text parts, a group's buttons, a disclosure's content under its summary, sections), 9 px groups (field
groups, the buttons under the last field, a box's block padding), 12 px blocks (a top-level box's inline
padding, fields in a row, columns, a new heading), 18 px the text after a chevron or a list marker.

A container spaces what it holds by one gap; what it holds carries no margins of its own, so nothing adds
up and a hidden part takes no room. The browser's own margins (paragraphs, lists, headings) are reset.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src" / "tow" / "static"
TEMPLATES = ROOT / "src" / "tow" / "templates"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")
SCALE = {"--sp-1": ".1875rem", "--sp-2": ".375rem", "--sp-3": ".5625rem", "--sp-4": ".75rem", "--sp-5": "1.125rem"}


def _rules(css: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return [(" ".join(m.group(1).split()), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def _rule(selector: str, css: str = CSS) -> str:
    bodies = [body for sel, body in _rules(css) if sel == selector]
    assert bodies, selector
    return " ".join(bodies)


def test_one_scale_of_five_steps():
    root = _rule(":root")
    for name, value in SCALE.items():
        assert f"{name}: {value};" in root, name
    assert len(re.findall(r"--sp-\d:", root)) == 5
    # 3 : 6 : 9 : 12 : 18 px at 16 px a rem.
    assert [16 * float(v.replace("rem", "")) for v in SCALE.values()] == [3, 6, 9, 12, 18]


def test_browser_margins_are_reset_and_hidden_parts_take_no_room():
    assert "margin: 0;" in _rule(":where(p, ol, ul, dl, dd, pre, figure, blockquote, h1, h2, h3, h4)")
    assert "display: none !important;" in _rule("[hidden]")
    assert "@media (scripting: enabled) { noscript { display: none; } }" in CSS
    # An empty live region (a hint, a status line) stays for a screen reader, out of the flow.
    empty = _rule(':is(.field-hint, .action-hint, .check-field p, .sub, [role="status"], [aria-live]):empty')
    assert "position: absolute" in empty
    assert "clip-path: inset(50%)" in empty


def test_flow_containers_space_their_parts_by_one_gap():
    flows = {
        ".stack-1": "--sp-1",
        ".stack-2": "--sp-2",
        ".stack-3": "--sp-3",
        ".stack-4": "--sp-4",
        ".details-body": "--sp-2",
        ".help-page": "--sp-4",
        ".doctor-page": "--sp-3",
        ".check-results": "--sp-2",
        ".edit-panel": "--sp-3",
        ".card.auth-card": "--sp-3",
        ".credential-prompt[open]": "--sp-3",
        ".backup-delete-confirm": "--sp-3",
        ".empty-state": "--sp-3",
        ".mirror-choice": "--sp-1",
    }
    for selector, token in flows.items():
        assert f"gap: var({token})" in _rule(selector), selector
    assert "gap: var(--sp-3)" in _rule(".release-panel", (STATIC / "updates.css").read_text(encoding="utf-8"))
    # A disclosure's content: 6 px under its summary.
    assert "margin-top: var(--sp-2)" in _rule(".details-body")


def test_no_paragraph_holds_a_button_row_and_every_disclosure_has_one_body():
    for path in sorted(TEMPLATES.glob("*.html")):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"<p>\s*<a class=\"btn", source), path.name  # a button row is .actions
        for match in re.finditer(
            r'<details class="technical-details[^"]*"[^>]*>\s*<summary>.*?</summary>', source, re.DOTALL
        ):
            after = source[match.end() :].lstrip()
            assert re.match(r'<[a-z]+ class="[^"]*details-body', after) or after.startswith("<code>"), (
                path.name,
                after[:80],
            )


def test_forms_space_their_groups_by_the_gap_alone():
    """A form's groups 9 px apart, a label 3 px above its field, the buttons 9 px under the last field on
    every screen (Settings had 10.4, the add forms 21.6, the sign-in dialog 27): no group adds a margin."""
    assert "gap: var(--sp-3)" in _rule(".form-grid")
    assert "gap: var(--sp-3)" in _rule(".settings-form")
    assert "padding" not in _rule(".settings-form")
    for selector in (".settings-actions", ".actions", ".edit-actions", ".check-field", ".form-error", ".field-error"):
        assert "margin" not in _rule(selector), selector
    for selector in (".settings-actions", ".actions", ".edit-actions", ".inline-save", ".client-add-row"):
        assert "gap: var(--sp-2)" in _rule(selector), selector
    label = _rule("label")
    assert "margin: 0" in label
    assert "line-height: 1.45" in label  # the hints' line height
    assert "margin" not in _rule(".unsaved-state")


def test_a_check_field_is_a_24px_target_without_padding():
    check = _rule(".check-field")
    assert "padding" not in check
    assert "column-gap: var(--sp-2)" in check
    assert "row-gap: var(--sp-1)" in check
    assert "min-block-size: 24px" in _rule("label.check-field")
    assert not [sel for sel, _ in _rules(CSS) if sel.startswith(".check-field + .check-field")]


def test_a_hint_about_a_row_of_fields_is_grouped_with_it():
    """The hint or "leave empty to keep" under a row of fields sits 3 px under it (it was a whole group gap
    away: 8.8 px in Settings and Setup, 14.4 px in the sign-in dialog)."""
    from bs4 import BeautifulSoup

    page = BeautifulSoup((ROOT / "tests" / "golden" / "settings-page.html").read_text(encoding="utf-8"), "html.parser")
    rows = page.select(".field-row")
    assert rows
    for row in rows:
        after = row.find_next_sibling()
        if after is not None and set(after.get("class") or []) & {"field-hint", "keep-note"}:
            assert "field" in (row.parent.get("class") or []), str(after)[:80]
    setup = (TEMPLATES / "setup.html").read_text(encoding="utf-8")
    assert re.search(r'<div class="field">\s*<div class="field-row">', setup)
    dialog = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    assert re.search(
        r'<div class="field">\s*<div class="field-row">\s*<div class="field"><label for="tracker-username', dialog
    )
