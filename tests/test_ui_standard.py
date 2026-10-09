"""The owner's UI standard: every action button is 26 px, everywhere (AGENTS.md, "UI standard").

Buttons get their size from one shared rule (`button, .btn`); no page, card or form may size
a button on its own. Only square icon buttons (.ico, .row-ico, .flash-x) are exempt.
"""

from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).parents[1] / "src" / "tow" / "static" / "app.css").read_text(encoding="utf-8")
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


def test_help_anchor_desktop_clearance_does_not_add_settings_card_layout():
    body = dict(_rules(CSS)).get(".help-page h2[id]", "")
    assert "scroll-margin-top: 3.3rem" in body
    assert re.sub(r"\s+", "", body) == "scroll-margin-top:3.3rem;"


def test_help_anchor_mobile_clearance_matches_the_two_row_header():
    mobile = CSS.partition("@media (max-width: 720px) {")[2]
    body = dict(_rules(mobile)).get(".settings-section, .help-page h2[id]", "")
    assert "scroll-margin-top: 5.5rem" in body


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
    home = (Path(__file__).parents[1] / "src" / "tow" / "templates" / "index.html").read_text(encoding="utf-8")
    assert 'class="topic-tracker" data-label="{{ t(\'home.row.site\') }}" title="{{ site_title(topic.tracker)' in home
    sites = first(".list-head.sites, details.row-edit.sites > summary")
    assert "minmax(10rem,1fr) 8rem 6.4rem" in sites
    assert "justify-self: start" in first("details.row-edit.sites > summary > .pill")
