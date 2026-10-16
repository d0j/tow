"""The owner's UI standard: every action button is 26 px, everywhere (AGENTS.md, "UI standard").

Buttons get their size from one shared rule (`button, .btn`); no page, card or form may size
a button on its own. Only square icon buttons (.ico, .row-ico, .flash-x) are exempt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CSS = (ROOT / "src" / "tow" / "static" / "app.css").read_text(encoding="utf-8")
# Phones and touch screens: fields grow to a 44 px touch target with 16 px text.
TOUCH = "@media (max-width: 720px), (pointer: coarse) {"
SIZE = re.compile(r"(?<![\w-])(height|min-height|max-height|font-size|font|line-height|padding|padding-block)\s*:")
BUTTON = re.compile(r"(^|[\s,>+~(])(button|\.btn)\b")
ICON = re.compile(r"\.(ico|row-ico|flash-x)\b")
SHARED = {"button, .btn", "button.sm, .btn.sm"}


def _rules(css: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    rules = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector = " ".join(match.group(1).split())
        if selector.startswith("@"):
            continue
        rules.append((selector, match.group(2)))
    return rules


def test_button_height_is_26px_and_text_12_5px():
    assert "--btn-h: 1.625rem;" in CSS  # 26 px
    assert "--btn-h-sm: 1.625rem;" in CSS
    shared = dict(_rules(CSS))["button, .btn"]
    assert "min-height: var(--btn-h)" in shared
    assert "font-size: 12.5px" in shared
    assert shared.index("font: inherit") < shared.index("font-size: 12.5px")  # not overridden


def _first(selector: str, css: str = CSS) -> str:
    """The first rule for ``selector`` (the base rule comes before any media override)."""
    return next(body for name, body in _rules(css) if name == selector)


def _block(opening: str) -> str:
    """The text of the @media block that starts with ``opening`` (up to its closing brace)."""
    start = CSS.index(opening) + len(opening)
    depth = 1
    for end in range(start, len(CSS)):
        depth += {"{": 1, "}": -1}.get(CSS[end], 0)
        if depth == 0:
            return CSS[start:end]
    raise AssertionError(f"unclosed {opening}")


def test_fields_are_36px_on_a_desktop_and_44px_on_phones_and_touch_screens():
    """The owner asked for smaller fields: 40 px fields with 15 px text outweighed their 12 px
    labels and the 26 px buttons beside them. A field is 36 px with 14 px text on a desktop; on a
    phone or a touch screen it stays a 44 px target with 16 px text (no zoom on focus in iOS)."""
    assert "--field-h: 2.25rem;" in _first(":root")  # 36 px
    touch = _block(TOUCH)
    assert "--field-h: 2.75rem;" in _first(":root", touch)  # 44 px
    assert "font-size: 14px" in _first("input, textarea, select")
    assert "font-size: 16px" in _first("input, textarea, select", touch)
    assert "height: var(--field-h)" in _first(
        'input:not([type="checkbox"]):not([type="radio"]):not([type="file"]), select'
    )
    # The folder list's arrow sits in the middle of whatever height the field has.
    assert "top: calc((var(--field-h) - var(--btn-h)) / 2)" in _first(".folder-toggle")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Inputs and selects are 36 px with 14 px text, 44 px with 16 px text on phones and touch" in agents
    assert "stay 40 px" not in agents


@pytest.mark.parametrize(
    ("name", "width"), [("w-xs", "6.5rem"), ("w-s", "12rem"), ("w-m", "18rem"), ("w-l", "26rem"), ("w-xl", "36rem")]
)
def test_a_field_is_as_wide_as_what_it_holds_at_most(name, width):
    """A port, a password and a webhook were all 1354 px wide at 1440 px. A width class is a
    maximum: the field still shrinks with its form; a secret field's wrapper takes its field's class."""
    assert _first(f".{name}").strip() == f"max-width: {width};"
    assert f"max-width: {width}" in _first(f".secret:has(> .{name})")


def test_one_form_layout_with_rows_that_wrap_by_the_forms_own_width():
    """One form system: at most 46 rem wide, a label 3 px above its field, 8-10 px between groups,
    help text at most 68 characters a line. A row of fields stands on one line once its form (not
    the window) is 38 rem wide: two cards side by side stack their fields on their own."""
    grid = _first(".form-grid")
    for declaration in ("display: grid", "max-width: 46rem", "container-type: inline-size"):
        assert declaration in grid
    gap = re.search(r"(?<![\w-])gap: \.(\d+)rem", grid)
    assert gap
    assert 8 <= 16 * float("." + gap.group(1)) <= 10
    assert "gap: .1875rem" in _first(".form-grid .field")  # 3 px
    # The older "next field" margins (.field + .field, the compact Settings one) pushed the second
    # field of a row 11 px down (seen in a browser); in the form the gap alone spaces groups.
    assert _first(".form-grid :is(.field, .field-grid) + .field.field").strip() == "margin-top: 0;"
    assert "max-width: 68ch" in _first(".form-grid :is(.field-hint, .action-hint, .section-note, .check-field p)")
    # A panel's intro and notes end where its widest field ends (1.32 QA L1: 440 px beside 576 px).
    assert _first(".settings-panel > p, .support-note").strip() == "max-width: 36rem;"
    assert "support-note" not in "".join(selector for selector, body in _rules(CSS) if "68ch" in body)
    assert "flex-direction: column" in _first(".field-row")
    wide = _block("@container (min-width: 38rem) {")
    assert "flex-direction: row" in _first(".field-row", wide)
    assert "flex: 0 1 6.5rem" in _first(".field-row > :is(.w-xs, :has(.w-xs))", wide)
    # An address and its port stay paired on a phone while the address keeps 12 rem.
    keep = _first(".field-row.keep")
    assert "flex-direction: row" in keep
    assert "flex-wrap: wrap" in keep
    assert "flex: 1 1 12rem" in _first(".field-row.keep > *")


def test_placeholders_are_as_readable_as_hints():
    """A placeholder at the browser's grey was 3.6:1 in the dark theme; it is the hints' muted colour
    (6.5-7:1)."""
    assert "color: var(--mut)" in _first("::placeholder")


def test_no_rule_sizes_a_button_on_its_own():
    offenders = []
    for selector, body in _rules(CSS):
        if selector in SHARED or not SIZE.search(body):
            continue
        parts = [part.strip() for part in selector.split(",")]
        sized_buttons = [part for part in parts if BUTTON.search(part) and not ICON.search(part)]
        if sized_buttons:
            offenders.append(selector)
    assert offenders == [], f"button sizes must come from the shared `button, .btn` rule: {offenders}"


def test_templates_do_not_style_buttons_inline():
    templates = Path(__file__).parents[1] / "src" / "tow" / "templates"
    for path in templates.glob("*.html"):
        for tag in re.findall(r"<button[^>]*>", path.read_text(encoding="utf-8")):
            assert "style=" not in tag, (path.name, tag)


def test_site_access_fields_use_full_width_and_do_not_inherit_sibling_offsets():
    rules = dict(_rules(CSS))
    assert rules[".settings-form.compact-form.protection-form"].strip() == "max-width: none;"
    assert rules[".settings-form.protection-form .field-grid > .field"].strip() == "margin-top: 0;"


def test_help_anchor_desktop_clearance_does_not_add_settings_card_layout():
    body = dict(_rules(CSS)).get(".help-page h2[id]", "")
    assert re.sub(r"\s+", "", body) == "scroll-margin-top:var(--land-top);"


def test_help_anchor_mobile_clearance_matches_the_two_row_header():
    """1.32: one landing offset - the measured header and message strip (messages.js), else the
    header's usual height, two rows on a phone (tests/js/message_landing.mjs)."""
    mobile = CSS.partition("@media (max-width: 720px) {")[2]
    assert re.search(r"--land-top: calc\(var\(--msg-top, 4\.75rem\) \+ var\(--msg-h, 0px\) \+ \.75rem\)", mobile)


def test_long_site_names_and_sign_in_pills_stay_in_their_columns():
    """Qa8: a long site title wrapped over four lines in Home's narrow Site column (the row four
    times as tall), and "No sign-in needed" ran out of its 5.5 rem pill on Sites. The title ends
    in "…" at desktop width (its tooltip says it all) and wraps on a phone; the sign-in column is
    as wide as its longest pill, which keeps its own width."""

    def first(selector: str) -> str:  # the base rule comes before any media override
        return next(body for name, body in _rules(CSS) if name == selector)

    tracker = first(".topic-tracker")
    for rule in ("overflow: hidden", "text-overflow: ellipsis", "white-space: nowrap", "min-width: 0"):
        assert rule in tracker
    phones = [block for block in CSS.split("@media (max-width: 720px) {")[1:] if ".topic-tracker" in block]
    assert phones
    assert "white-space: normal" in dict(_rules(phones[0]))[".topic-tracker"]
    home = (Path(__file__).parents[1] / "src" / "tow" / "templates" / "_topic_row.html").read_text(encoding="utf-8")
    assert 'class="topic-tracker" data-label="{{ t(\'home.row.site\') }}" title="{{ site_title(topic.tracker)' in home
    sites = first(".list-head.sites, details.row-edit.sites > summary")
    assert "minmax(10rem,1fr) 8rem 6.4rem" in sites
    assert "justify-self: start" in first("details.row-edit.sites > summary > .pill")


def test_the_copy_command_button_stays_one_line_in_the_master_key_note():
    """Qa10: "Скопировать команду" wrapped into two lines (32 px) inside the note, at 1280 px too:
    the note's text column shrank it. It keeps its width and one line, and goes under the text
    on a phone (measured in a browser at 1280 and 390 px: 26 px)."""
    rules = dict(_rules(CSS))
    assert "flex: 0 0 auto" in rules[".secret-store-copy"]
    assert "white-space: nowrap" in rules[".secret-store-copy > button"]
    # A red note since the notes became one component (it was a blue .flash).
    assert "flex-wrap: wrap" in rules[".notice.secret-store"]
    assert "flex: 1 1 min(24rem, calc(100% - 1.5rem))" in rules[".notice.secret-store > .notice-text"]


def test_the_home_progress_link_is_a_24_px_target_that_looks_the_same():
    """Qa10: "0/1" was a 23.9 x 22.5 px link (axe target-size; 20.7 x 19.5 on a phone). It is at
    least 24 px each way, and its padding is taken back by the margin: in a browser the rows are
    pixel for pixel as before at 1280 and 390 px."""
    body = dict(_rules(CSS))[".row-progress > a"]
    assert "min-width: 1.5rem" in body
    padding = re.search(r"padding-block: \.(\d+)rem", body)
    margin = re.search(r"margin-block: -\.(\d+)rem", body)
    assert padding
    assert margin
    assert padding.group(1) == margin.group(1)
    assert 13 * 1.5 + 2 * 16 * float("." + padding.group(1)) >= 24  # the phone's 13 px line


def test_a_long_settings_pill_wraps_on_a_phone_instead_of_losing_its_end():
    """Qa10: at 390 px the Backups pill ("Ночная копия 13.10.2026 09:28:19 UTC+03:00") ended in
    "…" with no tooltip, and a phone has none to show anyway. It wraps; nothing is cut off."""
    phone = CSS.split("@media (max-width: 720px) {")
    bodies = [dict(_rules(block)).get(".settings-accordion > summary .pill", "") for block in phone[1:]]
    body = next(body for body in bodies if body)
    assert "white-space: normal" in body
    assert "text-overflow" not in body
    assert "overflow: hidden" not in body


def test_the_content_trees_folder_expander_is_a_24_px_icon():
    """1.32 QA L3: the expander was a 20 x 20 ghost action button, below the 24 px target size and
    outside the icon exemption. It is a square .ico: 24 px, 32 px under a finger."""
    content = (ROOT / "src" / "tow" / "static" / "content.css").read_text(encoding="utf-8")
    script = (ROOT / "src" / "tow" / "static" / "content.js").read_text(encoding="utf-8")
    assert 'toggle.className = "ico ghost content-toggle"' in script
    body = _first(".content-folder > button.ico.content-toggle", content)
    assert "width: 1.5rem" in body
    assert "height: 1.5rem" in body
    coarse = content.partition("@media (pointer: coarse) {")[2]
    assert "width: 2rem; height: 2rem" in coarse
