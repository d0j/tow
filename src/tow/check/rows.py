"""A topic's result row in a check: its error, and the result stamped on the topic."""

from __future__ import annotations

from typing import Any

from tow import errors
from tow.clock import iso_now
from tow.errors import TowError
from tow.i18n import t
from tow.log import error_class, owner_language
from tow.records import Topic
from tow.store import StoreCorruptionError, load_state


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


def client_marks(topic: Topic) -> tuple[str, ...]:
    """What the client is changed with: the link (which torrent), the folder, the client and
    the file selection."""
    return tuple(repr(topic.get(key)) for key in ("url", "save_path", "client_id", "selection"))


def withdrawn_meanwhile(topic: Topic, row: dict[str, Any], old: str, started: tuple[str, ...] | None) -> bool:
    """The owner paused or deleted the topic, or changed its link, folder, client or file selection,
    after this run started (its copy is from the start): look at the state now, right before
    the client is changed, and skip it then - the next check works with what is saved."""
    try:
        current = next(
            (t for t in load_state(quarantine=False).get("topics") or [] if str(t.get("id")) == str(topic.get("id"))),
            None,
        )
    except StoreCorruptionError:
        return False  # the commit will fail closed on its own
    edited = current is not None and started is not None and client_marks(current) != started
    if current is not None and not current.get("paused") and not edited:
        return False
    key = (
        "check.deleted_meanwhile"
        if current is None
        else "check.paused_meanwhile"
        if current.get("paused")
        else "check.edited_meanwhile"
    )
    # Nothing was handed to the client: the topic keeps its last result (an error it had stays,
    # never a green "ok" or a "works again" message for a check that did not finish).
    row.update(
        {
            "ok": True,
            "hash": old,
            "changed": False,
            "status": "skipped",
            "skip": "withdrawn",
            "skipped": t(key, owner_language()),
        }
    )
    return True


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
