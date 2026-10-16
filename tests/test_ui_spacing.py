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


def test_settings_sections_panels_and_cards_on_the_scale():
    """One padding per box, no compact duplicates: a section's row 6/12 px, its panel 9/12 px (9 on a
    phone), a card 9 px all round with its head 6 px above its line and its body 6 px under it, the
    links below the sections with their titles where the section titles start (12 + 18 px)."""
    summary = _rule(".settings-accordion > summary")
    assert "grid-template-columns: var(--sp-4) minmax(0, 1fr) auto" in summary  # a 12 px chevron slot
    assert "gap: var(--sp-2)" in summary
    assert "padding: var(--sp-2) var(--sp-4)" in summary
    assert "outline-offset: -3px" in _rule(".settings-accordion > summary:focus-visible")  # not clipped
    panel = _rule(".settings-panel")
    assert "padding: var(--sp-3) var(--sp-4)" in panel
    assert "gap: var(--sp-3)" in panel
    assert "  .settings-panel { padding: var(--sp-3); }" in CSS  # a phone
    assert "padding: var(--sp-3)" in _rule(".integration-card")
    assert "margin-top: var(--sp-2)" in _rule(".integration-head + .card-body")
    assert "gap: var(--sp-3)" in _rule(".card-body")
    head = _rule(".integration-head")
    assert "padding-bottom: var(--sp-2)" in head
    assert "gap: var(--sp-3)" in head  # a title and its pills at least 9 px apart
    assert "calc(var(--sp-4) + var(--sp-5))" in _rule(".settings-link")
    assert "padding: var(--sp-2) var(--sp-3)" in _rule(".backup-inventory > summary, .backup-location > summary")
    assert "padding: 0 var(--sp-3) var(--sp-3)" in _rule(".backup-location > form")
    for duplicate in (".settings-page-compact .settings-panel", ".settings-page-compact .integration-card"):
        assert "padding" not in _rule(duplicate), duplicate
    assert "margin" not in _rule(".backup-card h2")
    assert ".backup-card form" not in CSS


def test_settings_button_only_forms_and_empty_rows_take_no_place():
    page = (ROOT / "tests" / "golden" / "settings-page.html").read_text(encoding="utf-8")
    forms = re.findall(
        r'<form id="(?:ping|default|remove|notifier-test|notifier-remove|protection-remove)-[^"]*"[^>]*>', page
    )
    assert forms
    assert all(" hidden" in tag for tag in forms), forms
    assert "display: none" in _rule(":is(.settings-actions, .actions):not(:has(> :not([hidden])))")
    # The language note describes the choice it sits under (3 px), inside its fieldset.
    settings = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
    fieldset = settings[settings.index('<fieldset class="theme-field" aria-describedby="language-note">') :]
    assert fieldset.index('id="language-note"') < fieldset.index("</fieldset>")


def test_home_sites_dialogs_and_the_picker_on_the_scale():
    """A top-level box is 12 px inside (the add and sign-in cards, dialogs, a message's sides, a row's
    sides), a nested one 9 px (the edit panel's block, the picker); 6 px above and below a one-line
    box (a message, a row). The edit panel was 6 px above and 16 px below its content."""
    content = (STATIC / "content.css").read_text(encoding="utf-8")
    messages = (STATIC / "messages.css").read_text(encoding="utf-8")
    assert "padding: var(--sp-4);" in _rule(".card")
    flash = _rule(".flash")
    assert "padding: var(--sp-2) var(--sp-4)" in flash
    assert "gap: var(--sp-2)" in flash
    assert "margin-bottom: var(--sp-3)" in flash
    assert "column-gap: var(--sp-2); row-gap: var(--sp-1)" in messages
    assert "padding: var(--sp-3) var(--sp-4)" in _rule(".edit-panel")
    row = _rule(".list-head, details.row-edit > summary")
    assert "padding: var(--sp-2) var(--sp-4)" in row
    assert "gap: var(--sp-1) var(--sp-4)" in row
    ops = _rule(".row-wrap > .row-ops")
    assert "top: var(--sp-2)" in ops
    assert "right: var(--sp-4)" in ops  # at the row's padding edge
    for dialog in (".credential-prompt[open]", "dialog.confirm"):
        assert "padding: var(--sp-4)" in _rule(dialog), dialog
        assert "2 * var(--gutter)" in _rule(dialog), dialog
    assert "max-width: calc(46rem + 2 * var(--sp-4) + 2px)" in _rule("details#new.add.card")
    assert "padding: var(--sp-3)" in _rule(".content-picker", content)
    assert "min-block-size: 24px" in _rule(".content-file label, .content-folder label", content)
    assert "padding" not in _rule(".content-file, .content-folder", content)
    # 8 px a level of the tree.
    steps = re.findall(r'\[data-depth="(\d)"\] \{ padding-inline-start: ([.\d]+)rem; \}', content)
    assert [(int(d), 16 * float(v)) for d, v in steps] == [(d, 8.0 * d) for d in range(1, 9)]
    assert "margin" not in _rule(".dot")
    assert "padding-block: var(--sp-3);" in _rule("main")


# The spacing that is a component's own geometry, not a distance between blocks: selector, property,
# the reason. Anything else in the four style sheets is 0, auto, a token or a calc() of tokens.
EXCEPTIONS = {
    ("app.css", "header.app", "padding-inline"): "the gutter, or the 90 rem page's own edge beside it",
    ("app.css", ".hdr-sites", "padding-right"): "room for the strip's fading end (its mask)",
    ("app.css", "input, textarea, select", "padding"): "a field's text centred by the field's height",
    ("app.css", "textarea", "padding-block"): "a one-row rule field as tall as a field",
    ("app.css", ".secret > input", "padding-right"): "room for the eye inside the field",
    ("app.css", '.folder-input input[role="combobox"]', "padding-right"): "room for the folder button",
    ("app.css", ".sr-only", "margin"): "the standard visually hidden pattern",
    ("app.css", ".check-field input", "margin-top"): "the box on the words' first line",
    ("app.css", ".check-field label", "margin"): "a one-line label's 24 px target taken back from its line",
    ("app.css", ".check-field label", "padding"): "a one-line label's 24 px target",
    ("app.css", ".notice > svg.i", "margin-top"): "the icon on the note's first line",
    (
        "app.css",
        ":is(.technical-details, details.advanced, .backup-inventory, .backup-location) > summary::before",
        "margin-inline-end",
    ): "the chevron (.45rem) fills a 12 px slot",
    ("app.css", ":is(.notifier-card, .client-card, .protection-card) > summary h2::before", "margin-inline-end"): (
        "the chevron (.45rem) fills a 12 px slot, then 6 px"
    ),
    ("app.css", ".theme-choice", "gap"): "one segmented control (2 px seams)",
    ("app.css", ".theme-choice", "padding"): "one segmented control (2 px rim)",
    (
        "app.css",
        '.portable-import-form input[type="file"], .content-picker input[type="file"]',
        "padding",
    ): "the browser's file field as tall as a field, its button centred",
    ("app.css", ".row-progress > a", "padding-block"): "a 24 px target over the progress words",
    ("app.css", ".row-progress > a", "margin-block"): "the 24 px target taken back from the row",
    ("app.css", ".attention-count", "padding"): "a counter badge on an icon",
    (
        "app.css",
        (
            "details.row-edit > summary > .topic-name, details.row-edit > summary > [data-hl], "
            "details.row-edit.sites > summary > :first-child"
        ),
        "padding-right",
    ): "a phone row's text keeps clear of its three icons",
    ("app.css", "details.row-edit.sites > summary > .mirror-list", "padding-right"): "clear of the row's icons",
    ("app.css", "body", "padding-bottom"): "room for the version at the page's end",
    ("app.css", "#topics details.row-edit > summary > .dot", "margin-top"): "the dot on the name's first line",
    ("app.css", ".follow-row > .check-label", "margin-top"): "level with the interval field beside it",
    ("app.css", ".panel-top a", "padding-block"): "an inline link's 24 px target, its line unchanged",
    ("messages.css", ".msg-count", "padding"): "a counter badge on an icon",
    ("updates.css", "body:has(> .app-version)", "padding-bottom"): "room for the version at the page's end",
} | {
    ("content.css", f'.content-tree [data-depth="{depth}"]', "padding-inline-start"): "8 px a level of the tree"
    for depth in range(1, 9)
}
SPACING = re.compile(
    r"^(?:(?:margin|padding)(?:-(?:top|right|bottom|left|inline|block)(?:-(?:start|end))?)?|gap|row-gap|column-gap)$"
)


def _declarations(css: str) -> list[tuple[str, str, str]]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    out, stack, buf = [], [], ""
    for ch in css:
        if ch == "{":
            stack.append(" ".join(buf.split()))
            buf = ""
        elif ch == "}":
            selector = stack.pop() if stack else ""
            for declaration in buf.split(";"):
                if ":" in declaration and not selector.startswith("@"):
                    prop, value = declaration.split(":", 1)
                    out.append((selector, prop.strip(), value.strip()))
            buf = ""
        else:
            buf += ch
    return out


def _on_scale(value: str) -> bool:
    value = re.sub(r"var\(--(?:sp-[1-5]|gutter)\)", "T", value)
    if value == "inherit":
        return True
    parts = re.findall(r"calc\([^;]*?\)(?=\s|$)|\S+", value)
    return all(part in ("0", "T", "auto") or re.fullmatch(r"calc\([T\d\s*+\-/()]*\)", part) for part in parts)


def test_every_distance_is_on_the_scale_or_a_named_exception():
    """No margin, padding or gap in the four style sheets but 0, auto, a token, a calc() of tokens or
    a listed exception (a component's geometry, with its reason); every exception is still used."""
    seen, off = set(), []
    for name in ("app.css", "content.css", "messages.css", "updates.css"):
        for selector, prop, value in _declarations((STATIC / name).read_text(encoding="utf-8")):
            if not SPACING.match(prop) or _on_scale(value):
                continue
            key = (name, selector, prop)
            if key in EXCEPTIONS:
                seen.add(key)
            else:
                off.append(f"{name}: {selector} {{ {prop}: {value} }}")
    assert not off, "\n".join(off)
    assert seen == set(EXCEPTIONS), set(EXCEPTIONS) - seen


def test_browser_pass_targets_clipping_and_empty_rows():
    """Found in the browser (1.32.3 QA): links alone on their line were 18.9-22.5 px targets; the site
    strip's names lost 2 px of their letters (line-height 1 in a strip that scrolls) and the strip was a
    16 px target; Check all's pills stretched to their row (text 3.5 px high); an empty actions box of a
    message took a row (and 3 px) in the phone strip; a copy's row on a phone put 12 px between its words
    and its buttons."""
    messages = (STATIC / "messages.css").read_text(encoding="utf-8")
    links = [body for sel, body in _rules(CSS) if ".help-back, .link-row, .step-head, .check-result-row" in sel]
    assert links
    assert "min-block-size: 1.5rem" in links[0]
    assert "line-height: 1.2" in _rule(".trk")
    assert "min-height: 1.5rem" in _rule(".hdr-sites")
    assert "align-items: center" in _rule(".check-result-row")
    assert ".flash[data-kind] > .msg-actions:empty { display: none; }" in messages.replace(".msg-actions:empty, ", "")
    assert ".restore-point-row { grid-template-columns: 1fr; row-gap: var(--sp-2); }" in CSS


def test_the_text_after_a_chevron_starts_18px_from_it_and_menu_rings_stay_inside():
    """A pseudo-element is no `*`: the chevron's 1.5 px border came on top of its .36rem box and put every
    disclosure's and card's title at 19.3 px. The chevron is one border-box now, in a 12 px slot, then
    6 px. A menu scrolls, so it clips: its rows' focus rings go inside them. The version badge's links
    and the links alone on their line are 24 px targets."""
    chevron = [body for sel, body in _rules(CSS) if sel.endswith(".settings-accordion > summary::before")]
    assert chevron
    assert "box-sizing: border-box" in chevron[0]
    assert "width: .45rem" in chevron[0]
    assert "outline-offset: -2px" in _rule(".sort-menu .sort-option:focus-visible")
    updates = (STATIC / "updates.css").read_text(encoding="utf-8")
    assert "min-block-size: 1.5rem" in _rule(".app-version > a", updates)
    for name in (
        "index.html",
        "_topic_edit.html",
        "_topic_row.html",
        "settings_help.html",
        "_settings_protection.html",
    ):
        assert "link-row" in (TEMPLATES / name).read_text(encoding="utf-8"), name
