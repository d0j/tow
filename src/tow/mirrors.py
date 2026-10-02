from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from tow import http as thttp
from tow.errors import Msg, TowError
from tow.store import load_state, persistence_lock, save_state
from tow.torrent import is_download_limit


def _now() -> float:
    return time.time()


# What a failure says about the site, as a status class (default: the site is not answering well).
_FAILURE_CLASSES = {"auth": "tracker_auth", "quota": "quota", "cloudflare": "cloudflare", "frozen": "frozen"}


class MirrorFetchError(TowError, RuntimeError):
    """A fetch from a tracker's mirrors failed. ``failure`` says how (``auth``, ``quota``,
    ``http``, ``http_topic``, ``redirect_policy`` ...): it decides the mirror cooldown and the
    status class; the text is the catalog key (``mirrors.*``)."""

    def __init__(self, code: str, /, *, failure: str, cls: str | None = None, **params: Any) -> None:
        super().__init__(code, cls=cls or _FAILURE_CLASSES.get(failure), **params)
        self.failure = failure

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuild_fetch_error, (self.code, self.failure, self.error_class, self.params))


def _rebuild_fetch_error(code: str, failure: str, cls: str, params: dict[str, Any]) -> MirrorFetchError:
    return MirrorFetchError(code, failure=failure, cls=cls, **params)


# Failures that describe the requested topic, not the mirror's health.
_TOPIC_LEVEL_CODES = frozenset({"auth", "http_topic", "too_large"})


def _bucket(state: dict[str, Any], tracker: str) -> dict[str, Any]:
    mirrors = state.get("mirrors")
    if not isinstance(mirrors, dict):
        mirrors = state["mirrors"] = {}
    b = mirrors.get(tracker)
    if not isinstance(b, dict):
        b = mirrors[tracker] = {}
    for key in ("fail", "cool"):
        raw = b.get(key)
        if not isinstance(raw, dict):
            b[key] = {}
            continue
        clean = {}
        for host, value in raw.items():
            if not isinstance(host, str) or isinstance(value, bool):
                continue
            try:
                number = float(value)
            except TypeError, ValueError, OverflowError:
                continue
            if not math.isfinite(number) or number < 0:
                continue
            clean[host] = int(number) if key == "fail" else number
        b[key] = clean
    if not isinstance(b.get("active"), str):
        b["active"] = None
    if not isinstance(b.get("frozen"), bool):
        b["frozen"] = False
    return b


def _save_bucket(tracker: str, bucket: dict[str, Any], host: str) -> None:
    from tow.config import load_config

    with persistence_lock():
        spec = (load_config().get("trackers") or {}).get(tracker)
        if not isinstance(spec, dict) or host.rstrip("/") not in {
            str(item).rstrip("/") for item in (spec.get("fetch_hosts") or [])
        }:
            return
        st = load_state()
        current = _bucket(st, tracker)
        before = (current["fail"].get(host), current["cool"].get(host), current["active"])
        if host in bucket["fail"]:
            current["fail"][host] = bucket["fail"][host]
        else:
            current["fail"].pop(host, None)
        if host in bucket["cool"]:
            current["cool"][host] = bucket["cool"][host]
        else:
            current["cool"].pop(host, None)
        if bucket.get("active") == host:
            current["active"] = host
        # A healthy fetch from the active mirror changes nothing: no state rewrite + fsync.
        if (current["fail"].get(host), current["cool"].get(host), current["active"]) != before:
            save_state(st)


def _ordered(hosts: list[str], active: str | None) -> list[str]:
    out = [h.rstrip("/") for h in hosts if h]
    if not active:
        return out
    a = str(active).rstrip("/")
    if a in out:
        return [a] + [h for h in out if h != a]
    return out


def has_available_host(tracker: str, hosts: list[str], *, ignore_cool: bool = False) -> bool:
    bucket = _bucket(load_state(), tracker)
    if bucket["frozen"]:
        return False
    return any(ignore_cool or float(bucket["cool"].get(host.rstrip("/")) or 0) <= _now() for host in hosts)


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, parsed.hostname.lower(), port


def origin_key(url: str) -> str | None:
    origin = _origin(url)
    if origin is None:
        return None
    scheme, host, port = origin
    return f"{scheme}://{host}:{port}"


def _cookies_for_host(
    cookies: dict[str, Any] | None,
    host: str,
    hosts: list[str],
) -> dict[str, str]:
    if not cookies:
        return {}
    if all(isinstance(value, str) for value in cookies.values()):
        origins = {origin_key(item) for item in hosts}
        if len(origins) == 1 and origin_key(host) in origins:
            return {str(name): value for name, value in cookies.items()}
        return {}
    origin = origin_key(host)
    scoped = cookies.get(origin) if origin else None
    if not isinstance(scoped, dict):
        return {}
    return {str(name): str(value) for name, value in scoped.items() if value}


_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _follow_redirects(
    c: Any, r: Any, *, ua: str | None, max_bytes: int, allowed_redirects: set[str], public_only: bool = False
) -> Any:
    """Follow at most five redirects: same-origin with the tracker client, allowlisted
    download hosts only in a fresh cookie-free client, anything else refused."""
    for _ in range(5):
        if r.status_code not in _REDIRECT_STATUSES:
            break
        loc = r.headers.get("location") or ""
        if not loc:
            raise MirrorFetchError("mirrors.no_location", failure="redirect_loop")
        nxt = urljoin(str(r.url), loc)
        current_origin = _origin(str(r.url))
        next_origin = _origin(nxt)
        if current_origin is None:
            raise MirrorFetchError("mirrors.redirect_bad_origin", failure="redirect_policy")
        if next_origin == current_origin:
            r = thttp.get_limited(c, nxt, max_bytes=max_bytes)
            continue
        if origin_key(nxt) not in allowed_redirects:
            raise MirrorFetchError("mirrors.redirect_cross_origin", failure="redirect_policy")
        # Download hosts are explicitly allowlisted, but tracker cookies must never cross
        # that boundary. Follow the rest of this redirect chain in a fresh cookie-free client.
        with thttp.client(ua=ua, follow_redirects=False, public_only=public_only) as clean:
            r = thttp.get_limited(clean, nxt, max_bytes=max_bytes)
            for _redirect in range(4):
                if r.status_code not in _REDIRECT_STATUSES:
                    break
                loc = r.headers.get("location") or ""
                if not loc:
                    raise MirrorFetchError("mirrors.no_location", failure="redirect_loop")
                following = urljoin(str(r.url), loc)
                if origin_key(following) not in allowed_redirects:
                    raise MirrorFetchError("mirrors.redirect_outside_allowlist", failure="redirect_policy")
                r = thttp.get_limited(clean, following, max_bytes=max_bytes)
            if r.status_code in _REDIRECT_STATUSES:
                raise MirrorFetchError("mirrors.redirect_limit", failure="redirect_loop")
        break
    if r.status_code in _REDIRECT_STATUSES:
        raise MirrorFetchError("mirrors.redirect_limit", failure="redirect_loop")
    return r


def _raise_for_status(r: Any) -> None:
    if r.status_code < 400:
        return
    preview = r.text[:800] if r.headers.get("content-type", "").startswith("text") else ""
    if thttp.is_cloudflare(r.status_code, preview or r.content[:800].decode("latin-1", "replace")):
        raise MirrorFetchError("mirrors.cloudflare", failure="cloudflare")
    if r.status_code == 401 or (
        r.status_code == 403
        and any(marker in preview.casefold() for marker in ("login", "sign in", "авторизац", "войти"))
    ):
        raise MirrorFetchError("mirrors.http_auth", failure="auth", status=r.status_code)
    # 5xx/429 say the host is unhealthy; other 4xx (404/410 ...) are about this topic.
    host_level = r.status_code >= 500 or r.status_code == 429
    raise MirrorFetchError("mirrors.http_status", failure="http" if host_level else "http_topic", status=r.status_code)


def _mark_host_ok(tracker: str, bucket: dict[str, Any], host: str, *, persist: bool) -> None:
    bucket["fail"][host] = 0
    bucket["cool"].pop(host, None)
    bucket["active"] = host
    if persist:
        _save_bucket(tracker, bucket, host)


def _failure_code(error: Exception) -> str:
    if isinstance(error, MirrorFetchError):
        return error.failure
    if isinstance(error, thttp.ResponseTooLargeError):
        return "too_large"
    return "transport"


def pick_and_get(
    tracker: str,
    hosts: list[str],
    path: str,
    *,
    cookies: dict[str, str] | None = None,
    ua: str | None = None,
    fail_threshold: int = 3,
    cooldown_sec: int = 3600,
    ok: Callable[[Any], bool] | None = None,
    ignore_cool: bool = False,
    persist: bool = True,
    max_bytes: int = thttp.MAX_HTML_RESPONSE_BYTES,
    allowed_redirect_origins: list[str] | None = None,
    public_only: bool = False,
) -> tuple[httpx.Response, str]:
    if not hosts:
        raise MirrorFetchError("mirrors.no_hosts", failure="config", cls="error", tracker=tracker)
    state = load_state()
    b = _bucket(state, tracker)
    if b.get("frozen"):
        raise MirrorFetchError("mirrors.frozen", failure="frozen", tracker=tracker)
    last_err = None
    auth_err = None
    allowed_redirects = {
        origin for value in (allowed_redirect_origins or []) if (origin := origin_key(value)) is not None
    }

    for host in _ordered(hosts, b.get("active")):
        host = host.rstrip("/")
        until = float(b["cool"].get(host) or 0)
        if not ignore_cool and until > _now():
            continue
        url = host + (path if path.startswith("/") else "/" + path)
        try:
            host_cookies = _cookies_for_host(cookies, host, hosts)
            with thttp.client(
                ua=ua, cookies=host_cookies or None, follow_redirects=False, public_only=public_only
            ) as c:
                r = thttp.get_limited(c, url, max_bytes=max_bytes)
                r = _follow_redirects(
                    c, r, ua=ua, max_bytes=max_bytes, allowed_redirects=allowed_redirects, public_only=public_only
                )
            _raise_for_status(r)
            if ok and not ok(r):
                if is_download_limit(r.content):
                    raise MirrorFetchError("mirrors.daily_limit", failure="quota")
                raise MirrorFetchError("mirrors.not_torrent_login", failure="auth")
            _mark_host_ok(tracker, b, host, persist=persist)
            return r, host
        except Exception as e:  # noqa: BLE001 - any failure of a host is classified (_failure_code) and the next host tried
            last_err = e
            code = _failure_code(e)
            if code == "auth":
                auth_err = e
            if code == "quota":
                break
            # Only host-health failures count towards the mirror cooldown; a missing or
            # oversized topic must not pause the whole tracker for every other topic.
            if code not in _TOPIC_LEVEL_CODES:
                n = int(b["fail"].get(host) or 0) + 1
                b["fail"][host] = n
                if n >= fail_threshold:
                    b["cool"][host] = _now() + cooldown_sec
                if persist:
                    _save_bucket(tracker, b, host)
    if last_err is None:
        raise MirrorFetchError("mirrors.all_paused", failure="paused", tracker=tracker)
    selected_err = (
        last_err if isinstance(last_err, MirrorFetchError) and last_err.failure == "quota" else auth_err or last_err
    )
    failure = _failure_code(selected_err)
    raise MirrorFetchError(
        "mirrors.all_failed",
        failure=failure,
        cls=_all_failed_class(selected_err),
        tracker=tracker,
        error=_failure_text(selected_err),
    ) from selected_err


def _all_failed_class(error: Exception) -> str:
    """Every mirror failed: the class of the failure that decided it. Every mirror saying the
    topic does not exist (404) or is gone for good (410) means the topic was removed."""
    if isinstance(error, MirrorFetchError):
        if error.failure == "http_topic" and error.params.get("status") in (404, 410):
            return "gone"
        return error.error_class
    return "tracker"


def _failure_text(error: Exception) -> Any:
    """The failure as a value of ``mirrors.all_failed``: TOW's own error as it is, anything else
    (the network library's words) as a short note."""
    if isinstance(error, TowError):
        return error
    if isinstance(error, thttp.ResponseTooLargeError):
        return Msg("mirrors.too_large")
    return Msg("mirrors.transport", error=str(error)[:160] or type(error).__name__)


def prefer_host(tracker: str, host: str) -> bool:
    from tow.config import load_config, save_config

    host = host.strip().rstrip("/")
    with persistence_lock():
        cfg = load_config()
        spec = (cfg.get("trackers") or {}).get(tracker)
        if not spec:
            return False
        lst = [h.rstrip("/") for h in (spec.get("fetch_hosts") or [])]
        if host not in lst:
            return False
        spec["fetch_hosts"] = [host] + [h for h in lst if h != host]
        save_config(cfg)
        state = load_state()
        _bucket(state, tracker)["active"] = host
        save_state(state)
    return True
