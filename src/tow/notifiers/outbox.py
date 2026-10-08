"""The bounded delivery queue: messages are stored first and delivered at least once.

``state["notify_outbox"][target]`` holds, per recipient (a Telegram chat, a Discord webhook…):

- ``items``: messages not delivered yet, oldest first, each with an id;
- ``inflight``: the message being delivered (several queued items are combined into one) and how
  many of its parts already arrived - a long message that broke after part 2 resumes at part 3;
- ``blocked``: settings fingerprint after a permanent error (401/403/404: wrong token, deleted
  webhook). The queue waits until the owner changes the settings, instead of failing every
  10 minutes. 429/5xx and network errors are retried later; any other refusal (a 4xx about
  the message itself) drops just that message, with the reason in the log, and the queue goes on
  (``one_by_one`` while it looks for the refused message among combined ones).

Only one dispatcher delivers to a recipient at a time, so the watchdog and a running check -
other processes or other threads of the same one - never send the same message twice: a
per-recipient lock inside the process, and across processes ``notify_lease``, a claim with its
own ``owner`` (process, thread and a random part; TOW 1.28 and older called it ``token``, which
restore points took for a credential). Every progress write checks the owner: a
dispatcher whose lease was taken over stops instead of writing over the new owner. Items are
removed by id. Delivery is at least once: a TOW stopped after a messenger's answer but before
the queue's write sends that part again (no messenger here offers a way to deduplicate it).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from tow.errors import Msg
from tow.i18n import t
from tow.notifiers.base import DeliveryError, Notifier, message_language
from tow.notifiers.registry import fingerprint, targets, title
from tow.records import NotifyStatus

OUTBOX_LIMIT = 50
LEASE_SEC = 120
MAX_ROUNDS = 5
# How long a second thread of this process waits for the one delivering to the same recipient
# (then it reports "busy", like a lease held by another process).
LOCAL_WAIT_SEC = 30.0

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


def _local_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _new_token() -> str:
    """This claim's identity: the process, the thread and a random part (two claims of one
    thread differ too)."""
    return f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex[:12]}"


def _lease_owner(lease: dict[str, Any]) -> Any:
    """The claim that holds ``lease``: ``owner``, or ``token`` as TOW 1.28 and older wrote it."""
    return lease.get("owner", lease.get("token"))


def _lease_held_by_other(lease: dict[str, Any], token: str, now: float) -> bool:
    if not lease or float(lease.get("until") or 0) <= now:
        return False
    held = _lease_owner(lease)
    if held is None:  # written by TOW 1.20 or older: only the process was recorded
        return bool(lease.get("pid") != os.getpid())
    return bool(held != token)


def _sleep(seconds: float) -> None:
    from tow.notifiers import base

    base.backoff_sleep(seconds)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _fit(line: str, size: int, measure: Callable[[str], int]) -> int:
    """How many characters of ``line`` fit in ``size`` (at least one, so the split always advances)."""
    if measure is len:
        return max(1, size)
    low, high = 1, len(line)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(line[:middle]) <= size:
            low = middle
        else:
            high = middle - 1
    return low


def chunks(text: str, size: int, *, measure: Callable[[str], int] = len) -> list[str]:
    """Split on line breaks where possible so no messenger cuts a message silently.

    ``measure`` is how the messenger counts: characters (default) or UTF-8 bytes (ntfy).
    """
    if measure(text) <= size:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while measure(line) > size:
            if current:
                parts.append(current)
                current = ""
            cut = _fit(line, size, measure)
            parts.append(line[:cut])
            line = line[cut:]
        candidate = f"{current}\n{line}" if current else line
        if measure(candidate) > size:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def parts_for(module: Notifier, text: str) -> list[str]:
    """``text`` split the way ``module`` needs it: MAX_BYTES (UTF-8) or MAX_UTF16 (UTF-16 code
    units) when it has one, else MAX_LEN characters."""
    max_bytes = getattr(module, "MAX_BYTES", None)
    if isinstance(max_bytes, int):
        return chunks(text, max_bytes, measure=_utf8_len)
    max_utf16 = getattr(module, "MAX_UTF16", None)
    if isinstance(max_utf16, int):
        return chunks(text, max_utf16, measure=_utf16_len)
    return chunks(text, int(getattr(module, "MAX_LEN", 2000)))


def _box(state: dict[str, Any], key: str) -> dict[str, Any]:
    outbox = state.setdefault("notify_outbox", {})
    box = outbox.get(key)
    if isinstance(box, list):  # 1.11-1.13: a plain list of texts per messenger
        box = {"items": [{"id": uuid.uuid4().hex[:12], "text": str(text)} for text in box], "retry": True}
    if not isinstance(box, dict):
        box = {}
    box.setdefault("items", [])
    outbox[key] = box
    return box


def _tidy(state: dict[str, Any]) -> None:
    outbox = state.get("notify_outbox") or {}
    for key in [
        k
        for k, box in outbox.items()
        if isinstance(box, dict) and not box.get("items") and not box.get("inflight") and not box.get("dropped")
    ]:
        outbox.pop(key)
    if not outbox:
        state.pop("notify_outbox", None)
    leases = state.get("notify_lease") or {}
    if not leases:
        state.pop("notify_lease", None)


def enqueue(state: dict[str, Any], secrets: dict[str, Any], text: str) -> list[str]:
    """Add ``text`` for every connected recipient (in ``state``; the caller saves it)."""
    keys = []
    for key, _kind, _module, _settings in targets(secrets):
        box = _box(state, key)
        box["items"].append({"id": uuid.uuid4().hex[:12], "text": text})
        overflow = len(box["items"]) - OUTBOX_LIMIT
        if overflow > 0:
            box["items"] = box["items"][overflow:]
            box["dropped"] = int(box.get("dropped") or 0) + overflow
            totals = state.setdefault("notify_dropped", {})
            totals[key] = int(totals.get(key) or 0) + overflow
        keys.append(key)
    return keys


def _combined(items: list[dict[str, Any]], dropped: int, *, retry: bool) -> str:
    """One message for what is queued; a repeat attempt or a backlog says it is late."""
    texts = [str(item["text"]) for item in items]
    if len(texts) == 1 and not retry and not dropped:
        return texts[0]
    lang = message_language()
    note = t("notifier.common.dropped", lang, n=dropped) + "\n\n" if dropped else ""
    if len(texts) == 1 and not retry:
        return note + texts[0]
    return t("notifier.common.late", lang) + "\n\n" + note + "\n\n".join(texts)


def set_status(state: dict[str, Any], key: str, reason: str | Msg | None) -> None:
    """The recipient's last delivery: ok, or why not - its text (the message language) and,
    for TOW's own wording, its key and values (the settings page shows it in the reader's)."""
    status = NotifyStatus(ok=reason is None, at=int(time.time()))
    if isinstance(reason, Msg):
        record = reason.record()
        status["error"] = reason.text(message_language())
        status["error_code"] = record["code"]
        status["error_params"] = record["params"]
    else:
        status["error"] = reason or ""
    state.setdefault("notify_status", {})[key] = status


def _lease(token: str, until: float) -> dict[str, Any]:
    # Not "token": a restore point refuses a field of that name outside the encrypted secrets.
    return {"pid": os.getpid(), "owner": token, "until": until}


def _claim(key: str, settings: dict[str, Any], token: str) -> tuple[str, dict[str, Any] | None]:
    """Take the lease and the message to send: ("send", inflight) / ("idle"|"busy"|"blocked", None)."""
    from tow.store import load_state, persistence_lock, save_state

    with persistence_lock():
        state = load_state()
        box = _box(state, key)
        lease = (state.get("notify_lease") or {}).get(key) or {}
        now = time.time()
        if _lease_held_by_other(lease, token, now):
            return "busy", None
        if box.get("blocked") and box["blocked"] == fingerprint(settings):
            return "blocked", None
        box.pop("blocked", None)
        inflight = box.get("inflight")
        if inflight and int(inflight.get("done") or 0) == 0:
            # Nothing of it arrived yet: rebuild it with whatever was queued meanwhile.
            box.pop("inflight")
            box["retry"] = True
        if not box.get("inflight"):
            if not box["items"]:
                box.pop("retry", None)
                box.pop("one_by_one", None)
                _tidy(state)
                save_state(state)
                return "idle", None
            # After a refused combined message, find the one the messenger refuses: one at a time.
            batch = box["items"][:1] if box.get("one_by_one") else box["items"]
            box["inflight"] = {
                "ids": [item["id"] for item in batch],
                "text": _combined(batch, int(box.pop("dropped", 0) or 0), retry=bool(box.pop("retry", False))),
                "done": 0,
            }
        state.setdefault("notify_lease", {})[key] = _lease(token, now + LEASE_SEC)
        save_state(state)
        return "send", dict(box["inflight"])


def _progress(
    key: str,
    token: str,
    *,
    done: int | None = None,
    finished: bool = False,
    reason: str | Msg | None = None,
    block_fp: str | None = None,
    refused: bool = False,
) -> bool:
    """Record what arrived; False (nothing written) when the lease is no longer this claim's -
    it expired and another dispatcher took the queue over: the caller stops sending."""
    from tow.store import load_state, persistence_lock, save_state

    with persistence_lock():
        state = load_state()
        lease = (state.get("notify_lease") or {}).get(key) or {}
        if _lease_owner(lease) != token:
            return False
        box = _box(state, key)
        inflight = box.get("inflight") or {}
        if refused:
            # The messenger refused this message: a single one is dropped (the reason stays in
            # the status); a combined one is retried one by one to find the message it refuses.
            ids = set(inflight.get("ids") or [])
            if len(ids) == 1:
                box["items"] = [item for item in box["items"] if item.get("id") not in ids]
            else:
                box["one_by_one"] = True
            box.pop("inflight", None)
            if reason is not None:
                set_status(state, key, reason)
                reason = None  # the lease is released below like for a finished message
            finished = True
        elif finished:
            sent = set(inflight.get("ids") or [])
            box["items"] = [item for item in box["items"] if item.get("id") not in sent]
            box.pop("inflight", None)
            set_status(state, key, None)
        elif done is not None and inflight:
            inflight["done"] = done
        if reason is not None:
            set_status(state, key, reason)
            if block_fp:
                box["blocked"] = block_fp
        leases = state.setdefault("notify_lease", {})
        if finished or reason is not None:
            leases.pop(key, None)
        else:
            leases[key] = _lease(token, time.time() + LEASE_SEC)
        _tidy(state)
        save_state(state)
        return True


def _busy(module: Notifier) -> tuple[bool, str]:
    lang = message_language()
    return False, t("notifier.common.busy_elsewhere", lang, title=title(module, lang))


def _deliver_target(key: str, module: Notifier, settings: dict[str, Any]) -> tuple[bool, str] | None:
    """Send what is queued for one recipient; None when there was nothing to do."""
    lock = _local_lock(key)
    if not lock.acquire(timeout=LOCAL_WAIT_SEC):
        return _busy(module)
    try:
        return _deliver_locked(key, module, settings, _new_token())
    finally:
        lock.release()


def _deliver_locked(key: str, module: Notifier, settings: dict[str, Any], token: str) -> tuple[bool, str] | None:
    result: tuple[bool, str] | None = None
    for _round in range(MAX_ROUNDS):  # messages queued meanwhile go out in the same pass
        claim, inflight = _claim(key, settings, token)
        if claim == "idle":
            return result
        if claim == "busy":
            return result or _busy(module)
        if claim == "blocked":
            from tow.store import load_state

            status = (load_state().get("notify_status") or {}).get(key) or {}
            if status.get("error"):
                return False, str(status["error"])
            lang = message_language()
            return False, t("notifier.common.settings_error", lang, title=title(module, lang))
        assert inflight is not None
        outcome, reason = _send_inflight(key, module, settings, token, inflight)
        if outcome == "refused":
            result = (False, reason)
            continue  # the rest of the queue goes on
        if outcome == "failed":
            return False, reason
        if outcome == "lost":
            return result or _busy(module)
        result = (True, "")
    return result


def _send_inflight(
    key: str, module: Notifier, settings: dict[str, Any], token: str, inflight: dict[str, Any]
) -> tuple[str, str]:
    """Send the parts not delivered yet: ("sent" | "refused" | "failed" | "lost", reason).

    "lost": the lease is no longer this claim's (another dispatcher took over): stop at once.
    """
    parts = parts_for(module, str(inflight["text"]))
    pause = float(getattr(module, "CHUNK_PAUSE", 0.0))
    for index in range(int(inflight.get("done") or 0), len(parts)):
        try:
            module.send(settings, parts[index])
        except DeliveryError as exc:
            if exc.drop:
                if _progress(key, token, done=index, reason=exc.detail, refused=True):
                    _log_refused(key, exc.reason, len(inflight.get("ids") or []))
                return "refused", exc.reason
            permanent = not exc.transient
            _progress(key, token, done=index, reason=exc.detail, block_fp=fingerprint(settings) if permanent else None)
            return "failed", exc.reason
        except Exception as exc:  # noqa: BLE001 - a bug in one messenger must not stop the others; recorded
            lang = message_language()
            failure = Msg("notifier.common.internal_error", title=title(module, lang), error=type(exc).__name__)
            _progress(key, token, done=index, reason=failure)
            return "failed", failure.text(lang)
        if index + 1 < len(parts):
            if not _progress(key, token, done=index + 1):
                return "lost", ""
            if pause:
                _sleep(pause)
    # Delivered. When the lease was taken over meanwhile nothing is written: the next claim
    # finds the queue busy and this pass ends with what it delivered.
    _progress(key, token, finished=True)
    return "sent", ""


def _log_refused(key: str, reason: str, count: int) -> None:
    """A message the messenger refused is never silently gone: it is in the log with the reason."""
    from tow.log import log_event

    log_event(
        "bot_delivery_failed",
        component="notification",
        integration_id=key.split(":", 1)[0],
        status="dropped" if count == 1 else "retry_one_by_one",
        reason="refused",
        error=reason,
        how="auto",
    )


def flush(secrets: dict[str, Any], *, only: set[str] | None = None) -> dict[str, tuple[bool, str]]:
    """Deliver what is queued; {target: (delivered, reason)} for targets that had something."""
    results: dict[str, tuple[bool, str]] = {}
    for key, _kind, module, settings in targets(secrets):
        if only is not None and key not in only:
            continue
        outcome = _deliver_target(key, module, settings)
        if outcome is not None:
            results[key] = outcome
    return results


def unblock(kind: str) -> None:
    """After a successful check of the settings, a waiting queue may go again."""
    from tow.store import load_state, persistence_lock, save_state

    with persistence_lock():
        state = load_state()
        changed = False
        for key, box in (state.get("notify_outbox") or {}).items():
            if (key == kind or key.startswith(kind + ":")) and isinstance(box, dict) and box.pop("blocked", None):
                changed = True
        if changed:
            save_state(state)
