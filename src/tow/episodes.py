from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from tow.jsonish import as_dict


@dataclass(frozen=True, slots=True)
class EpisodeLabel:
    episode: int
    season: int | None = None

    @property
    def key(self) -> str:
        if self.season is None:
            return f"episode:e{self.episode:02d}"
        return f"episode:s{self.season:02d}e{self.episode:02d}"


def parse_expected_count(title: str) -> dict[str, Any] | None:
    text = str(title or "")
    candidates: list[tuple[int, bool, int]] = []
    previous_count_end = 0
    for match in re.finditer(
        r"(?i)(?P<current>\d{1,4}(?:[xх]\d{1,4})?(?:\s*[-–—]\s*\d{1,4})?)"
        r"\s*(?P<episode_word>сери(?:я|и|й)|эпизод(?:ы|а|ов)?|episodes?|eps?)?"
        r"\s*(?:\bиз\b|\bof\b)\s*(?P<total>\d{1,4})\b",
        text,
    ):
        clause = re.split(r"[\[\](),;/|]", text[previous_count_end : match.start()])[-1]
        previous_count_end = match.end()
        clause = f"{clause}{match.group('current')}"
        following = re.split(r"[\[\](),;/|]", text[match.end() :])[0]
        explicit_episode = bool(
            match.group("episode_word")
            or re.search(r"(?i)\b(?:сери(?:я|и|й)|эпизод(?:ы|а|ов)?|episodes?|eps?)\b", clause)
            or re.search(r"(?i)s\d{1,2}e\d{1,4}", clause)
            or (
                re.search(
                    r"(?i)(?:\b(?:season|сезон)\s*\d{1,2}|(?<!\w)s\d{1,2})\s+\d{1,4}\s*[-–—]\s*\d{1,4}$",
                    clause,
                )
                is not None
            )
        )
        if re.match(
            r"(?i)\s*(?:[-–—]?\s*[xх]\s*)?(?:сезон(?:ов|ы|а)?|seasons?|parts?|vol(?:ume)?s?\.?|том(?:ов|а|ы)?|част(?:ь|и|ей)|фильм(?:ов|а|ы)?|movie)\b",
            following,
        ):
            continue
        if not explicit_episode and (
            re.search(
                r"(?i)\b(?:сезон(?:ов|ы|а)?|seasons?|parts?|vol(?:ume)?s?\.?|том(?:ов|а|ы)?|част(?:ь|и|ей)|фильм(?:ов|а|ы)?|movie)\b",
                clause,
            )
            or re.search(r"(?i)(?:^|[^\w])s\d{1,2}\b", clause)
        ):
            continue
        if not explicit_episode and re.search(r"(?i)\b(?:фильм|movie|film)\b", text):
            continue
        total = int(match.group("total"))
        numbers = [int(value) for value in re.findall(r"\d{1,4}", match.group("current"))]
        if total <= 0 or not numbers or numbers[-1] > total:
            continue
        if (
            len(numbers) > 1
            and "x" not in match.group("current").casefold()
            and "х" not in match.group("current").casefold()
            and numbers[-1] < numbers[0]
        ):
            continue
        current = match.group("current")
        range_start = numbers[0] if re.match(r"\d{1,4}\s*[-–—]", current) else 1
        # "73-176 серии из 176" can describe a mixed-season subset whose files
        # restart numbering each season. Without the cumulative-to-season map,
        # applying 73..176 to SxxEyy would invent a false completion target.
        if match.group("episode_word") and range_start > 1:
            continue
        candidates.append((total, explicit_episode, range_start))
    preferred = {(total, start) for total, explicit, start in candidates if explicit}
    totals = preferred or {(total, start) for total, _explicit, start in candidates}
    if len(totals) != 1:
        return None
    maximum, minimum = totals.pop()
    return {
        "kind": "episodes",
        "total": maximum - minimum + 1,
        "episode_min": minimum,
        "episode_max": maximum,
        "source": "title",
        "confidence": "high",
    }


def expected_for_topic(topic: Mapping[str, Any]) -> dict[str, Any] | None:
    """Choose the known release total or the current selected episode count."""
    selection = as_dict(topic.get("selection"))
    mode = str(selection.get("mode") or "all")
    keys = (
        sorted({str(value) for value in topic.get("selected_episode_keys") or [] if value})
        if not topic.get("selection_dirty")
        and topic.get("selection_verified") is not False
        and (
            not topic.get("selection_hash")
            or str(topic.get("selection_hash") or "").casefold() == str(topic.get("hash") or "").casefold()
        )
        else []
    )
    if mode != "all" and keys:
        return {
            "kind": "episodes",
            "total": len(keys),
            "keys": keys,
            "source": "selection",
            "confidence": "exact",
        }
    if mode != "all":
        return None
    title = str(topic.get("tracker_title") or topic.get("title") or "")
    title_total = parse_expected_count(title)
    if title_total:
        title_total["season"] = parse_season_hint(title)
    if title_total and len(keys) <= int(title_total["total"]):
        return title_total
    if keys:
        return {
            "kind": "episodes",
            "total": len(keys),
            "keys": keys,
            "source": "files",
            "confidence": "current",
        }
    return title_total


def parse_season_hint(title: str) -> int | None:
    """Extract a season only from explicit or corroborated serial title syntax."""
    text = str(title or "")
    if re.search(
        r"(?i)\bs\d{1,2}e\d{1,4}\s*[-–—~]\s*s\d{1,2}e\d{1,4}\b"
        r"|\bs\d{1,2}\s*(?:[-–—,+/&]|\band\b|\bи\b|\bto\b|\bпо\b|\s+)\s*s\d{1,2}\b"
        r"|\bs\d{1,2}\s*[-–—]\s*\d{1,2}\b(?!\s*[-–—]\s*\d)"
        r"|\b(?:season|сезон)\w*\s*[:№#.]?\s*\d{1,2}\s*(?:[-–—,+/&]|\band\b|\bи\b|\bto\b|\bпо\b)\s*(?:(?:season|сезон)\w*\s*)?\d{1,2}\b(?!\s*[-–—]\s*\d)"
        r"|\b\d{1,2}\s*(?:season|сезон)\w*\s*(?:[-–—,+/&]|\band\b|\bи\b|\bto\b|\bпо\b)\s*\d{1,2}\s*(?:season|сезон)\w*\b"
        r"|\b\d{1,2}\s*(?:[-–—,+/&]|\band\b|\bи\b|\bto\b|\bпо\b)\s*\d{1,2}\s*(?:season|сезон)\w*\b",
        text,
    ):
        return None
    patterns = (
        r"(?i)(?:^|[^\w])s(?:eason)?[ ._-]*0*(\d{1,2})(?!\d)",
        r"(?i)\b(?:season|сезон)\s*[:№#.]?\s*0*(\d{1,2})\b",
        r"(?i)\b0*(\d{1,2})\s*(?:season|сезон)\b",
        r"(?i)\[\s*0*(\d{1,2})[xх]\d{1,4}",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = int(match.group(1))
            return value if 0 < value <= 30 else None
    # A sequel number immediately before a subtitle colon is useful only when
    # the same title declares an episode total (for example "Show 3: ... 13 из
    # 14"). This avoids treating numbered films and years as TV seasons.
    if parse_expected_count(text):
        match = re.search(r"(?:^|\D)(\d{1,2})\s*:\s*\D", text)
        if match:
            value = int(match.group(1))
            return value if 0 < value <= 30 else None
    return None


def parse_episode_label(name: str) -> EpisodeLabel | None:
    labels = parse_episode_coverage(name)
    return labels[0] if labels else None


def _fractional_special(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """Fractional specials (``S01E05.5``) need their own identity; never guess."""
    if re.search(
        r"(?i)(?:s\d{1,2}[ ._-]*e|(?:episode|ep|серия|эпизод)\s*)"
        r"\d{1,4}\.\d{1,2}(?:v\d+)?\b",
        text,
    ):
        # Fractional specials need a distinct identity model. Never collapse
        # them onto the preceding normal episode.
        return ()
    return None


def _compact_sequence(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``S01E01E02``/``S01E01.S01E02`` and ``S01E01-02-03`` sequences."""
    compact_head = re.search(r"(?i)\bs(\d{1,2})e(\d{1,4})", text)
    if compact_head:
        season, first = map(int, compact_head.groups())
        episodes = [first]
        tail = text[compact_head.end() :]
        while next_episode := re.match(
            rf"(?i)(?:\.s0*{season}|\.?)[ ._]*e(\d{{1,4}})"
            r"(?!\.\d)(?=$|[ ._-]|e\d|v\d+\b)",
            tail,
        ):
            episodes.append(int(next_episode.group(1)))
            tail = tail[next_episode.end() :]
        if len(episodes) > 1 and season > 0 and all(episode > 0 for episode in episodes):
            return tuple(EpisodeLabel(episode=value, season=season) for value in dict.fromkeys(episodes))
        numeric_list = re.match(r"((?:-\d{1,4}){2,})(?!\d)", tail)
        if numeric_list and season > 0 and first > 0:
            values = [first, *[int(value) for value in re.findall(r"\d{1,4}", numeric_list.group(1))]]
            if all(value > 0 for value in values):
                return tuple(EpisodeLabel(episode=value, season=season) for value in dict.fromkeys(values))
    return None


def _repeated_episodes(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``S01E01 & E03``-style lists (three or more, or a non-dash separator)."""
    repeated = re.search(
        r"(?i)\bs(\d{1,2})[ ._-]*e(\d{1,4})"
        r"((?:\s*([-&+,/])\s*e\d{1,4})+)",
        text,
    )
    if repeated:
        tail = repeated.group(3)
        episodes = [int(repeated.group(2)), *[int(value) for value in re.findall(r"(?i)e(\d{1,4})", tail)]]
        separators = re.findall(r"[-&+,/]", tail)
        if len(episodes) >= 3 or any(separator != "-" for separator in separators):
            season = int(repeated.group(1))
            return tuple(EpisodeLabel(episode=value, season=season) for value in dict.fromkeys(episodes))
    return None


def _season_episode_range(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``S01E01``, ``S01E01-E05``, ``1x01``, ``1x01-05``."""
    match = re.search(
        r"(?i)\bs(\d{1,2})[ ._-]*e(\d{1,4})(?:"
        r"\s*(?:-|–|—|~|to)\s*(?:s\1[ ._-]*)?e(\d{1,4})"
        r"(?!\w|\.\d|[-_. ]\s*bits?\b)"
        r"|(?:-|–|—|~)(\d{1,4})(?!\w|\.\d|[-_. ]\s*bits?\b))?",
        text,
    )
    if not match:
        match = re.search(
            r"(?i)(?<!\d\.)\b(\d{1,2})[xх](\d{1,4})(?:"
            r"\s*(?:-|–|—|~|to)\s*\1[xх](\d{1,4})"
            r"(?!\w|\.\d|[-_. ]\s*bits?\b)"
            r"|(?:-|–|—|~)(\d{1,4})(?!\w|\.\d|[-_. ]\s*bits?\b))?",
            text,
        )
    if match:
        season, first = int(match.group(1)), int(match.group(2))
        if season == 0 or first == 0:
            return ()
        end_text = match.group(3) or match.group(4)
        last = int(end_text or first)
        if last < first or last - first > 1000:
            return (EpisodeLabel(episode=first, season=season),)
        return tuple(EpisodeLabel(episode=episode, season=season) for episode in range(first, last + 1))
    return None


def _season_dash_episode(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``S1 - 05`` / ``S1-Ep05``."""
    match = re.search(
        r"(?i)\bs[ ._-]*(\d{1,2})\s*(?:-|–|—)[ ._-]*"
        r"(?:e(?:p(?:isode)?)?[ ._-]*)?(\d{1,4})"
        r"(?!\w|\.\d|[-_. ]\s*bits?\b)",
        text,
    )
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        if season > 0 and 0 < episode <= 1000:
            return (EpisodeLabel(episode=episode, season=season),)
    return None


def _season_word_episode_word(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``Season 1 ... Episode 5`` / ``Сезон 1 ... Серия 5``."""
    match = re.search(
        r"(?i)\b(?:season|сезон)\s*(\d{1,2})\D{1,20}"
        r"(?:episode|ep|серия|эпизод)\s*(\d{1,4})\b",
        text,
    )
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        if season > 0 and 0 < episode <= 1000:
            return (EpisodeLabel(episode=episode, season=season),)
    return None


def _episode_word(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``E05``, ``Ep 5``, ``Серия 5``, with an optional second number or range."""
    match = re.search(
        r"(?i)(?:^|[^\w])(?:e(?:p(?:isode)?)?|серия|эпизод)\s*[._-]?(\d{1,4})"
        r"(?:v\d+)?(?!\w)"
        r"(?:\s*([-&+,/])\s*(?:e(?:p(?:isode)?)?)?(\d{1,4})"
        r"(?!\w|\.\d|[-_. ]\s*bits?\b))?",
        text,
    )
    if match:
        first, second = int(match.group(1)), int(match.group(3) or match.group(1))
        if first == 0 or second == 0:
            return ()
        if match.group(2) == "-" and first <= second and second - first <= 1000:
            return tuple(EpisodeLabel(episode=value) for value in range(first, second + 1))
        if match.group(2) and first != second:
            return (EpisodeLabel(episode=first), EpisodeLabel(episode=second))
        return (EpisodeLabel(episode=first),)
    return None


def _number_then_episode_word(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``5 серия`` / ``5 episode``."""
    match = re.search(r"(?i)\b(\d{1,4})\s*(?:серия|эпизод|episode)\b", text)
    if match:
        value = int(match.group(1))
        return (EpisodeLabel(episode=value),) if value > 0 and not 1900 <= value <= 2099 else ()
    return None


def _range_then_plural_word(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``1-5 серии`` / ``1-5 episodes``."""
    match = re.search(
        r"(?i)\b(\d{1,4})\s*[-–—]\s*(\d{1,4})\s*"
        r"(?:серии|серий|эпизоды|эпизодов|episodes)\b",
        text,
    )
    if match:
        first, last = int(match.group(1)), int(match.group(2))
        if 0 < first <= last and last - first <= 1000 and not 1900 <= first <= 2099:
            return tuple(EpisodeLabel(episode=value) for value in range(first, last + 1))
    return None


def _bare_e_number(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """`` E05`` after a separator."""
    match = re.search(r"(?i)(?:^|[ ._-])e(\d{1,4})(?:v\d+)?(?!\w)", text)
    if match:
        value = int(match.group(1))
        return (EpisodeLabel(episode=value),) if value > 0 else ()
    return None


def _anime_dash_range(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """Anime ``Show - 01-02 [1080p].mkv``."""
    # Anime releases commonly use a bare episode number after a spaced dash,
    # for example ``Show - 13 RAW.mkv``.  Requiring both the delimiter and a
    # release/file token keeps years and resolution markers from becoming
    # episode numbers accidentally.
    anime_tail = (
        r"(?=[\s_]*(?:raw\b|web(?:-dl)?\b|end\b|\[|\(|\{|\."
        r"(?:mkv|mp4|m4v|avi|mov|ts|ass|srt|vtt)\b|$))"
    )
    match = re.search(
        r"(?i)(?:\s|_)(?:-|–|—)(?:\s|_)(\d{1,4})\s*[-–—]\s*(\d{1,4})" + anime_tail,
        text,
    )
    if match:
        first, last = int(match.group(1)), int(match.group(2))
        if 0 < first <= last and last - first <= 1000 and not 1900 <= first <= 2099:
            return tuple(EpisodeLabel(episode=value) for value in range(first, last + 1))
    return None


def _anime_dash_single(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """Anime ``Show - 13 RAW.mkv``."""
    match = re.search(
        r"(?i)(?:\s|_)(?:-|–|—)(?:\s|_)(\d{1,4})(?:v\d+)?(?!\d)"
        r"(?=[\s_]*(?:raw\b|web(?:-dl)?\b|end\b|\[|\(|\{|\."
        r"(?:mkv|mp4|m4v|avi|mov|ts|ass|srt|vtt)\b|$))",
        text,
    )
    if match:
        value = int(match.group(1))
        return (EpisodeLabel(episode=value),) if value > 0 and not 1900 <= value <= 2099 else ()
    return None


def _dash_number_dash(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``Show - 13 - Title``."""
    match = re.search(
        r"(?i)(?:\s|_)-(?:\s|_)(\d{1,4})(?:v\d+)?(?:\s|_)-(?:\s|_)\S",
        text,
    )
    if match:
        value = int(match.group(1))
        return (EpisodeLabel(episode=value),) if value > 0 and not 1900 <= value <= 2099 else ()
    return None


def _number_before_release_tag(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``Show 13 [1080p]``."""
    match = re.search(r"(?i)\s(\d{1,4})(?:v\d+)?\s+\[(?:\d{3,4}p|web|bd|raw)\b", text)
    if match:
        value = int(match.group(1))
        return (EpisodeLabel(episode=value),) if value > 0 and not 1900 <= value <= 2099 else ()
    return None


def _leading_number_dot(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """``13. Title``."""
    match = re.match(r"(\d{1,4})\.\s+\S", text)
    if match:
        value = int(match.group(1))
        return (EpisodeLabel(episode=value),) if value > 0 and not 1900 <= value <= 2099 else ()
    return None


def _numeric_stem(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """A file named just ``13`` (not in an extras folder, not 1080 or a year)."""
    stem = PurePosixPath(text).stem
    if re.fullmatch(r"\d{1,4}", stem):
        value = int(stem)
        auxiliary = any(
            re.fullmatch(
                r"(?i)(?:extras?|bonus|bonuses|specials?|ova|ona|sp|nc|ncop|nced|"
                r"fonts?|subtitles?|samples?|featurettes?|scans?|бонусы?|"
                r"доп(?:олнительные)?[ ._-]*материалы)",
                part,
            )
            for part in path.parts[:-1]
        )
        if auxiliary or value in {720, 1080, 2160, 4320} or 1900 <= value <= 2099:
            return ()
        return (EpisodeLabel(episode=value),) if value > 0 else ()
    return None


def _bracketed_stem(text: str, path: PurePosixPath) -> tuple[EpisodeLabel, ...] | None:
    """A file named just ``[13]``."""
    stem = PurePosixPath(text).stem
    bracketed = re.fullmatch(r"\[(\d{1,4})\]", stem)
    if bracketed:
        value = int(bracketed.group(1))
        if value in {720, 1080, 2160, 4320} or 1900 <= value <= 2099:
            return ()
        return (EpisodeLabel(episode=value),) if value > 0 else ()
    return None


# Tried in order; the first parser that recognises the name decides (an empty
# tuple means "recognised, but not an episode").
_COVERAGE_PARSERS: tuple[Callable[[str, PurePosixPath], tuple[EpisodeLabel, ...] | None], ...] = (
    _fractional_special,
    _compact_sequence,
    _repeated_episodes,
    _season_episode_range,
    _season_dash_episode,
    _season_word_episode_word,
    _episode_word,
    _number_then_episode_word,
    _range_then_plural_word,
    _bare_e_number,
    _anime_dash_range,
    _anime_dash_single,
    _dash_number_dash,
    _number_before_release_tag,
    _leading_number_dot,
    _numeric_stem,
    _bracketed_stem,
)


def parse_episode_coverage(name: str) -> tuple[EpisodeLabel, ...]:
    """Parse episode coverage from a filename, including compact ranges."""
    path = PurePosixPath(str(name or "").replace("\\", "/"))
    text = path.name
    for parse in _COVERAGE_PARSERS:
        coverage = parse(text, path)
        if coverage is not None:
            return coverage
    return ()


def resolve_episode_coverages(names: Iterable[str]) -> tuple[tuple[EpisodeLabel, ...], ...]:
    """Parse a file set and collapse overlapping pseudo-ranges from dual numbering."""
    coverages = tuple(_coverage_with_folder_season(name) for name in names)
    nonempty = [coverage for coverage in coverages if coverage]
    if len(nonempty) == 1 and len(nonempty[0]) > 30:
        # With no neighbouring filenames the syntax is irreducibly ambiguous.
        # Prefer under-counting to selecting dozens of false episode members.
        return tuple((coverage[0],) if len(coverage) > 30 else coverage for coverage in coverages)
    first_keys = {coverage[0].key for coverage in coverages if coverage}
    coverages = tuple(
        (coverage[0],)
        if len(coverage) > 1 and sum(label.key in first_keys for label in coverage[1:]) >= 2
        else coverage
        for coverage in coverages
    )
    expanded = [coverage for coverage in coverages if len(coverage) > 1]
    if len(expanded) < 2:
        return coverages
    memberships = sum(len(coverage) for coverage in expanded)
    unique = {label.key for coverage in expanded for label in coverage}
    expanded_first_keys = {coverage[0].key for coverage in expanded}
    # Genuine split ranges are normally disjoint. Heavy overlap across files
    # means the second number is an absolute alias (for example E02-E074).
    overlap = memberships - len(unique)
    if (
        len(expanded_first_keys) == len(expanded)
        and unique
        and overlap >= min(len(coverage) for coverage in expanded) / 2
    ):
        return tuple((coverage[0],) if len(coverage) > 1 else coverage for coverage in coverages)
    return coverages


def _coverage_with_folder_season(name: str) -> tuple[EpisodeLabel, ...]:
    path = PurePosixPath(str(name or "").replace("\\", "/"))
    if any(
        re.fullmatch(
            r"(?i)(?:extras?|bonus|bonuses|specials?|ova|ona|sp|nc|ncop|nced|"
            r"samples?|featurettes?|scans?|бонусы?|"
            r"доп(?:олнительные)?[ ._-]*материалы)",
            part,
        )
        for part in path.parts[:-1]
    ):
        return ()
    if re.search(
        r"(?i)(?:^|[ ._\-\[(])(?:ova|oad|special|спец(?:выпуск)?|"
        r"ncop|nced|omake)(?:\b|\d)",
        path.stem,
    ):
        return ()
    coverage = parse_episode_coverage(str(path))
    # ONA can name a regular episodic web release, unlike an OVA bonus. Keep
    # unnumbered/seasonless ONA files conservative, but honour explicit SxxExx.
    if re.search(r"(?i)(?:^|[ ._\-\[(])ona(?:\b|\d)", path.stem) and (
        not coverage or any(label.season is None or label.season == 0 for label in coverage)
    ):
        return ()
    if not coverage or all(label.season is not None for label in coverage):
        return coverage
    seasons = {
        int(match.group(1))
        for part in path.parts[:-1]
        if (
            match := re.fullmatch(
                r"(?i)(?:season|сезон|s)[ ._-]*0*(\d{1,2})(?:\s*\(\d{4}\))?",
                part,
            )
        )
    }
    seasons.update(
        int(match.group(1))
        for part in path.parts[:-1]
        if (match := re.fullmatch(r"(?i)0*(\d{1,2})[ ._-]*(?:season|сезон)", part))
    )
    if len(seasons) != 1 or 0 in seasons:
        return coverage
    season = seasons.pop()
    return tuple(EpisodeLabel(label.episode, season) if label.season is None else label for label in coverage)


def is_video_file(name: str) -> bool:
    return PurePosixPath(str(name or "").replace("\\", "/")).suffix.casefold() in {
        ".mkv",
        ".mp4",
        ".m4v",
        ".avi",
        ".mov",
        ".ts",
        ".m2ts",
        ".webm",
        ".wmv",
        ".mpg",
        ".mpeg",
        ".flv",
        ".vob",
    }


def normalize_episode_seasons(
    coverages: Iterable[tuple[EpisodeLabel, ...]],
    preferred_season: int | None = None,
    *,
    names: Iterable[str] | None = None,
) -> tuple[tuple[EpisodeLabel, ...], ...]:
    rows = tuple(tuple(coverage) for coverage in coverages)
    if names is not None and preferred_season is None:
        paths = tuple(names)
        if len(paths) != len(rows):
            raise ValueError("episode coverage and path counts differ")
        video_seasons: dict[int, set[int]] = {}
        for path, coverage in zip(paths, rows, strict=True):
            if is_video_file(path):
                for label in coverage:
                    if label.season is not None:
                        video_seasons.setdefault(label.episode, set()).add(label.season)
        return tuple(
            tuple(
                EpisodeLabel(label.episode, next(iter(video_seasons[label.episode])))
                if label.season is None and not is_video_file(path) and len(video_seasons.get(label.episode, ())) == 1
                else label
                for label in coverage
            )
            for path, coverage in zip(paths, rows, strict=True)
        )
    explicit = {label.season for coverage in rows for label in coverage if label.season is not None}
    season = (
        preferred_season if preferred_season is not None else (next(iter(explicit)) if len(explicit) == 1 else None)
    )
    if season is None:
        return rows
    return tuple(
        tuple(
            EpisodeLabel(episode=label.episode, season=season) if label.season is None else label for label in coverage
        )
        for coverage in rows
    )


def item_identity(relative_path: str, label: EpisodeLabel | None, size: int | None = None) -> str:
    path = str(PurePosixPath(str(relative_path or "").replace("\\", "/"))).lower()
    suffix = f":{int(size)}" if size is not None else ""
    return f"file:{path}{suffix}"


def summarize_completion(items: Mapping[str, Mapping[str, Any]], expected: Mapping[str, Any] | None) -> dict[str, Any]:
    episode_groups: dict[str, list[bool]] = {}
    completed_files: set[str] = set()
    for key, item in items.items():
        if item.get("superseded"):
            continue
        episode_keys = item.get("episode_keys")
        if not isinstance(episode_keys, list) or not episode_keys:
            inferred = _progress_identity(key, item) if item.get("kind") != "file" else ""
            episode_key = item.get("episode_key") or (inferred if inferred.startswith("episode:") else None)
            episode_keys = [episode_key] if episode_key else []
        if episode_keys:
            for episode_key in episode_keys:
                episode_groups.setdefault(str(episode_key), []).append(item.get("status") == "completed")
        elif item.get("status") == "completed":
            completed_files.add(_progress_identity(key, item))
    completed_episodes = {key for key, statuses in episode_groups.items() if statuses and all(statuses)}
    expected_keys = {str(value) for value in (expected or {}).get("keys") or []}
    if expected_keys:
        completed_episodes.intersection_update(expected_keys)
    if expected and expected.get("source") == "title":
        minimum = int(expected.get("episode_min") or 1)
        maximum = int(expected.get("episode_max") or expected.get("total") or 0)
        season = expected.get("season")
        completed_episodes = {
            key
            for key in completed_episodes
            if (match := re.fullmatch(r"episode:(?:s(\d+))?e(\d+)", key))
            and minimum <= int(match.group(2)) <= maximum
            and (match.group(1) is None or int(match.group(1)) > 0)
            and (season is None or match.group(1) is None or int(match.group(1)) == int(season))
        }
    completed = (
        completed_episodes if (expected and expected.get("kind") == "episodes") or episode_groups else completed_files
    )
    total_raw = expected.get("total") if expected else None
    total = int(total_raw) if total_raw else None
    raw_completed = len(completed)
    inconsistent = bool(total is not None and raw_completed > total)
    final_total_known = bool(expected and expected.get("source") != "files")
    return {
        "completed": min(raw_completed, total) if total is not None else raw_completed,
        "expected": total,
        "completion_known": final_total_known,
        "is_complete": bool(final_total_known and total is not None and raw_completed == total),
        "completion_inconsistent": inconsistent,
    }


def _progress_identity(key: str, item: Mapping[str, Any]) -> str:
    """Return a stable progress key while retaining revision history elsewhere."""
    if item.get("kind") == "file":
        return str(item.get("identity") or key)
    if item.get("episode_key"):
        return str(item["episode_key"])
    label = parse_episode_label(str(item.get("label") or item.get("identity") or ""))
    if label is not None:
        return label.key
    return str(item.get("identity") or key)
