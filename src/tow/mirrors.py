from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

import httpcore
import httpx

from tow import http as thttp
from tow.errors import Msg, TowError
from tow.protection import Verdict, classify
from tow.store import load_state, persistence_lock, save_state
from tow.torrent import is_download_limit

if TYPE_CHECKING:
    from tow.protection.spec import Clearance


def _now() -> float:
    return time.time()


# What a failure says about the site, as a status class (default: the site is not answering well).
_FAILURE_CLASSES = {
    "auth": "tracker_auth",
    "quota": "quota",
    "cloudflare": "cloudflare",
    "frozen": "frozen",
    "gone": "gone",
    "interactive": "cloudflare",
    "blocked": "tracker",
    "rate_limited": "tracker",
    "origin_down": "tracker",
    "maintenance": "tracker",
    "unexpected": "not_torrent",
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

    def __init__(
        self, code: str, /, *, failure: str, cls: str | None = None, host_failure: bool | None = None, **params: Any
    ) -> None:
        super().__init__(code, cls=cls or _FAILURE_CLASSES.get(failure), **params)
        self.failure = failure
        self.host_failure = host_failure  # request-scoped evidence, not a persisted diagnostic or catalog value

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuild_fetch_error, (self.code, self.failure, self.error_class, self.params, self.host_failure))


def _rebuild_fetch_error(
    code: str, failure: str, cls: str, params: dict[str, Any], host_failure: bool | None = None
) -> MirrorFetchError:
    return MirrorFetchError(code, failure=failure, cls=cls, host_failure=host_failure, **params)


# Failures that describe the requested topic, not the mirror's health; nor does TOW's own
# failure on an answer it got (``local``): neither counts towards the mirror cooldown.
_NOT_HOST_HEALTH_CODES = frozenset({"auth", "http_topic", "too_large", "gone", "local", "unexpected"})


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
    for key in ("fail", "cool", "rate_cool"):
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
    if not isinstance(b.get("observed"), dict):
        b["observed"] = {}
    return b


def _save_bucket(tracker: str, bucket: dict[str, Any], host: str) -> None:
    from tow.config import load_config

    with persistence_lock():
        spec = (load_config().get("trackers") or {}).get(tracker)
        if not isinstance(spec, dict) or host.rstrip("/") not in {
            str(item).rstrip("/") for item in [*(spec.get("fetch_hosts") or []), *(spec.get("login_hosts") or [])]
        }:
            return
        st = load_state()
        current = _bucket(st, tracker)
        before = (
            current["fail"].get(host),
            current["cool"].get(host),
            current["rate_cool"].get(host),
            current["active"],
            current.get("observed", {}).get(host),
        )
        if host in bucket["fail"]:
            current["fail"][host] = bucket["fail"][host]
        else:
            current["fail"].pop(host, None)
        if host in bucket["cool"]:
            current["cool"][host] = bucket["cool"][host]
        else:
            current["cool"].pop(host, None)
        if host in bucket["rate_cool"]:
            current["rate_cool"][host] = max(float(current["rate_cool"].get(host) or 0), bucket["rate_cool"][host])
        elif float(current["rate_cool"].get(host) or 0) <= _now():
            current["rate_cool"].pop(host, None)
        if float(current["rate_cool"].get(host) or 0) > _now():
            current["cool"][host] = max(float(current["cool"].get(host) or 0), current["rate_cool"][host])
        if bucket.get("active") == host:
            current["active"] = host
        observed = bucket.get("observed", {}).get(host)
        if isinstance(observed, dict):
            configured = {
                str(item).rstrip("/") for item in [*(spec.get("fetch_hosts") or []), *(spec.get("login_hosts") or [])]
            }
            current["observed"] = {
                key: value for key, value in current.get("observed", {}).items() if key in configured
            }
            current["observed"][host] = observed
        # A healthy fetch from the active mirror changes nothing: no state rewrite + fsync.
        if (
            current["fail"].get(host),
            current["cool"].get(host),
            current["rate_cool"].get(host),
            current["active"],
            current.get("observed", {}).get(host),
        ) != before:
            save_state(st)


def _ordered(hosts: list[str], active: str | None, observed: dict[str, Any] | None = None) -> list[str]:
    out = [h.rstrip("/") for h in hosts if h]
    a = str(active or "").rstrip("/")
    if a in out:
        out = [a] + [h for h in out if h != a]

    def protected(host: str) -> bool:
        row = (observed or {}).get(host)
        if not isinstance(row, dict) or row.get("failure") not in {
            "cloudflare",
            "interactive",
            "blocked",
            "rate_limited",
        }:
            return False
        at = row.get("at")
        return (
            isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at) and 0 <= _now() - at < 3600
        )

    return sorted(out, key=protected)  # stable: healthy/unknown mirrors first; stale evidence expires


def has_available_host(tracker: str, hosts: list[str], *, ignore_cool: bool = False) -> bool:
    bucket = _bucket(load_state(), tracker)
    if bucket["frozen"]:
        return False
    return any(not _host_paused(bucket, host.rstrip("/"), ignore_cool=ignore_cool) for host in hosts)


def _host_paused(bucket: dict[str, Any], host: str, *, ignore_cool: bool) -> bool:
    if float(bucket["rate_cool"].get(host) or 0) > _now():
        return True
    until = float(bucket["cool"].get(host) or 0)
    observed = bucket.get("observed", {}).get(host)
    rate_limited = isinstance(observed, dict) and observed.get("failure") == "rate_limited"
    return until > _now() and (not ignore_cool or rate_limited)


def _pause_rate_limited(bucket: dict[str, Any], host: str, retry_after: float) -> None:
    failures = int(bucket["fail"].get(host) or 1)
    delay = max(min(1800, 60 * 2 ** min(failures - 1, 5)), retry_after)
    bucket["cool"][host] = max(float(bucket["cool"].get(host) or 0), _now() + delay)
    bucket["rate_cool"][host] = max(float(bucket["rate_cool"].get(host) or 0), _now() + delay)


def note_rate_limit(tracker: str, host: str, response: httpx.Response, *, persist: bool) -> bool:
    """Direct password sign-in obeys the same mirror limit as ordinary GET requests."""
    if response_verdict(response) != Verdict.RATE_LIMITED:
        return False
    if persist:
        with persistence_lock():
            host = host.rstrip("/")
            bucket = _bucket(load_state(), tracker)
            bucket["fail"][host] = int(bucket["fail"].get(host) or 0) + 1
            _observe(bucket, host, "rate_limited", response.status_code)
            _pause_rate_limited(
                bucket, host, thttp.retry_after_seconds(response.headers.get("retry-after"), now=_now())
            )
            _save_bucket(tracker, bucket, host)
    return True


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


def note_signed_in(tracker: str) -> None:
    """Remember that the owner's sign-in to this (still configured) site was just confirmed by a
    check (``signed_in_at``): its other topics refused before that are not asked to sign in again
    until their next check says so (``tow.issues.kinds.site_needs_login``)."""
    from tow.config import load_config

    with persistence_lock():
        if not isinstance((load_config().get("trackers") or {}).get(tracker), dict):
            return
        st = load_state()
        _bucket(st, tracker)["signed_in_at"] = _now()
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


def cached_request_credentials(
    tracker: str,
    host: str,
    protection: dict[str, Any] | None,
    ua: str | None,
    cookies: dict[str, str],
    *,
    used: dict[str, Clearance] | None = None,
) -> tuple[str | None, dict[str, str]]:
    """Read-only reuse of verified origin-bound clearance; no external request."""
    if not protection or protection.get("enabled") is not True:
        return ua, cookies
    from tow.protection.chain import cached

    clearance = cached(tracker, host, protection)
    if clearance is not None and used is not None:
        used[host] = clearance  # the exact pre-request identity, not a later cache lookup
    return (clearance.user_agent, {**cookies, **clearance.cookies}) if clearance else (ua, cookies)


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
    c: Any,
    r: Any,
    *,
    ua: str | None,
    max_bytes: int,
    allowed_redirects: set[str],
    public_only: bool = False,
    deadline: float | None = None,
    probe_path: str | None = None,
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
        if probe_path is not None:
            target = urlparse(nxt)
            # Root-to-sign-in redirects can establish anonymous reachability. A
            # path-specific probe may not fall back to an unchallenged home page.
            allowed_probe_paths = (
                {"/", "/index.php", "/forum/index.php", "/login", "/login.php", "/forum/login.php"}
                if probe_path == "/"
                else {probe_path}
            )
            if target.path not in allowed_probe_paths or target.query or target.fragment:
                raise MirrorFetchError("mirrors.redirect_bad_origin", failure="redirect_policy")
        current_origin = _origin(str(r.url))
        next_origin = _origin(nxt)
        if current_origin is None:
            raise MirrorFetchError("mirrors.redirect_bad_origin", failure="redirect_policy")
        if next_origin == current_origin:
            r = _deadline_get(c, nxt, max_bytes=max_bytes, deadline=deadline)
            continue
        if origin_key(nxt) not in allowed_redirects:
            raise MirrorFetchError("mirrors.redirect_cross_origin", failure="redirect_policy")
        # Download hosts are explicitly allowlisted, but tracker cookies must never cross
        # that boundary. Follow the rest of this redirect chain in a fresh cookie-free client.
        with thttp.client(ua=ua, follow_redirects=False, public_only=public_only) as clean:
            r = _deadline_get(clean, nxt, max_bytes=max_bytes, deadline=deadline)
            for _redirect in range(4):
                if r.status_code not in _REDIRECT_STATUSES:
                    break
                loc = r.headers.get("location") or ""
                if not loc:
                    raise MirrorFetchError("mirrors.no_location", failure="redirect_loop")
                following = urljoin(str(r.url), loc)
                if origin_key(following) not in allowed_redirects:
                    raise MirrorFetchError("mirrors.redirect_outside_allowlist", failure="redirect_policy")
                r = _deadline_get(clean, following, max_bytes=max_bytes, deadline=deadline)
            if r.status_code in _REDIRECT_STATUSES:
                raise MirrorFetchError("mirrors.redirect_limit", failure="redirect_loop")
        break
    if r.status_code in _REDIRECT_STATUSES:
        raise MirrorFetchError("mirrors.redirect_limit", failure="redirect_loop")
    return r


def _deadline_get(c: Any, url: str, *, max_bytes: int, deadline: float | None) -> httpx.Response:
    if deadline is None:
        return thttp.get_limited(c, url, max_bytes=max_bytes)
    from tow.protection.transport import bounded_get

    return bounded_get(c, url, max_bytes=max_bytes, deadline=deadline)


_VERDICT_KEYS = {
    Verdict.CHALLENGE: "mirrors.cloudflare",
    Verdict.INTERACTIVE: "mirrors.interactive",
    Verdict.BLOCKED: "mirrors.blocked",
    Verdict.RATE_LIMITED: "mirrors.rate_limited",
    Verdict.ORIGIN_DOWN: "mirrors.origin_down",
    Verdict.MAINTENANCE: "mirrors.maintenance",
    Verdict.LOGIN_NEEDED: "mirrors.http_auth",
    Verdict.DAILY_LIMIT: "mirrors.daily_limit",
    Verdict.UNEXPECTED: "mirrors.unexpected",
}


def response_verdict(
    r: Any, *, maintenance_markers: list[str] | None = None, wanted: bool = False, download_limit: bool = False
) -> Verdict:
    from types import SimpleNamespace

    content = getattr(r, "content", None)
    bounded = bytes(content[:65536]) if isinstance(content, (bytes, bytearray)) else None
    if bounded is not None and isinstance(content, (bytes, bytearray)) and len(content) > len(bounded):
        try:
            bounded.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data":
                # A byte cap can split a valid UTF-8 character. Do not let that final
                # fragment make the shared decoder reinterpret the entire prefix as cp1251.
                bounded = bounded[: exc.start]
    page = SimpleNamespace(
        content=bounded,
        text="" if isinstance(content, (bytes, bytearray)) else str(getattr(r, "text", "") or "")[:65536],
        headers=r.headers,
    )
    return classify(
        r.status_code,
        thttp.html_text(page),
        r.headers,
        maintenance_markers=maintenance_markers or (),
        torrent_wanted=wanted,
        daily_limit=download_limit and isinstance(content, (bytes, bytearray)) and is_download_limit(bytes(content)),
    )


def refuse_verdict(verdict: Verdict, status: int) -> None:
    if verdict != Verdict.OK:
        raise MirrorFetchError(_VERDICT_KEYS[verdict], failure=verdict.value, status=status)


def _raise_for_status(r: Any) -> None:
    if r.status_code < 400:
        return
    verdict = response_verdict(r)
    if verdict == Verdict.RATE_LIMITED:
        raise MirrorFetchError(
            "mirrors.rate_limited",
            failure="rate_limited",
            status=r.status_code,
            retry_after=thttp.retry_after_seconds(r.headers.get("retry-after"), now=_now()),
        )
    refuse_verdict(verdict, r.status_code)
    preview = thttp.html_text(r)[:800]
    if r.status_code == 401 or (
        r.status_code == 403
        and any(marker in preview.casefold() for marker in ("login", "sign in", "авторизац", "войти"))
    ):
        raise MirrorFetchError("mirrors.http_auth", failure="auth", status=r.status_code)
    # 5xx/429 say the host is unhealthy; other 4xx (404/410 ...) are about this topic.
    host_level = r.status_code >= 500 or r.status_code == 429
    raise MirrorFetchError("mirrors.http_status", failure="http" if host_level else "http_topic", status=r.status_code)


def _refuse_page(r: Any, *, wanted: bool, download_limit: bool, maintenance_markers: list[str] | None = None) -> None:
    """A good status, but not the answer: why. ``wanted``: a torrent was asked for (any other
    page is then refused; without it only a Cloudflare check page is)."""
    verdict = response_verdict(r, maintenance_markers=maintenance_markers, wanted=wanted, download_limit=download_limit)
    if wanted and verdict == Verdict.UNEXPECTED and says_topic_removed(r.content):
        raise MirrorFetchError("mirrors.topic_removed", failure="gone")
    refuse_verdict(verdict, r.status_code)


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
    bucket["rate_cool"].pop(host, None)
    bucket["active"] = host
    if persist:
        _save_bucket(tracker, bucket, host)


def _observe(bucket: dict[str, Any], host: str, failure: str, status: int | None = None) -> None:
    if not isinstance(bucket.get("observed"), dict):
        bucket["observed"] = {}
    old = bucket["observed"].get(host)
    # A routine success does not rewrite/fsync state for every page. An error (or a
    # transition back to success) is fresh evidence for Sites and mirror preference.
    if failure == "ok" and isinstance(old, dict) and old.get("failure") == "ok":
        return
    bucket["observed"][host] = {"failure": failure, "at": _now(), "status": status}


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
        return error.failure in {"cloudflare", "maintenance"}
    return isinstance(error, _SLOW_ANSWER_ERRORS)


def _host_answers(
    host: str,
    *,
    cookies: dict[str, str] | None,
    ua: str | None,
    public_only: bool,
    on_challenge: Callable[[], None] | None = None,
) -> bool:
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
    verdict = response_verdict(r)
    if verdict in {Verdict.CHALLENGE, Verdict.INTERACTIVE} and on_challenge is not None:
        on_challenge()
    if r.status_code >= 500 or r.status_code == 429:
        return False
    return verdict not in {
        Verdict.CHALLENGE,
        Verdict.INTERACTIVE,
        Verdict.BLOCKED,
        Verdict.RATE_LIMITED,
        Verdict.ORIGIN_DOWN,
        Verdict.MAINTENANCE,
    }


def _counts_against_host(
    error: Exception,
    host: str,
    path: str,
    *,
    cookies: dict[str, str] | None,
    ua: str | None,
    public_only: bool,
    persist: bool,
    revoke_access: Callable[[], None] | None = None,
) -> bool:
    """Whether a failure counts towards the host's cooldown. A failure that may be one page's
    own counts only when the host's front page fails too; a preview (``persist=False``) keeps
    no count, so it does not ask the front page either."""
    if _failure_code(error) in _NOT_HOST_HEALTH_CODES:
        return False
    if not _may_be_page_only(error) or path.split("?", 1)[0] in {"", "/"}:
        return True
    return persist and not _host_answers(
        host, cookies=cookies, ua=ua, public_only=public_only, on_challenge=revoke_access
    )


def _access_revoker(tracker: str, host: str, access: Clearance | None, persist: bool) -> Callable[[], None] | None:
    if not persist or access is None:
        return None
    from tow.protection import chain

    return lambda: chain.discard(tracker, host, expected=access)


def _invalidate_probe_access(tracker: str, path: str, failure: str, revoke: Callable[[], None] | None) -> None:
    if revoke is None or failure not in {"cloudflare", "interactive"}:
        return
    from tow.protection.spec import public_probe_path

    try:
        if "/" + path.lstrip("/") in {"/", public_probe_path(tracker)}:
            revoke()
    except ValueError:
        pass  # a broken optional probe declaration cannot change the resource's failure


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
    maintenance_markers: list[str] | None = None,
    protection: dict[str, Any] | None = None,
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
    failures: list[Exception] = []
    failure_indices: dict[str, int] = {}
    host_failures: list[bool] = []
    mirror_reasons: list[Msg] = []
    challenged: list[str] = []
    allowed_redirects = {
        origin for value in (allowed_redirect_origins or []) if (origin := origin_key(value)) is not None
    }

    for host in _ordered(hosts, b.get("active"), b.get("observed")):
        host = host.rstrip("/")
        if _host_paused(b, host, ignore_cool=ignore_cool):
            continue
        url = host + (path if path.startswith("/") else "/" + path)
        host_cookies = _cookies_for_host(cookies, host, hosts)
        used_access: dict[str, Clearance] = {}
        host_ua, host_cookies = cached_request_credentials(
            tracker, host, protection, ua, host_cookies, used=used_access
        )
        revoke_access = _access_revoker(tracker, host, used_access.get(host), persist)
        try:
            with thttp.client(
                ua=host_ua, cookies=host_cookies or None, follow_redirects=False, public_only=public_only
            ) as c:
                r = thttp.get_limited(c, url, max_bytes=max_bytes)
                r = _follow_redirects(
                    c, r, ua=host_ua, max_bytes=max_bytes, allowed_redirects=allowed_redirects, public_only=public_only
                )
            _raise_for_status(r)
            if ok is None or not ok(r):
                _refuse_page(
                    r, wanted=ok is not None, download_limit=download_limit, maintenance_markers=maintenance_markers
                )
            _observe(b, host, "ok", r.status_code)
            _mark_host_ok(tracker, b, host, persist=persist)
            return r, host
        except Exception as e:  # noqa: BLE001 - any failure of a host is classified (_failure_code) and the next host tried
            last_err = e
            failure_indices[host] = len(failures)
            failures.append(e)
            code = _failure_code(e)
            if code == "cloudflare":
                challenged.append(host)
            _invalidate_probe_access(tracker, path, code, revoke_access)
            mirror_reasons.append(Msg("mirrors.mirror_reason", host=urlparse(host).netloc, error=_failure_text(e)))
            _observe(b, host, code, getattr(e, "params", {}).get("status"))
            if code == "quota":
                host_failures.append(False)
                if persist:
                    _save_bucket(tracker, b, host)
                break
            # Only host-health failures count towards the mirror cooldown; a missing or
            # oversized topic, a check page or a slow answer on one topic's page (its host's
            # front page answers), or TOW failing on a good answer, must not pause the whole
            # tracker for every other topic.
            host_failed = _counts_against_host(
                e,
                host,
                path,
                cookies=host_cookies,
                ua=host_ua,
                public_only=public_only,
                persist=persist,
                revoke_access=revoke_access,
            )
            host_failures.append(host_failed)
            if host_failed:
                n = int(b["fail"].get(host) or 0) + 1
                b["fail"][host] = n
                if code == "rate_limited":
                    _pause_rate_limited(b, host, float(getattr(e, "params", {}).get("retry_after") or 0))
                elif n >= fail_threshold:
                    b["cool"][host] = _now() + cooldown_sec
            if persist:
                _save_bucket(tracker, b, host)
    if persist and challenged and not (isinstance(last_err, MirrorFetchError) and last_err.failure == "quota"):
        resource_failures: dict[str, Exception] = {}
        recovered = _recover_challenges(
            tracker,
            challenged,
            hosts,
            path,
            b,
            protection=protection,
            cookies=cookies,
            ua=ua,
            ok=ok,
            max_bytes=max_bytes,
            allowed_redirects=allowed_redirects,
            public_only=public_only,
            download_limit=download_limit,
            maintenance_markers=maintenance_markers,
            resource_failures=resource_failures,
        )
        if recovered is not None:
            return recovered
        _merge_recovery_failures(failure_indices, failures, host_failures, mirror_reasons, resource_failures)
    if last_err is None:
        raise MirrorFetchError("mirrors.all_paused", failure="paused", tracker=tracker)
    selected_err = _preferred_failure(failures)
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
        host_failure=bool(host_failures) and all(host_failures),
        tracker=tracker,
        error=_failure_text(selected_err),
        details=_reason_set(mirror_reasons[:16]),
    ) from selected_err


def _verify_clearance(response: httpx.Response, host: str, markers: list[str] | None) -> None:
    """An account sign-in page is reachable access, even when it carries HTTP 401/403."""
    if _origin(str(response.url)) != _origin(host):
        raise MirrorFetchError("mirrors.redirect_cross_origin", failure="redirect_policy")
    verdict = response_verdict(response, maintenance_markers=markers, wanted=response.status_code in {401, 403})
    if verdict == Verdict.LOGIN_NEEDED:
        return
    _raise_for_status(response)
    refuse_verdict(verdict, response.status_code)


def _recovery_supported(public_only: bool, deadline: float) -> bool:
    """Do not spend a solver attempt on a transport whose deadline cannot be enforced."""
    from tow.protection.transport import with_deadline

    try:
        with thttp.client(follow_redirects=False, public_only=public_only) as client, with_deadline(client, deadline):
            pass
    except Exception:  # noqa: BLE001 - preserve normal mirror diagnostics; no service was contacted
        return False
    return True


def _recover_challenges(
    tracker: str,
    challenged: list[str],
    hosts: list[str],
    path: str,
    bucket: dict[str, Any],
    *,
    protection: dict[str, Any] | None,
    cookies: dict[str, Any] | None,
    ua: str | None,
    ok: Callable[[Any], bool] | None,
    max_bytes: int,
    allowed_redirects: set[str],
    public_only: bool,
    download_limit: bool,
    maintenance_markers: list[str] | None,
    resource_failures: dict[str, Exception],
) -> tuple[httpx.Response, str] | None:
    """Only after ordinary mirrors fail; never send a login or download to a provider.

    A provider supplies credentials, not an answer. A guarded GET of the site's
    audited public probe validates them; the requested path is fetched normally once.
    """
    if not protection or protection.get("enabled") is not True:
        return None
    from tow.protection import chain, store
    from tow.protection.spec import RecoveryBudget, public_probe_path

    try:
        probe_path = public_probe_path(tracker)
        challenged = store.recovery_origins(tracker, challenged)
    except Exception:  # noqa: BLE001 - unreadable optional storage/preset preserves ordinary mirror failures
        return None

    deadline = time.monotonic() + float(protection.get("timeout_sec", 30))
    if not _recovery_supported(public_only, deadline):
        return None
    call_budget = RecoveryBudget(int(protection.get("max_attempts", 2)))
    for host in challenged:
        if time.monotonic() >= deadline or call_budget.remaining <= 0:
            break
        if _host_paused(bucket, host, ignore_cool=True):
            continue  # recovery cannot override a server's rate deadline
        candidates = chain.candidates(
            tracker,
            host,
            protection,
            public_only=public_only,
            budget_sec=deadline - time.monotonic(),
            skip_cached=True,
            call_budget=call_budget,
        )
        try:
            with ExitStack() as cleanup:
                if callable(close := getattr(candidates, "close", None)):
                    cleanup.callback(close)
                result = _recover_origin(
                    candidates,
                    tracker,
                    host,
                    hosts,
                    path,
                    bucket,
                    protection=protection,
                    cookies=cookies,
                    ok=ok,
                    max_bytes=max_bytes,
                    allowed_redirects=allowed_redirects,
                    public_only=public_only,
                    download_limit=download_limit,
                    maintenance_markers=maintenance_markers,
                    probe_path=probe_path,
                    deadline=deadline,
                    resource_failures=resource_failures,
                )
        except _RecoveryStopped:
            return None
        if result is not None:
            return result
    return None


def _recover_origin(
    candidates: Iterator[Clearance],
    tracker: str,
    host: str,
    hosts: list[str],
    path: str,
    bucket: dict[str, Any],
    *,
    protection: dict[str, Any],
    cookies: dict[str, Any] | None,
    ok: Callable[[Any], bool] | None,
    max_bytes: int,
    allowed_redirects: set[str],
    public_only: bool,
    download_limit: bool,
    maintenance_markers: list[str] | None,
    probe_path: str,
    deadline: float,
    resource_failures: dict[str, Exception],
) -> tuple[httpx.Response, str] | None:
    from tow.protection import chain

    for clearance in candidates:
        verified_access = False
        phase = "probe"
        started = time.monotonic()
        try:
            with thttp.client(
                ua=clearance.user_agent, cookies=clearance.cookies, follow_redirects=False, public_only=public_only
            ) as probe:
                verified = _deadline_get(
                    probe, host + probe_path, max_bytes=thttp.MAX_HTML_RESPONSE_BYTES, deadline=deadline
                )
                verified = _follow_redirects(
                    probe,
                    verified,
                    ua=clearance.user_agent,
                    max_bytes=thttp.MAX_HTML_RESPONSE_BYTES,
                    allowed_redirects=set(),
                    public_only=public_only,
                    deadline=deadline,
                    probe_path=probe_path,
                )
            _verify_clearance(verified, host, maintenance_markers)
            chain.accept(tracker, host, protection, clearance)
            verified_access = True
            if time.monotonic() >= deadline:
                break
            merged = {**_cookies_for_host(cookies, host, hosts), **clearance.cookies}
            phase = "resource"
            with thttp.client(
                ua=clearance.user_agent, cookies=merged, follow_redirects=False, public_only=public_only
            ) as c:
                r = _deadline_get(
                    c, host + (path if path.startswith("/") else "/" + path), max_bytes=max_bytes, deadline=deadline
                )
                r = _follow_redirects(
                    c,
                    r,
                    ua=clearance.user_agent,
                    max_bytes=max_bytes,
                    allowed_redirects=allowed_redirects,
                    public_only=public_only,
                    deadline=deadline,
                )
            _raise_for_status(r)
            if ok is None or not ok(r):
                _refuse_page(
                    r, wanted=ok is not None, download_limit=download_limit, maintenance_markers=maintenance_markers
                )
            _observe(bucket, host, "ok", r.status_code)
            _mark_host_ok(tracker, bucket, host, persist=True)
            chain.record_event("protection_restored", tracker, clearance.provider, "succeeded")
            return r, host
        except MirrorFetchError as exc:
            if exc.failure == "rate_limited":
                bucket["fail"][host] = int(bucket["fail"].get(host) or 0) + 1
                _observe(bucket, host, exc.failure, exc.params.get("status"))
                _pause_rate_limited(bucket, host, float(exc.params.get("retry_after") or 0))
                _save_bucket(tracker, bucket, host)
            chain.record_event(
                "protection_replay_refused",
                tracker,
                clearance.provider,
                "failed",
                reason=exc.failure,
                phase=phase,
                elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
            if verified_access:
                resource_failures[host] = exc
                _observe(bucket, host, exc.failure, exc.params.get("status"))
                _save_bucket(tracker, bucket, host)
            if exc.failure in {"quota", "blocked"}:
                # A probe ban/quota ends recovery too; do not manufacture resource evidence.
                raise _RecoveryStopped from exc
            if verified_access or exc.failure not in {"cloudflare", "interactive", "redirect_policy"}:
                break  # stop this origin, not recovery of the next allowed mirror
            chain.discard(tracker, host, expected=clearance)
        except Exception as exc:  # noqa: BLE001 - optional probe/resource outage ends only this origin
            reason, _ = chain.failure_details(exc, phase)
            chain.record_event(
                "protection_replay_refused",
                tracker,
                clearance.provider,
                "failed",
                reason=reason,
                phase=phase,
                elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
            if verified_access:
                resource_failures[host] = exc
                _observe(bucket, host, _failure_code(exc))
                _save_bucket(tracker, bucket, host)
            break
    return None


class _RecoveryStopped(Exception):
    """No more service calls after a site's explicit ban or quota."""


def _merge_recovery_failures(
    indices: dict[str, int],
    failures: list[Exception],
    host_failures: list[bool],
    reasons: list[Msg],
    replacements: dict[str, Exception],
) -> None:
    for host, error in replacements.items():
        index = indices[host]
        failures[index] = error
        host_failures[index] = False  # the origin's anonymous page independently answered
        reasons[index] = Msg("mirrors.mirror_reason", host=urlparse(host).netloc, error=_failure_text(error))


def _preferred_failure(failures: list[Exception]) -> Exception:
    for failure_kind in ("quota", "auth"):
        for error in reversed(failures):
            if _failure_code(error) == failure_kind:
                return error
    return _deciding_failure(failures)


def _reason_set(reasons: list[Msg]) -> Msg | str:
    if not reasons:
        return ""
    result = reasons[0]
    for reason in reasons[1:]:
        result = Msg("mirrors.reason_pair", first=result, second=reason)
    return result


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
