"""A topic's result row in a check: its error, and the result stamped on the topic."""

from __future__ import annotations

from typing import Any

from tow import errors
from tow.clock import iso_now
from tow.errors import TowError
from tow.log import error_class
from tow.records import Topic


def set_error(row: dict[str, Any], error: BaseException | str) -> None:
    """A failed row keeps the error's text (the language of the moment), its record (code and
    values, rendered again in the reader's language) and its class."""
    row["error"] = str(error)
    row["error_record"] = errors.record_of(error) if isinstance(error, BaseException) else None
    row["error_class"] = error_class(error)


def row_error(row: dict[str, Any]) -> Any:
    """The row's error as a value a message can carry (the typed error's record, else its text)."""
    return errors.as_value(row.get("error_record"), str(row.get("error") or ""))


def now() -> str:
    """When a check ran, as stored: an ISO time with its offset. The pages write it in their own
    language when they show it (TOW 1.24 and before stored it already written out)."""
    return iso_now()


def fail_row(topic: Topic, row: dict[str, Any], error: TowError) -> None:
    row.update({"ok": False, "status": "failed"})
    set_error(row, error)
    stamp_result(topic, row)


def stamp_result(topic: Topic, row: dict[str, Any]) -> None:
    """The check's result on the topic: ``last_error`` keeps the text (older TOW versions read
    it), ``last_error_code``/``_params`` the typed error Home and History render in the reader's
    language, ``last_error_class`` its status class."""
    topic["last_ok"] = bool(row.get("ok"))
    topic["last_error"] = row.get("error")
    record = row.get("error_record") if row.get("error") else None
    if isinstance(record, dict):
        topic["last_error_code"] = record["code"]
        topic["last_error_params"] = record.get("params") or {}
    else:
        topic.pop("last_error_code", None)
        topic.pop("last_error_params", None)
    topic["last_error_class"] = (
        str(row.get("error_class") or error_class(str(row.get("error") or ""))) if row.get("error") else ""
    )
    topic["last_check"] = now()
    if row.get("ok") and row.get("status") != "skipped":
        topic["last_ok_at"] = topic["last_check"]  # G3: when it last really worked
    if row.get("site_answered"):
        topic["site_ok_at"] = topic["last_check"]  # the site's part worked, the client's may not
    topic["last_changed"] = bool(row.get("ok") and row.get("changed"))
