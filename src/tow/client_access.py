"""Finite negative connection observations, independent of torrent command permissions."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from tow.clients import factory
from tow.clients.address import base_url
from tow.clients.spec import get
from tow.clock import parse_timestamp
from tow.errors import Msg, TowError, value_record
from tow.http import retry_after_from
from tow.store import derive_local_secret

MAX_WAIT_SEC = 86400
AUTH_WAIT_SEC = 12 * 3600


def _causes(error: BaseException) -> list[Any]:
    queue: list[Any] = [error]
    found: list[Any] = []
    while queue and len(found) < 8:
        item = queue.pop(0)
        if any(item is previous for previous in found):
            continue
        found.append(item)
        record = value_record(item) if isinstance(item, Mapping) else None
        params = record.get("params") if record is not None else getattr(item, "params", None)
        reason = params.get("reason") if isinstance(params, Mapping) else None
        if isinstance(reason, (Mapping, Msg, TowError)):
            queue.append(reason)
        if isinstance(item, BaseException) and (cause := item.__cause__ or item.__context__) is not None:
            queue.append(cause)
    return found


def http_status(error: BaseException) -> int | None:
    for item in _causes(error):
        record = value_record(item) if isinstance(item, Mapping) else None
        code, params = (
            (record.get("code"), record.get("params"))
            if record is not None
            else (getattr(item, "code", None), getattr(item, "params", None))
        )
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


def temporary_http(error: BaseException) -> bool:
    status = http_status(error)
    return status is not None and (status in {408, 425, 429} or 500 <= status < 600)


def retry_after(error: BaseException, *, now: float) -> float:
    delay = 0.0
    for item in _causes(error):
        record = value_record(item) if isinstance(item, Mapping) else None
        params = record.get("params") if record is not None else getattr(item, "params", None)
        params = params if isinstance(params, Mapping) else {}
        for key in ("retry_at", "retry_after"):
            value = params.get(key, getattr(item, key, None))
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                delay = max(delay, min(MAX_WAIT_SEC, max(0, value - now if key == "retry_at" else value)))
        headers = getattr(getattr(item, "response", None), "headers", None)
        if callable(getattr(headers, "get", None)):
            delay = max(delay, retry_after_from(headers, now=now))
    return delay


class DeferredClientError(TowError):
    """An already observed refusal: suppressing a request must never renew its clock."""

    def __init__(self, code: int, *, retry_after: float = 0, observation_id: str = ""):
        super().__init__("client.managed.http_error", code=code, retry_after=retry_after)
        self.observation_id = observation_id


def endpoint(cfg: Mapping[str, Any], secrets: Mapping[str, Any], client_id: str | None = None) -> dict[str, str]:
    configuration = factory.client_configuration(dict(cfg), client_id)
    selected = str(configuration["id"])
    block = factory.client_secret_block(dict(cfg), dict(secrets), selected)
    kind = str(configuration.get("kind") or "")
    spec = get(kind)
    port = int(block.get("port") or (spec.default_port if spec is not None else 8080))
    address = urlsplit(base_url(str(block.get("host") or ""), port))
    endpoint = [
        kind,
        address.scheme.casefold(),
        str(address.hostname or ""),
        address.port or (443 if address.scheme.casefold() == "https" else 80),
        address.path.rstrip("/"),
    ]
    endpoint_id = hashlib.sha256(json.dumps(endpoint, sort_keys=True).encode()).hexdigest()
    return {"client": selected, "endpoint": endpoint_id}


def context(cfg: Mapping[str, Any], secrets: Mapping[str, Any], client_id: str | None = None) -> dict[str, str]:
    identity = endpoint(cfg, secrets, client_id)
    selected, endpoint_id = identity["client"], identity["endpoint"]
    block = factory.client_secret_block(dict(cfg), dict(secrets), selected)
    key = derive_local_secret("client-access-context").encode("ascii")
    credentials = {key: value for key, value in block.items() if key not in {"host", "port"}}
    authentication = json.dumps([endpoint_id, credentials], sort_keys=True, default=str).encode()
    return {
        "client": selected,
        "endpoint": endpoint_id,
        "auth": hmac.new(key, authentication, hashlib.sha256).hexdigest(),
    }


def _bucket(state: Mapping[str, Any], ctx: Mapping[str, str]) -> Mapping[str, Any]:
    records = state.get("client_access")
    bucket = records.get(ctx.get("client")) if isinstance(records, Mapping) else None
    return bucket if isinstance(bucket, Mapping) else {}


def _number(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def _identity(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def server_wait(state: Mapping[str, Any], ctx: Mapping[str, str], *, now: float) -> dict[str, Any]:
    floor = _bucket(state, ctx).get("floor")
    if not isinstance(floor, Mapping) or floor.get("endpoint") != ctx.get("endpoint") or not _identity(floor.get("id")):
        return {}
    observed, until = _number(floor.get("observed_at")), _number(floor.get("not_before"))
    code = floor.get("code")
    if (
        observed is None
        or until is None
        or not observed <= now < until <= observed + MAX_WAIT_SEC
        or type(code) is not int
        or not (code in {408, 425, 429} or 500 <= code < 600)
    ):
        return {}
    return dict(floor)


def auth_refusal(state: Mapping[str, Any], ctx: Mapping[str, str], *, now: float) -> dict[str, Any]:
    refusal = _bucket(state, ctx).get("auth")
    if (
        not isinstance(refusal, Mapping)
        or refusal.get("context") != ctx.get("auth")
        or not _identity(refusal.get("id"))
    ):
        return {}
    observed, until = _number(refusal.get("observed_at")), _number(refusal.get("until"))
    if observed is None or until is None or not observed <= now < until <= observed + AUTH_WAIT_SEC:
        return {}
    return dict(refusal) if type(refusal.get("code")) is int and refusal.get("code") in {401, 403} else {}


def capture(state: Mapping[str, Any], ctx: Mapping[str, str]) -> str:
    refusal = _bucket(state, ctx).get("auth")
    return (
        str(refusal.get("id") or "")
        if isinstance(refusal, Mapping) and refusal.get("context") == ctx.get("auth") and _identity(refusal.get("id"))
        else ""
    )


def receipt_auth_pending(waiting: Mapping[str, Any], *, now: float) -> bool:
    params = waiting.get("blocked_params")
    code = params.get("code") if isinstance(params, Mapping) else None
    if waiting.get("blocked") != "client.managed.http_error" or type(code) is not int or code not in {401, 403}:
        return False
    try:
        since = (
            float(waiting["requested_at"])
            if "requested_at" in waiting
            else parse_timestamp(str(waiting.get("since") or "")).timestamp()
        )
        until = float(waiting.get("deadline", since + AUTH_WAIT_SEC))
    except TypeError, ValueError, OverflowError:
        return False
    return math.isfinite(since) and math.isfinite(until) and since <= now < min(until, since + AUTH_WAIT_SEC)


def acknowledge(state: dict[str, Any], ctx: Mapping[str, str], expected_id: str) -> bool:
    if capture(state, ctx) != expected_id:
        return False
    if not expected_id:
        return True  # no matching auth observation: preserve the caller's exact state
    bucket = dict(_bucket(state, ctx))
    bucket.pop("auth", None)
    records = state.get("client_access")
    if not isinstance(records, dict):
        return False
    if bucket:
        records[ctx["client"]] = bucket
    else:
        records.pop(ctx["client"], None)
    return True


def credentials_changed(state: dict[str, Any], old_context: Mapping[str, str], new_context: Mapping[str, str]) -> None:
    if old_context.get("client") == new_context.get("client") and old_context.get("auth") != new_context.get("auth"):
        acknowledge(state, old_context, capture(state, old_context))


def observe(state: dict[str, Any], ctx: Mapping[str, str], error: Exception, *, now: float) -> str:
    """Only a new actual HTTP answer may create or lengthen an observation."""
    if isinstance(error, DeferredClientError):
        return error.observation_id
    status = http_status(error)
    if status is None:
        return ""
    bucket = dict(_bucket(state, ctx))
    entry: dict[str, Any] = {"id": uuid4().hex, "code": status, "observed_at": now}
    if status in {408, 425, 429} or 500 <= status < 600:
        until = now + max(60, retry_after(error, now=now))
        previous = server_wait(state, ctx, now=now)
        if previous and previous["not_before"] >= until:
            return str(previous["id"])  # a shorter later response cannot shorten the first one
        entry.update(endpoint=ctx["endpoint"], not_before=until)
        bucket["floor"] = entry
    elif status in {401, 403}:
        entry.update(context=ctx["auth"], until=now + AUTH_WAIT_SEC)
        bucket["auth"] = entry
    else:
        return ""
    records = state.setdefault("client_access", {})
    if not isinstance(records, dict):
        records = state["client_access"] = {}
    records[ctx["client"]] = bucket
    return str(entry["id"])
