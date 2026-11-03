"""Typed native-client refusals and saved read-only retry floors; never command authority."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from typing import Any

from tow.clients import factory
from tow.errors import Msg, TowError
from tow.http import retry_after_from


def _causes(error: BaseException) -> list[Any]:
    queue: list[Any] = [error]
    found: list[Any] = []
    while queue and len(found) < 8:
        item = queue.pop(0)
        if any(item is previous for previous in found):
            continue
        found.append(item)
        params = item.get("params") if isinstance(item, Mapping) else getattr(item, "params", None)
        reason = params.get("reason") if isinstance(params, Mapping) else None
        if isinstance(reason, (Mapping, Msg, TowError)):
            queue.append(reason)
        if isinstance(item, BaseException) and (cause := item.__cause__ or item.__context__) is not None:
            queue.append(cause)
    return found


def http_status(error: BaseException) -> int | None:
    for item in _causes(error):
        if isinstance(item, Mapping):
            code, params = item.get("code"), item.get("params")
        else:
            code, params = getattr(item, "code", None), getattr(item, "params", None)
        status = params.get("code") if code == "client.managed.http_error" and isinstance(params, Mapping) else None
        if status is None:
            status = getattr(item, "http_status_code", None)
        if status is None:
            status = getattr(getattr(item, "response", None), "status_code", None)
        if type(status) is int and 100 <= status < 600:
            return status
    return None


def permanent_http(error: BaseException) -> TowError | None:
    status = http_status(error)
    return (
        TowError("client.managed.http_error", code=status)
        if (status is not None and 400 <= status < 500 and status not in {408, 425, 429})
        else None
    )


def retry_after(error: BaseException, *, now: float) -> float:
    delay = 0.0
    for item in _causes(error):
        params = item.get("params") if isinstance(item, Mapping) else getattr(item, "params", None)
        params = params if isinstance(params, Mapping) else {}
        for key in ("retry_at", "retry_after"):
            value = params.get(key, getattr(item, key, None))
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                delay = max(delay, min(86400, max(0, value - now if key == "retry_at" else value)))
        headers = getattr(getattr(item, "response", None), "headers", None)
        if callable(getattr(headers, "get", None)):
            delay = max(delay, retry_after_from(headers, now=now))
    return delay


def remember(waiting: dict[str, Any], error: Exception, *, now: float) -> Exception:
    """Keep a server refusal on the existing receipt, without renewing its transaction."""
    status = http_status(error)
    if status is None:
        return error
    if (permanent := permanent_http(error)) is not None:
        waiting.update(blocked=permanent.code, blocked_params=permanent.params)
        return permanent
    if status in {408, 425, 429} or status >= 500:
        delay = retry_after(error, now=now)
        waiting["not_before"] = now + max(60, delay)
        waiting["retry_status"] = status
    return error


def clear_retry(waiting: dict[str, Any]) -> None:
    waiting.pop("not_before", None)
    waiting.pop("retry_status", None)


def _current(topic: Mapping[str, Any], waiting: Mapping[str, Any], cfg: Mapping[str, Any] | None) -> bool:
    try:
        selected = str(
            topic.get("client_id")
            or (factory.default_client_id(dict(cfg)) if cfg is not None else waiting.get("client") or "")
        )
        return bool(
            waiting.get("client") == selected
            and str(waiting.get("hash") or "").upper() == str(topic.get("hash") or "").upper()
            and not topic.get("selection_dirty")
            and (
                "client_generation" not in waiting
                or waiting["client_generation"] == topic.get("file_observation_revision")
            )
            and ("path" not in waiting or "owner" not in waiting or waiting["path"] == topic.get("save_path"))
            and (
                "owner" not in waiting
                or waiting["owner"] == [repr(topic.get(key)) for key in ("url", "save_path", "client_id", "selection")]
            )
        )
    except factory.ClientUnavailableError, AttributeError, TypeError, ValueError:
        return False


def deferred(
    topic: Mapping[str, Any], *, now: float, manual: bool = False, cfg: Mapping[str, Any] | None = None
) -> bool:
    """A current refusal excludes automatic native reads, including progress reconciliation."""
    for waiting in _receipts(topic, cfg):
        floor = waiting.get("not_before")
        if isinstance(floor, (int, float)) and not isinstance(floor, bool) and math.isfinite(floor) and now < floor:
            return True
        params = waiting.get("blocked_params")
        if not manual and waiting.get("blocked") == "client.managed.http_error" and isinstance(params, Mapping):
            status = params.get("code")
            if type(status) is int and 400 <= status < 500 and status not in {408, 425, 429}:
                return True
    return False


def _receipts(topic: Mapping[str, Any], cfg: Mapping[str, Any] | None) -> Iterator[Mapping[str, Any]]:
    for key in ("verification_pending", "waiting_space", "repair_pending"):
        waiting = topic.get(key)
        if not isinstance(waiting, Mapping) or not _current(topic, waiting, cfg):
            continue
        if key == "repair_pending":
            from tow import file_repair

            receipt = file_repair.receipt_of(topic)
            if not receipt or not file_repair.current(topic, receipt, cfg):
                continue
            retry = receipt.get("retry_error") or {}
            params = retry.get("params") or {}
            waiting = {
                **receipt,
                "not_before": receipt["next_at"] if retry.get("code") == "repair.waiting_client" else 0,
                "retry_status": params.get("code"),
            }
        yield waiting


def deferred_clients(
    state: Mapping[str, Any], cfg: Mapping[str, Any], *, now: float, manual: bool = False
) -> dict[str, Exception]:
    """A current HTTP refusal covers that connection's ping and its other topics too."""
    held: dict[str, Exception] = {}
    for topic in state.get("topics") or []:
        if not isinstance(topic, Mapping) or not deferred(topic, now=now, manual=manual, cfg=cfg):
            continue
        for waiting in _receipts(topic, cfg):
            status = waiting.get("retry_status")
            params = waiting.get("blocked_params")
            if waiting.get("blocked") == "client.managed.http_error" and isinstance(params, Mapping):
                status = params.get("code")
            if type(status) is int:
                floor = waiting.get("not_before")
                delay = max(0.0, float(floor) - now) if isinstance(floor, (int, float)) and math.isfinite(floor) else 0
                held[str(waiting["client"])] = TowError("client.managed.http_error", code=status, retry_after=delay)
    return held
