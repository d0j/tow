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


# 1.32.2: exactly three field sizes (the owner: "all the same, clear, compact"). Short: every number;
# standard: passwords, names, tokens, keys, chat IDs, hosts, reminders, site codes, searches and every
# select; long: links, webhooks, folders, site paths, rules, host lists and a topic's name.
SIZES = {"w-xs": "5.5rem", "w-m": "15rem", "w-xl": "36rem"}  # 88, 240 and 576 px
GONE = ("w-2xs", "w-s", "w-l")
LONG = {
    "url",
    "from_url",
    "title",
    "save_path",
    "path",
    "selection_value",
    "fetch_hosts",
    "login_hosts",
    "url_regex",
    "download_href_regex",
    "download_path",
    "topic_path",
    "login_path",
    "infohash_path",
    "provider_url",
}
TEMPLATES = ROOT / "src" / "tow" / "templates"
FIELD = re.compile(r"<(input|select|textarea)\b(?:[^>{]|\{\{.*?\}\}|\{%.*?%\})*>", re.DOTALL)


def _fields() -> list[tuple[str, str, str]]:
    """Every visible field of every template: (template, its tag, the tag's text)."""
    found = []
    for path in sorted(TEMPLATES.glob("*.html")):
        source = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        for match in FIELD.finditer(source):
            tag = match.group()
            kind = re.search(r'\stype="([^"]+)"', tag)
            if kind and kind.group(1) in ("hidden", "checkbox", "radio", "file", "submit"):
                continue
            found.append((path.name, match.group(1), tag))
    return found


@pytest.mark.parametrize(("name", "width"), list(SIZES.items()))
def test_a_field_is_one_of_three_sizes_at_most(name, width):
    """A port, a password and a webhook were all 1354 px wide at 1440 px. A width class is a
    maximum: the field still shrinks with a narrower form. 1.32.2 keeps three of 1.32.1's six:
    88 px for numbers (65535, 10080 and 86400 fit), 240 px for what one types or picks, 576 px for
    links, paths and rules; a secret field's wrapper and a folder field's take their field's."""
    assert _first(f".{name}").strip() == f"max-width: {width};"
    if name != "w-xs":  # no secret or folder holds a number
        assert f"max-width: {width}" in _first(f":is(.secret, .folder-input):has(> .{name})")


def test_no_other_field_size_is_left():
    """1.32.1 had 72, 192 and 416 px too, and the sign-in page's password filled its card: the
    same kind of field had three widths on three screens."""
    for gone in GONE:
        assert not re.search(rf"\.{gone}\b", CSS), gone
        for page, _, tag in _fields():
            assert not re.search(rf"\b{gone}\b", tag), (page, tag)
    assert ".auth-form .secret" not in CSS
    for name in ("#list-sort", "#list-tracker"):
        assert f"{name} {{" not in CSS  # a select is a select: the standard size


def test_every_field_has_its_size_by_what_it_holds():
    """Every field on every screen (Home, Sites, Settings, Setup, Login, the dialogs and the edit
    panels) carries exactly one of the three classes, chosen by what it holds."""
    seen = set()
    for page, _, tag in _fields():
        sizes = [size for size in SIZES if re.search(rf'class="[^"]*\b{size}\b', tag)]
        if 'class="w-{{ f.width }}"' in tag:  # a messenger's field: its module says (test below)
            continue
        name = re.search(r'\sname="([^"]+)"', tag) or re.search(r'\sid="([^"]+)"', tag)
        key = name.group(1) if name else tag
        kind = re.search(r'\stype="([^"]+)"', tag)
        if kind and kind.group(1) == "number":
            want = "w-xs"
        elif key in LONG:
            want = "w-xl"
        else:  # a password, a name, a host, a reminder, a code, a search, every select
            want = "w-m"
        assert sizes == [want], (page, key, sizes)
        seen.add(want)
    assert seen == set(SIZES)


def test_a_messengers_fields_have_their_size_too():
    """Telegram's token was 416 px, WhatsApp's phone and key 192 px, ntfy's server 416 px: a
    messenger's field says its size by the same three, and a secret is standard - but for the
    Discord webhook, which is a link (long)."""
    from tow.notifiers import kinds

    template = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
    assert template.count('class="w-{{ f.width }}"') == 2  # always one class: no field without
    for kind, module in kinds().items():
        for field in module.FIELDS:
            link = field.placeholder.startswith("http") or field.pattern.startswith("https://")
            want = "xl" if link else "m"
            assert field.width == want, (kind, field.name, field.width)


def test_a_phone_field_fills_its_line_and_a_number_stays_short():
    """1.32.1 F3: at 360-390 px the classes still capped fields, so one form ended at 288, 306 and
    346 px. On a phone a text, select, secret or folder field has no cap and fills its line in a
    row; numbers keep theirs."""
    blocks = CSS.split("@media (max-width: 720px) {")[1:]
    phone = next(block.split("\n}")[0] for block in blocks if ".field-row >" in block.split("\n}")[0])
    caps = ":is(input, select, textarea):is(.w-m, .w-xl), :is(.secret, .folder-input):has(> :is(.w-m, .w-xl))"
    assert _first(caps, phone).strip() == "max-width: none;"
    assert "flex: 1 1 12rem" in _first(".field-row > :is(.w-m, .w-xl, :has(.w-m, .w-xl))", phone)
    assert "w-xs" not in caps


def test_one_form_layout_with_rows_that_wrap_by_the_forms_own_width():
    """One form system: at most 46 rem wide, a label 3 px above its field, 8-10 px between groups,
    help text at most 68 characters a line. 1.32.1 (F4): a row of fields wraps by its fields'
    classes instead of waiting for a 38 rem form - Setup, the sign-in dialog and half cards stacked
    two 288 px fields with half the card empty. Two 240 px fields share a line from 492 px on."""
    grid = _first(".form-grid")
    for declaration in ("display: grid", "max-width: 46rem", "container-type: inline-size"):
        assert declaration in grid
    gap = re.search(r"(?<![\w-])gap: \.(\d+)rem", grid)
    assert gap
    assert 8 <= 16 * float("." + gap.group(1)) <= 10
    assert "gap: .1875rem" in _first(".form-grid .field")  # 3 px
    # The older "next field" margins (.field + .field, the compact Settings one) pushed the second
    # field of a row 11 px down (seen in a browser); in the form the gap alone spaces groups.
    assert _first(".form-grid .field + .field.field").strip() == "margin-top: 0;"
    assert "max-width: 68ch" in _first(".form-grid :is(.field-hint, .action-hint, .section-note, .check-field p)")
    # A panel's intro and notes end where its widest field ends (1.32 QA L1: 440 px beside 576 px).
    assert _first(".settings-panel > p, .support-note").strip() == "max-width: 36rem;"
    assert "support-note" not in "".join(selector for selector, body in _rules(CSS) if "68ch" in body)
    row = _first(".field-row")
    assert "flex-flow: row wrap" in row
    assert "flex-direction: column" not in row
    assert "flex: 1 1 12rem" in _first(".field-row > *")
    assert "min-width: 0" in _first(".form-grid > *, .field-row > *")
    assert "flex: 0 1 auto" in _first(".field-row > :is(.w-xs, :has(.w-xs))")
    assert "flex: 0 1 15rem" in _first(".field-row > :is(.w-m, :has(.w-m))")
    # 1.32.2 browser QA: a number whose label and hint are narrower than 88 px ("От 1 до 4.") was
    # 84 px in its row, and ntfy's server shared a 768 px card's line with the topic at 447 px. A
    # short field keeps its 88 px; a long one needs its 576 px or takes a line of its own.
    assert "min-width: 5.5rem" in _first(".field-row > :is(.w-xs, :has(.w-xs))")
    assert "flex: 1 1 36rem" in _first(".field-row > :is(.w-xl, :has(.w-xl))")
    # No form-width switch for rows any more, and no "keep" variant: a pair wraps on its own.
    assert ".field-row.keep" not in CSS
    assert ".field-row {" not in "".join(CSS.split("@container (min-width: 38rem) {")[1:])


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
    # 1.32.1 (C6): the .field-grid rules went with the last template that used them.
    assert ".field-grid" not in CSS


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
    # 1.32.1 ST1: "Реквизиты сохранены" is 136 px in Chromium; 8 rem cut it to "Реквизиты сохран…".
    assert "minmax(10rem,1fr) 9.5rem 6.4rem" in sites
    assert "justify-self: start" in first("details.row-edit.sites > summary > .pill")


def test_browser_sign_in_puts_the_keep_note_aside_with_the_password():
    """1.32.1 S1: a site with a saved password says "leave empty to keep" once under its sign-in;
    with browser sign-in chosen the password steps aside, and its note goes with it."""
    hidden = _first('.site-form:has(input[name="browser_auth"]:checked) :is(.password-group, .keep-note)')
    assert hidden.strip() == "display: none;"


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


def test_a_file_in_the_contents_list_sits_under_its_folder():
    """QA Q5: a file row has no expander, so its checkbox sat left of its parent folder's (68 vs
    92 px). A spacer as wide as the expander (24 px, 32 px under a finger) puts files in the
    column of their sibling folders' boxes, one step right of their parent's."""
    content = (ROOT / "src" / "tow" / "static" / "content.css").read_text(encoding="utf-8")
    spacer = _first(".content-file::before", content)
    assert 'content: ""' in spacer
    assert "flex: none" in spacer
    assert "width: 1.5rem" in spacer
    coarse = content.partition("@media (pointer: coarse) {")[2]
    assert ".content-file::before { width: 2rem; }" in coarse


def test_the_edit_panels_buttons_wrap_whole_on_a_small_phone():
    """1.32 QA L2: at 320 px "Убрать из TOW" wrapped inside its button (32 px high, the rule is 26)."""
    actions = _first(".edit-actions")
    assert "flex-wrap: wrap" in actions
    assert "white-space: nowrap" in _first(".edit-actions > button")


def test_the_content_pickers_parts_stand_apart():
    """1.32 QA L4: the help sat right under "Get contents", and the status lines stuck to "Other
    sources" and to "Find a file"."""
    content = (ROOT / "src" / "tow" / "static" / "content.css").read_text(encoding="utf-8")
    assert "margin-top: .45rem" in _first(".content-picker > :not(:first-child):not(:empty)", content)


def test_the_language_is_one_segmented_choice_like_the_theme():
    """1.32.1 L1: the language is three submit buttons (a select changed with the arrow keys would
    reload at each step). They were a solid button and two outlines in a row of actions; they are
    the theme's segmented control now - the chosen one raised (aria-pressed), each segment a 26 px
    target that wraps whole on a small phone. The 1.32 select's phone rule went with the select."""
    from bs4 import BeautifulSoup
    from fastapi.testclient import TestClient

    from tow.web import app

    page = BeautifulSoup(TestClient(app).get("/settings").text, "html.parser")
    form = page.select_one('form[action="/settings/language"]')
    buttons = form.select('button[name="language"]')
    assert len(buttons) == 3
    assert all(b.find_parent(class_="theme-choice") for b in buttons)
    assert [b["class"] for b in buttons] == [["theme-option"]] * 3
    assert [b["aria-pressed"] for b in buttons].count("true") == 1
    assert "background: transparent" in _first(".theme-option")
    assert "border: 0" in _first(".theme-option")
    # A theme option is a label: without this the shared label margin made its box 44 px beside the
    # language's 32 px and pushed the options 9 px down (measured in Chromium).
    assert "margin: 0" in _first(".theme-option")
    raised = _first('.theme-option:has(input:checked), .theme-option[aria-pressed="true"]')
    assert "background: var(--panel)" in raised
    assert "outline: 2px solid var(--acc)" in _first(
        ".theme-option:has(input:focus-visible), .theme-option:focus-visible"
    )
    assert ".inline-save > select" not in CSS
    # And the automatic choice says itself in a few words ("Автоматически — язык браузера (сейчас:
    # Ру…" was still cut off at 390 px on its own line).
    from tow import i18n

    for lang in ("en", "ru"):
        assert len(i18n.translate("settings.language.auto", lang, name="Русский")) <= 30  # about 33 fit at 390 px


def test_the_language_segments_are_as_wide_as_their_buttons():
    """QA Q7: in the language form the segmented track stretched to the form's width (438 px in ru,
    set by the note under it), with an empty end after the last button; the theme's track is as
    wide as its options. A grid item that keeps its own width: both are."""
    assert "justify-self: start" in _first(".theme-field .theme-choice")


def test_the_help_question_mark_is_a_28_px_target_and_the_copy_delete_page_a_settings_width():
    """QA Q10: the Sites "?" help summary was the one disclosure under 28 px (A1: 24 px). The page
    that deletes a backup copy is as wide as Settings, where it comes from: 1.32.1 capped both at
    64 rem; 1.32.2 gives Settings the page's width back, and this page with it (no cap of its own)."""
    sizes = [body for selector, body in _rules(CSS) if selector == "details.help > summary"]
    assert "width: 1.75rem; height: 1.75rem" in sizes[-1]  # the last one wins
    assert ".backup-delete-page" not in CSS
    template = (ROOT / "src" / "tow" / "templates" / "backup_delete.html").read_text(encoding="utf-8")
    assert "backup-delete-page" not in template
    assert '{% block body %}\n<h1 id="page-title">' in template  # straight in <main>, as Settings' column


def test_the_history_search_fills_its_line_on_a_phone():
    """QA Q11: at 390 px the History search stopped at 296 of 374 px (its 18rem basis), the one
    ragged phone edge left after F3. On a phone it takes its own full line; on a desktop it is the
    standard 240 px field (1.32.2)."""
    rule = '.history-filter > form[role="search"]'
    phone = "@media (max-width: 720px) { " + rule + " { flex: 1 1 100%; } }"
    assert phone in CSS
    assert CSS.index(phone) > CSS.index(rule + " { flex: 0 1 15rem; }")  # it wins (the standard size)


def test_the_check_all_cards_title_is_a_card_title():
    """1.32 QA L8: "Results" used the browser's 1.5em (22 px); every card or dialog title is 1.05rem."""
    assert "font-size: 1.05rem" in _first(".check-results h2")
    assert "font-size: 1.05rem" in _first(".add-title")
    # 1.32.1 CR1: the run's time beside it ("Results · 20:31") is not bold like the title.
    assert _first(".check-results h2 .sub").strip() == "font-weight: 400;"


def test_diagnostics_text_runs_no_wider_than_a_form():
    """1.32.1 DG1: the torrent client's notes ran 1360 px wide; Diagnostics' paragraphs end where a
    form ends (46 rem)."""
    assert _first(".doctor-page p").strip() == "max-width: 46rem;"


def test_a_low_screen_keeps_its_height_for_the_page():
    """1.32 QA L9: at 200 % zoom (450 CSS px high) the sticky header and message strip took about
    30 % of the screen. Below 520 px of height neither sticks, and a target clears nothing."""
    low = CSS.partition("@media (max-height: 520px) {")[2]
    assert "position: static" in _first("header.app", low)
    assert "--land-top: .75rem" in _first(":root", low)
    assert CSS.rindex("@media (max-height: 520px) {") > CSS.rindex("--land-top: calc(")  # it wins
    messages = (ROOT / "src" / "tow" / "static" / "messages.css").read_text(encoding="utf-8")
    assert "@media (max-height: 520px) { .msg-host { position: static; } }" in messages


def test_sign_in_and_first_start_buttons_keep_their_width_on_a_phone():
    """1.32 QA N2: Login's and Setup's buttons went full width on a phone (.settings-actions
    button), while Settings keeps them as wide as their words."""
    phone = _block("@media (max-width: 720px) {\n  /* F3")
    assert "width: auto" in _first(".auth-card .settings-actions button", phone)
    for page in ("login.html", "setup.html"):  # both cards hold their buttons in .settings-actions
        template = (ROOT / "src" / "tow" / "templates" / page).read_text(encoding="utf-8")
        assert "auth-card" in template
        assert 'class="settings-actions"' in template


def test_the_download_list_is_as_wide_as_its_lines():
    """1.32 QA N3: the downloads popover was 92 vw wide for three short lines."""
    assert _first("dialog#download-pop").strip() == "width: min(92vw, 40rem);"


def test_a_grey_pill_shows_as_a_pill_on_a_client_card():
    """1.32 QA N4: "Main" was a --panel2 pill on a --panel2 card: plain bold text."""
    assert "background: var(--panel2)" in _first(".integration-card")
    assert _first(".integration-card .pill.mut").strip() == "background: var(--panel);"


def _render(source: str, lang: str = "ru") -> str:
    from tow import i18n
    from tow.web import app as _app  # noqa: F401 - configures the templates' globals
    from tow.web.templating import TEMPLATES

    i18n.use(lang)
    try:
        return TEMPLATES.env.from_string(source).render()
    finally:
        i18n.use("en")


def test_a_saved_secret_says_so_in_its_label():
    """1.32.1 (S1): "Saved · leave empty to keep" under every stored secret (twice in one row of
    messengers, and under "New password", where it was wrong) is a small "saved" tag in the label,
    a placeholder and one note per form. Every caller uses the tag now, so the older sentence and
    its key are gone."""
    page = _render(
        '{% from "_secret.html" import saved_hint, keep_note %}'
        '<label for="x">Токен{{ saved_hint("x-saved") }}</label>'
        '|{{ keep_note("f-keep") }}'
    )
    tag, note = page.split("|")
    assert tag == '<label for="x">Токен <span class="saved-tag" id="x-saved">сохранён</span></label>'
    assert note == '<p class="field-hint keep-note" id="f-keep">Оставьте пароли и ключи пустыми, чтобы не менять.</p>'
    from tow import i18n

    for lang in ("en", "ru"):
        assert "web.secret.saved_keep" not in i18n.texts_with_prefix("web.secret.", lang)
        assert "web.secret.saved_tag" in i18n.texts_with_prefix("web.secret.", lang)

    assert i18n.translate("web.secret.keep_placeholder", "ru") == "Пусто — не менять"
    assert i18n.translate("web.secret.keep_placeholder", "en") == "Leave empty to keep"
    assert "background: var(--panel2)" in _first(".saved-tag")


def test_a_file_field_is_in_the_pages_language_and_works_without_a_script():
    """1.32.1 (B2): the browser's "Choose File / No file chosen" (its own language, 12.5 px) is a
    button-like label and the file's name in the page's words; without a script the browser's field
    shows (the proxy is hidden until app.js marks it ready) and keeps the keyboard's focus."""
    page = _render(
        '{% from "_file.html" import file_input %}'
        '{{ file_input("f", "backup_file", accept=".towx", required=true, data_content_upload=true, title=false) }}'
    )
    field = '<input type="file" id="f" class="file-native" name="backup_file" accept=".towx" required '
    field += "data-content-upload>"
    assert field in page
    assert '<label for="f" class="btn ghost file-button" aria-hidden="true">Выбрать файл</label>' in page
    assert '<span class="file-name" data-file-name>Файл не выбран</span>' in page
    assert "title" not in page
    assert "display: none" in _first(".file-pick > .file-button, .file-pick > .file-name")
    hidden = _first(".file-pick[data-ready] > .file-native")
    assert "opacity: 0" in hidden
    assert "display: none" not in hidden  # still focusable
    focus = ".file-pick[data-ready]:has(> .file-native:focus-visible) > .file-button"
    assert "outline: 2px solid var(--acc)" in _first(focus)


def test_a_hint_under_a_wide_field_runs_as_wide_as_the_field():
    """QA Q4: hints stopped at 68ch (440 px) under 576 px folder fields; under a .w-xl field the
    hint ends where the field ends, outranking the hints' 68ch rules. An unbreakable piece of a
    hint (.nowrap: a backup folder's free space with its separator) keeps together."""
    wide = ".field:has(> .w-xl, > .folder-input > .w-xl) > .field-hint"
    assert _first(wide).strip() == "max-width: 36rem;"
    assert CSS.index(wide) > CSS.index(".settings-panel :is(.field-hint, .action-hint, .section-note")
    assert _first(".nowrap").strip() == "white-space: nowrap;"


def test_every_secret_field_is_the_same_on_every_screen():
    """1.32.2: a password, a token or a key looks and works the same on every screen (Settings,
    Sites, Setup, Login, the sign-in dialog): the standard size (the Discord webhook, a link, is
    long), in its .secret wrapper with the eye for that very field right after it, and a saved one
    says "saved" in its label with the "leave empty to keep" placeholder."""
    found = 0
    for path in sorted(TEMPLATES.glob("*.html")):
        if path.name == "_secret.html":
            continue
        source = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
        for match in FIELD.finditer(source):
            tag = match.group()
            if 'type="password"' not in tag:
                continue
            found += 1
            assert source[: match.start()].endswith('<span class="secret">'), (path.name, tag[:80])
            field_id = re.search(r'\sid="([^"]+)"', tag).group(1)
            after = source[match.end() :]
            assert after.startswith("{{ reveal("), (path.name, field_id, after[:60])
            eye_for = after[len("{{ reveal(") :].split(",", 1)[0]
            same = re.compile(r"[{}'~\s-]")
            assert same.sub("", eye_for) == same.sub("", field_id), (path.name, field_id, eye_for)  # its own eye
            assert after.split("</span>", 1)[0].count("{{") == 1, (path.name, field_id)  # the eye, then the end
            assert 'class="w-m"' in tag or 'class="w-{{ f.width }}"' in tag, (path.name, field_id)
            if "keep_placeholder" in tag:
                assert "saved_hint(" in source, (path.name, field_id)
    assert found >= 10


def test_one_label_gap_and_one_pair_gap_everywhere():
    """1.32.2: a label sits 3 px above its field on every screen (Settings had its own 2.6 px, a
    field outside a form 4.5 px), a search's label too (it wrapped its field); fields that share a
    line are 12 px apart (the three site-access limits were spread over the whole card)."""
    assert "gap: .1875rem" in _first(".field")
    gaps = [(selector, body) for selector, body in _rules(CSS) if re.search(r"\.field$", selector) and "gap" in body]
    assert all("gap: .1875rem" in body for _, body in gaps), gaps
    assert "gap: .55rem .75rem" in _first(".field-row")
    assert ".protection-limits .field-row > .field" not in CSS
    for path in sorted(TEMPLATES.glob("*.html")):
        source = path.read_text(encoding="utf-8")
        for label in re.findall(
            r"<label\b[^>]*>(?:(?!</label>).)*?<(?:input|select|textarea)\b[^>]*>", source, re.DOTALL
        ):
            assert re.search(r'type="(checkbox|radio|file)"', label), (path.name, label[:120])
    picker = (TEMPLATES / "_content_picker.html").read_text(encoding="utf-8")
    assert '<div class="field"><label for="{{ search_id }}">' in picker
