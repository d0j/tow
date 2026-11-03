"""Typed native-client refusals and saved read-only retry floors; never command authority."""

from __future__ import annotations

import copy
import math
from collections.abc import Iterator, Mapping
from typing import Any

from tow import client_access
from tow.clients import factory
from tow.clock import machine_now, parse_timestamp
from tow.errors import TowError
from tow.store import load_state, persistence_lock, save_state


def http_status(error: BaseException) -> int | None:
    return client_access.http_status(error)


def permanent_http(error: BaseException) -> TowError | None:
    return client_access.permanent_http(error)


def retry_after(error: BaseException, *, now: float) -> float:
    return client_access.retry_after(error, now=now)


def record_context(
    state: dict[str, Any],
    cfg: dict[str, Any],
    secrets: dict[str, Any],
    client_id: str,
    error: Exception,
    *,
    now: float,
    apply: bool,
) -> str:
    """Persist an actual refusal against the settings which made that request, by CAS."""
    if isinstance(error, client_access.DeferredClientError):
        return error.observation_id
    if not apply or http_status(error) is None:
        return ""
    from tow.check.run import fresh_client_settings

    ctx = client_access.context(cfg, secrets, client_id)
    with persistence_lock():
        fresh_cfg, fresh_secrets = fresh_client_settings()
        try:
            fresh_ctx = client_access.context(fresh_cfg, fresh_secrets, client_id)
        except factory.ClientSettingError, factory.ClientUnavailableError, AttributeError, TypeError, ValueError:
            return ""
        if (
            fresh_ctx["endpoint"] != ctx["endpoint"]
            or fresh_ctx["client"] != ctx["client"]
            or (not client_access.temporary_http(error) and fresh_ctx["auth"] != ctx["auth"])
        ):
            return ""  # an old in-flight failure says nothing about the owner's new credentials
        disk = load_state(quarantine=False)
        before = copy.deepcopy(disk.get("client_access"))
        ident = client_access.observe(disk, ctx, error, now=now)
        if before != disk.get("client_access"):
            save_state(disk)
        state["client_access"] = copy.deepcopy(disk.get("client_access") or {})
        return ident


def note_failure(run: Any, client_id: str, error: Exception) -> str:
    cached = getattr(run, "client_errors", {}).get(client_id)
    if isinstance(cached, client_access.DeferredClientError):
        return cached.observation_id
    cfg = getattr(run, "client_config", None)
    if not cfg:
        return ""
    return record_context(run.state, cfg, run.secrets, client_id, error, now=machine_now().timestamp(), apply=run.apply)


def remember(waiting: dict[str, Any], error: Exception, *, now: float, observation_id: str = "") -> Exception:
    """Keep a server refusal on the existing receipt, without renewing its transaction."""
    status = http_status(error)
    if status is None:
        return error
    if (permanent := permanent_http(error)) is not None:
        waiting.update(blocked=permanent.code, blocked_params=permanent.params)
        if observation_id:
            waiting["http_refusal_id"] = observation_id
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
    topic: Mapping[str, Any],
    *,
    now: float,
    manual: bool = False,
    cfg: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
    secrets: Mapping[str, Any] | None = None,
) -> bool:
    """A current refusal excludes automatic native reads, including progress reconciliation."""
    for waiting in _receipts(topic, cfg):
        floor = waiting.get("not_before")
        if isinstance(floor, (int, float)) and not isinstance(floor, bool) and math.isfinite(floor) and now < floor:
            return True
        params = waiting.get("blocked_params")
        if not manual and waiting.get("blocked") == "client.managed.http_error" and isinstance(params, Mapping):
            status = params.get("code")
            if (
                type(status) is int
                and status in {401, 403}
                and state is not None
                and cfg is not None
                and secrets is not None
            ):
                records = state.get("client_access")
                bucket = records.get(str(waiting["client"])) if isinstance(records, Mapping) else None
                if not isinstance(bucket, Mapping) or not isinstance(bucket.get("auth"), Mapping):
                    continue
                try:
                    ctx = client_access.context(cfg, secrets, str(waiting["client"]))
                except factory.ClientUnavailableError, AttributeError, ValueError, TypeError, KeyError, OverflowError:
                    continue
                refusal = client_access.auth_refusal(state, ctx, now=now)
                try:
                    until = (
                        float(waiting["deadline"])
                        if "deadline" in waiting
                        else (
                            parse_timestamp(str(waiting.get("since") or "")).timestamp() + client_access.AUTH_WAIT_SEC
                        )
                    )
                except TypeError, ValueError, OverflowError:
                    until = 0
                if now < until and refusal:
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
    state: Mapping[str, Any],
    cfg: Mapping[str, Any],
    *,
    now: float,
    manual: bool = False,
    secrets: Mapping[str, Any] | None = None,
) -> dict[str, Exception]:
    """A current HTTP refusal covers that connection's ping and its other topics too."""
    held: dict[str, Exception] = {}
    records = state.get("client_access")
    if secrets is None or not isinstance(records, Mapping) or not records:
        return held
    for configuration in factory.client_configurations(dict(cfg)):
        if not configuration.get("enabled", True):
            continue
        ident = str(configuration["id"])
        try:
            ctx = client_access.endpoint(cfg, secrets, ident)
            floor = client_access.server_wait(state, ctx, now=now)
        except factory.ClientUnavailableError, AttributeError, ValueError, TypeError, KeyError, OverflowError:
            continue
        if floor:
            held[ident] = client_access.DeferredClientError(
                floor["code"], retry_after=floor["not_before"] - now, observation_id=str(floor["id"])
            )
    return held
