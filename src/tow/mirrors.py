from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from tow import http as thttp
from tow.errors import Msg, TowError
from tow.store import load_state, persistence_lock, save_state
from tow.torrent import is_download_limit


def _now() -> float:
    return time.time()


# What a failure says about the site, as a status class (default: the site is not answering well).
_FAILURE_CLASSES = {
    "auth": "tracker_auth",
    "quota": "quota",
    "cloudflare": "cloudflare",
    "frozen": "frozen",
    "gone": "gone",
}
# A password login is not repeated by scheduled checks within this time: a site that answers
# every download with a page (a removed topic, a check page) must not get the password each time.
LOGIN_PAUSE_SEC = 600
# What a site's page says when the topic does not exist (any case; the page in any encoding).
_REMOVED_PHRASES = (
    "тема не найдена",
    "тема не существует",
    "темы не существует",
    "такой темы нет",
    "раздача не найдена",
    "раздача не существует",
    "раздачи не существует",
    "нет такой раздачи",
    "торрент не найден",
    "topic not found",
    "topic does not exist",
    "the requested topic does not exist",
    "torrent not found",
    "no such torrent",
)


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


# Failures that describe the requested topic, not the mirror's health; nor does TOW's own
# failure on an answer it got (``local``): neither counts towards the mirror cooldown.
_NOT_HOST_HEALTH_CODES = frozenset({"auth", "http_topic", "too_large", "gone", "local"})


def says_topic_removed(content: bytes | str) -> bool:
    """The page says the topic does not exist (a site answering 200 instead of 404)."""
    texts = [content[:65536]] if isinstance(content, str) else []
    if isinstance(content, (bytes, bytearray)):
        raw = bytes(content[:65536])
        for encoding in ("utf-8", "cp1251"):
            try:
                texts.append(raw.decode(encoding))
            except UnicodeDecodeError:
                continue
    return any(phrase in text.casefold() for text in texts for phrase in _REMOVED_PHRASES)


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


def login_recent(tracker: str) -> bool:
    """A password login of this site was tried less than ``LOGIN_PAUSE_SEC`` ago."""
    at = _bucket(load_state(), tracker).get("login_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return False
    return 0 <= _now() - at < LOGIN_PAUSE_SEC


def note_login(tracker: str) -> None:
    """Remember that a password login of this (still configured) site is being tried now."""
    from tow.config import load_config

    with persistence_lock():
        if not isinstance((load_config().get("trackers") or {}).get(tracker), dict):
            return
        st = load_state()
        _bucket(st, tracker)["login_at"] = _now()
        save_state(st)


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
    if thttp.is_cloudflare(r.status_code, preview or r.content[:800].decode("latin-1", "replace"), r.headers):
        raise MirrorFetchError("mirrors.cloudflare", failure="cloudflare")
    if r.status_code == 401 or (
        r.status_code == 403
        and any(marker in preview.casefold() for marker in ("login", "sign in", "авторизац", "войти"))
    ):
        raise MirrorFetchError("mirrors.http_auth", failure="auth", status=r.status_code)
    # 5xx/429 say the host is unhealthy; other 4xx (404/410 ...) are about this topic.
    host_level = r.status_code >= 500 or r.status_code == 429
    raise MirrorFetchError("mirrors.http_status", failure="http" if host_level else "http_topic", status=r.status_code)


def _refuse_page(r: Any, *, wanted: bool, download_limit: bool) -> None:
    """A good status, but not the answer: why. ``wanted``: a torrent was asked for (any other
    page is then refused; without it only a Cloudflare check page is)."""
    head = bytes(r.content[:65536]).decode("latin-1")
    if thttp.is_cloudflare(r.status_code, head, r.headers):
        raise MirrorFetchError("mirrors.cloudflare", failure="cloudflare")
    if not wanted:
        return
    if download_limit and is_download_limit(r.content):
        raise MirrorFetchError("mirrors.daily_limit", failure="quota")
    if says_topic_removed(r.content):
        raise MirrorFetchError("mirrors.topic_removed", failure="gone")
    raise MirrorFetchError("mirrors.not_torrent_login", failure="auth")


def _says_gone(error: Exception) -> bool:
    if not isinstance(error, MirrorFetchError):
        return False
    return error.failure == "gone" or (error.failure == "http_topic" and error.params.get("status") in (404, 410))


def _deciding_failure(failures: list[Exception]) -> Exception:
    """The failure that names the run: "the topic is gone" only when every mirror tried says
    so; a mirror that did not answer leaves it open, whichever mirror was asked last."""
    if all(_says_gone(error) for error in failures):
        return failures[-1]
    return next(error for error in reversed(failures) if not _says_gone(error))


def _mark_host_ok(tracker: str, bucket: dict[str, Any], host: str, *, persist: bool) -> None:
    bucket["fail"][host] = 0
    bucket["cool"].pop(host, None)
    bucket["active"] = host
    if persist:
        _save_bucket(tracker, bucket, host)


# What a host that does not answer well raises: the network library's errors, the socket's,
# a body that never ends. Anything else is TOW failing on an answer it got (``local``).
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    httpx.StreamError,
    httpcore.NetworkError,
    httpcore.TimeoutException,
    httpcore.ProtocolError,
    httpcore.ProxyError,
    httpcore.UnsupportedProtocol,
    OSError,
    thttp.ResponseTooSlowError,
)


def _failure_code(error: Exception) -> str:
    if isinstance(error, MirrorFetchError):
        return error.failure
    if isinstance(error, thttp.ResponseTooLargeError):
        return "too_large"
    if isinstance(error, _TRANSPORT_ERRORS):
        return "transport"
    return "local"


# A host that took the request and then did not answer it in time: the page may be the slow one.
_SLOW_ANSWER_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpcore.ReadTimeout,
    httpcore.WriteTimeout,
    thttp.ResponseTooSlowError,
)


def _may_be_page_only(error: Exception) -> bool:
    """A failure that may belong to the one page asked for, not to its host: a Cloudflare check
    on that page, or a page the host was too slow to give. A refused or reset connection, a
    name or TLS failure, a connect timeout and a 5xx always say the host."""
    if isinstance(error, MirrorFetchError):
        return error.failure == "cloudflare"
    return isinstance(error, _SLOW_ANSWER_ERRORS)


def _host_answers(host: str, *, cookies: dict[str, str] | None, ua: str | None, public_only: bool) -> bool:
    """The host's front page answers - any status but a 5xx or 429, no Cloudflare check: a
    failure of another page is then that page's own and does not pause the host. A front page
    that fails too (a site-wide check, a host that hangs) says the host."""
    try:
        with thttp.client(ua=ua, cookies=cookies or None, follow_redirects=False, public_only=public_only) as c:
            r = thttp.get_limited(c, host + "/", max_bytes=thttp.MAX_HTML_RESPONSE_BYTES)
    except thttp.ResponseTooLargeError:
        return True  # it answered, at length
    except Exception:  # noqa: BLE001 - a front page that does not come is the host's failure
        return False
    if r.status_code >= 500 or r.status_code == 429:
        return False
    head = bytes(r.content[:8192]).decode("latin-1")
    return not thttp.is_cloudflare(r.status_code, head, r.headers)


def _counts_against_host(
    error: Exception,
    host: str,
    path: str,
    *,
    cookies: dict[str, str] | None,
    ua: str | None,
    public_only: bool,
    persist: bool,
) -> bool:
    """Whether a failure counts towards the host's cooldown. A failure that may be one page's
    own counts only when the host's front page fails too; a preview (``persist=False``) keeps
    no count, so it does not ask the front page either."""
    if _failure_code(error) in _NOT_HOST_HEALTH_CODES:
        return False
    if not _may_be_page_only(error) or path.split("?", 1)[0] in {"", "/"}:
        return True
    return persist and not _host_answers(host, cookies=cookies, ua=ua, public_only=public_only)


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
    download_limit: bool = False,
) -> tuple[httpx.Response, str]:
    """The first mirror's good answer: ``ok`` says what a good one is (None: any page that is
    not a Cloudflare check). ``download_limit``: the site has a daily download limit, so a
    page instead of a torrent may be the one saying it is reached."""
    if not hosts:
        raise MirrorFetchError("mirrors.no_hosts", failure="config", cls="error", tracker=tracker)
    state = load_state()
    b = _bucket(state, tracker)
    if b.get("frozen"):
        raise MirrorFetchError("mirrors.frozen", failure="frozen", tracker=tracker)
    last_err = None
    auth_err = None
    failures: list[Exception] = []
    allowed_redirects = {
        origin for value in (allowed_redirect_origins or []) if (origin := origin_key(value)) is not None
    }

    for host in _ordered(hosts, b.get("active")):
        host = host.rstrip("/")
        until = float(b["cool"].get(host) or 0)
        if not ignore_cool and until > _now():
            continue
        url = host + (path if path.startswith("/") else "/" + path)
        host_cookies = _cookies_for_host(cookies, host, hosts)
        try:
            with thttp.client(
                ua=ua, cookies=host_cookies or None, follow_redirects=False, public_only=public_only
            ) as c:
                r = thttp.get_limited(c, url, max_bytes=max_bytes)
                r = _follow_redirects(
                    c, r, ua=ua, max_bytes=max_bytes, allowed_redirects=allowed_redirects, public_only=public_only
                )
            _raise_for_status(r)
            if ok is None or not ok(r):
                _refuse_page(r, wanted=ok is not None, download_limit=download_limit)
            _mark_host_ok(tracker, b, host, persist=persist)
            return r, host
        except Exception as e:  # noqa: BLE001 - any failure of a host is classified (_failure_code) and the next host tried
            last_err = e
            failures.append(e)
            code = _failure_code(e)
            if code == "auth":
                auth_err = e
            if code == "quota":
                break
            # Only host-health failures count towards the mirror cooldown; a missing or
            # oversized topic, a check page or a slow answer on one topic's page (its host's
            # front page answers), or TOW failing on a good answer, must not pause the whole
            # tracker for every other topic.
            if _counts_against_host(
                e, host, path, cookies=host_cookies, ua=ua, public_only=public_only, persist=persist
            ):
                n = int(b["fail"].get(host) or 0) + 1
                b["fail"][host] = n
                if n >= fail_threshold:
                    b["cool"][host] = _now() + cooldown_sec
                if persist:
                    _save_bucket(tracker, b, host)
    if last_err is None:
        raise MirrorFetchError("mirrors.all_paused", failure="paused", tracker=tracker)
    selected_err = (
        last_err
        if isinstance(last_err, MirrorFetchError) and last_err.failure == "quota"
        else auth_err or _deciding_failure(failures)
    )
    failure = _failure_code(selected_err)
    if failure == "local":
        # A mirror answered and TOW failed on the answer: red, never "no mirror answered".
        raise MirrorFetchError(
            "mirrors.local", failure=failure, cls="error", tracker=tracker, error=type(selected_err).__name__
        ) from selected_err
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
        return "gone" if _says_gone(error) else error.error_class
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
