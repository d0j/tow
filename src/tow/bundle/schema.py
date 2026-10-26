"""What each member of a bundle may contain: config.yaml, state.json, the download history and
the secrets, parsed and checked before anything is written.

The state check is the bundle's own, stricter than what the store accepts when it loads
state.json; the two stay apart on purpose."""

from __future__ import annotations

import json
import math
from typing import Any

import yaml

from tow.bundle.errors import ExportImportError
from tow.config import SAFE_ID, ConfigError, validated, web_address
from tow.errors import Msg, TowError
from tow.records import REPLACE_PREVIOUS_CHOICES
from tow.store import StateVersionError, state_schema_version
from tow.yaml_guard import YamlLimitError, normalize_utf8, validate_graph
from tow.yaml_guard import load as load_yaml


def parse_mapping(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = load_yaml(data) if label == "config.yaml" else json.loads(data.decode("utf-8"))
    except YamlLimitError as exc:
        raise ExportImportError(f"invalid {label}: {exc}") from exc
    except (UnicodeError, ValueError, RecursionError, yaml.YAMLError) as exc:
        raise ExportImportError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise ExportImportError(f"{label} must be an object")
    return value


def utf8_config(data: bytes) -> bytes:
    try:
        return normalize_utf8(data)
    except YamlLimitError as exc:
        raise ExportImportError(f"invalid config.yaml: {exc}") from exc
    except UnicodeError as exc:
        raise ExportImportError("invalid config.yaml") from exc


def validate_tree(value: Any, *, label: str, depth: int = 0) -> None:
    if depth > 32:
        raise ExportImportError(f"{label} is nested too deeply")
    if isinstance(value, float) and not math.isfinite(value):
        raise ExportImportError(f"{label} contains a non-finite number")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            validate_tree(item, label=label, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExportImportError(f"{label} contains a non-string key")
            validate_tree(item, label=label, depth=depth + 1)
        return
    raise ExportImportError(f"{label} contains an unsupported value type")


def validate_field(mapping: dict[str, Any], key: str, expected: type | tuple[type, ...], *, label: str) -> None:
    if key not in mapping or mapping[key] is None:
        return
    value = mapping[key]
    if isinstance(value, bool) and (expected is int or (isinstance(expected, tuple) and int in expected)):
        raise ExportImportError(f"{label}.{key} has an invalid type")
    if not isinstance(value, expected):
        raise ExportImportError(f"{label}.{key} has an invalid type")


def validate_config_schema(data: dict[str, Any]) -> None:
    """config.yaml of a bundle: exactly the schema ``load_config`` applies (``tow.config.validated``).

    Site names follow the config's own rule too: an older name (a dot, Cyrillic, a YAML number
    such as 123) loads, so the restore point, export and import of that same config.yaml must
    not refuse it; a name that could move a page address (/, \\, . or ..) is refused by
    ``validated`` and every page encodes the others (/sites/<name>/...)."""
    try:
        validate_graph(data)
    except YamlLimitError as exc:
        raise ExportImportError(f"invalid config.yaml: {exc}") from exc
    trackers = data.get("trackers")
    # A site name may be any YAML scalar ``validated`` accepts; the settings under it are
    # checked as anywhere else.
    validate_tree(
        {**data, "trackers": list(trackers.values())} if isinstance(trackers, dict) else data, label="config.yaml"
    )
    try:
        validated(data)
    except ConfigError as exc:
        raise ExportImportError(  # the English text for the log, the owner's language on screen
            f"invalid {exc.text('en')}", owner_text=Msg("cli.bundle.import_refused", reason=exc)
        ) from None


def _has_exact_policy(value: Any) -> bool:
    """Include saved undo data, not just the currently visible topics."""
    if isinstance(value, dict):
        return value.get("mode") == "exact" or any(_has_exact_policy(item) for item in value.values())
    return isinstance(value, list) and any(_has_exact_policy(item) for item in value)


def validate_state_schema(data: dict[str, Any]) -> None:
    validate_tree(data, label="state.json")
    topics = data.get("topics")
    mirrors = data.get("mirrors")
    if not isinstance(topics, list) or not isinstance(mirrors, dict):
        raise ExportImportError("state.json has an unsupported schema")
    try:
        version = state_schema_version(data)  # a newer TOW's state is never imported into this one
    except StateVersionError as exc:
        raise ExportImportError(f"state.json: {exc}") from exc
    if version < 2 and _has_exact_policy(data):
        raise ExportImportError("state.json exact selections require schema 2")
    for index, topic in enumerate(topics):
        _validate_state_topic(index, topic)
    for name, mirror in mirrors.items():
        _validate_state_mirror(name, mirror)
    for key in ("health", "doctor"):
        if key in data and data[key] is not None and not isinstance(data[key], dict):
            raise ExportImportError(f"state.json.{key} has an invalid type")
    if data.get("undo") is not None and not isinstance(data["undo"], dict):
        raise ExportImportError("state.json.undo has an invalid type")


def _validate_state_topic(index: int, topic: Any) -> None:
    if not isinstance(topic, dict):
        raise ExportImportError(f"state.json topic {index} has an invalid type")
    label = f"state.json.topic[{index}]"
    from tow.topic_timers import interval_of

    try:
        interval_of(topic)
    except (TowError, ValueError) as exc:
        raise ExportImportError(f"{label}.check_interval_min is invalid") from exc
    for key in (
        "id",
        "title",
        "url",
        "save_path",
        "hash",
        "client_id",
        "tracker_title",
        "tracking_mode",
        "selection_hash",
        "content_ref",
        "content_token",
        "content_hash",
    ):
        validate_field(topic, key, str, label=label)
    validate_field(topic, "replace_previous", str, label=label)
    if topic.get("replace_previous") not in (None, *REPLACE_PREVIOUS_CHOICES):
        raise ExportImportError(f"{label}.replace_previous must be on, off or empty")
    if topic.get("url") and not web_address(topic["url"]):  # a javascript: link would be one click away
        raise ExportImportError(f"{label}.url must be an http:// or https:// address")
    if "id" in topic and not SAFE_ID.fullmatch(str(topic["id"])):  # part of /topics/<id>/… addresses
        raise ExportImportError(f"{label}.id may have only letters, digits, - and _ (at most 64)")
    for key in (
        "paused",
        "title_set",
        "once_done",
        "selection_dirty",
        "selection_verified",
        "selected_files_truncated",
        "error_notified",
    ):
        validate_field(topic, key, bool, label=label)
    for key in ("selected_file_count", "torrent_file_count"):
        validate_field(topic, key, int, label=label)
    for key in ("previous_hashes", "selected_files", "selected_episode_keys"):
        if key in topic and (not isinstance(topic[key], list) or not all(isinstance(item, str) for item in topic[key])):
            raise ExportImportError(f"{label}.{key} has an invalid type")
    selection = topic.get("selection")
    if "file_aliases" in topic:
        from tow.selection import normalize_file_aliases

        try:
            normalize_file_aliases(topic["file_aliases"])
        except ValueError as exc:
            raise ExportImportError(f"{label}.file_aliases has invalid metadata") from exc
    if selection is not None:
        if not isinstance(selection, dict):
            raise ExportImportError(f"{label}.selection has an invalid type")
        for key in ("mode", "value"):
            validate_field(selection, key, str, label=f"{label}.selection")
        if selection.get("mode") == "exact":
            from tow.selection import policy_from_topic

            try:
                policy_from_topic(topic)
            except ValueError as exc:
                raise ExportImportError(f"{label}.selection has an invalid exact file selection") from exc


def _validate_state_mirror(name: Any, mirror: Any) -> None:
    if not isinstance(name, str) or not isinstance(mirror, dict):
        raise ExportImportError("state.json mirror entry has an invalid type")
    if mirror.get("active") not in (None, "") and not web_address(mirror["active"]):
        raise ExportImportError(f"state.json mirror {name}.active must be an http:// or https:// address")
    if "frozen" in mirror and not isinstance(mirror["frozen"], bool):
        raise ExportImportError(f"state.json mirror {name}.frozen has an invalid type")
    for field in ("fail", "cool"):
        values = mirror.get(field, {})
        if not isinstance(values, dict):
            raise ExportImportError(f"state.json mirror {name}.{field} has an invalid type")
        for host, value in values.items():
            if (
                not isinstance(host, str)
                or not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (field == "fail" and (not isinstance(value, int) or value < 0))
            ):
                raise ExportImportError(f"state.json mirror {name}.{field} has an invalid value")


def validate_history_schema(data: dict[str, Any]) -> None:
    validate_tree(data, label="download_history.json")
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ExportImportError("download_history.json.schema_version has an invalid type")
    topics = data.get("topics")
    if not isinstance(topics, dict):
        raise ExportImportError("download_history.json.topics has an invalid type")
    for topic_id, record in topics.items():
        if not isinstance(topic_id, str) or not isinstance(record, dict):
            raise ExportImportError("download_history.json topic entry has an invalid type")
        for key in ("expected", "summary", "items", "last_completed", "last_event"):
            if key in record and record[key] is not None and not isinstance(record[key], dict):
                raise ExportImportError(f"download_history.json.{key} has an invalid type")


def validate_secrets_schema(data: dict[str, Any], *, label: str) -> None:
    validate_tree(data, label=label)
