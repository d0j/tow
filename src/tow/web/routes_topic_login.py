"""Routes: signing in to a topic's site - a login and password typed on Home, or a browser window
(``tow.browser_auth``) whose cookies TOW keeps - each confirmed by a check of the topic."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from tow import i18n
from tow.log import error_class, error_fields, owner_language
from tow.mirrors import origin_key
from tow.records import CheckRow
from tow.store import CheckBusyError, SecretStoreError
from tow.trackers import GenericHttpTracker, load_trackers, match_tracker
from tow.web import services
from tow.web.site_store import restore_unverified_password, save_tracker_login
from tow.web.text import t
from tow.web.views import home_redirect, row_class, topic_check_row

router = APIRouter()


@router.get("/topics/{tid}/tracker-browser-auth")
def topics_tracker_browser_auth_info(tid: str) -> Response:
    return home_redirect("web.topics.auth_required", "warn", credential_topic=tid)


@router.post("/topics/{tid}/tracker-browser-auth")
def topics_tracker_browser_auth(tid: str) -> Response:
    state = services.load_state()
    topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == str(tid)), None)
    if not isinstance(topic, dict):
        return home_redirect("web.topics.watch_not_found", "err")
    tracker = match_tracker(load_trackers(services.load_config()), str(topic.get("url") or ""))
    if tracker is None or not tracker.spec.get("browser_auth"):
        return home_redirect("web.topics.browser_auth_not_set", "err", credential_topic=tid)
    try:
        return _start_browser_auth(str(tid), topic, tracker)
    except OSError, RuntimeError:
        return home_redirect("web.topics.auth_start_failed", "err", credential_topic=tid)


@router.get("/topics/{tid}/tracker-browser-auth/status")
def topics_tracker_browser_auth_status(tid: str, request: Request) -> Response:
    operation_id = (request.query_params.get("operation_id") or "").strip() or None
    return JSONResponse(services.browser_auth.status(str(tid), operation_id))


@router.post("/topics/{tid}/tracker-login")
def topics_tracker_login(tid: str, username: str = Form(""), password: str = Form("")) -> Response:
    state = services.load_state()
    topic = next((item for item in state.get("topics") or [] if str(item.get("id")) == str(tid)), None)
    if not isinstance(topic, dict):
        return home_redirect("web.topics.watch_not_found", "err")
    tracker = match_tracker(load_trackers(services.load_config()), str(topic.get("url") or ""))
    if tracker is None or not tracker.spec.get("login_path"):
        return home_redirect("web.topics.login_not_needed", "warn")
    if tracker.spec.get("browser_auth"):
        return home_redirect("web.topics.needs_auth", "warn", credential_topic=tid)
    if not username.strip() or not password.strip():
        return home_redirect("web.topics.enter_login", "warn", credential_topic=tid)
    try:
        with services.persistence_lock():
            if tracker.name not in (services.load_config().get("trackers") or {}):
                return home_redirect("web.topics.site_gone", "err", credential_topic=tid)
            old_secrets = copy.deepcopy(services.load_secrets())
            save_tracker_login(tracker.name, username, password, base=old_secrets)
    except SecretStoreError:
        return home_redirect("web.topics.login_not_saved", "err", credential_topic=tid)
    old_entry = copy.deepcopy((old_secrets.get("trackers") or {}).get(tracker.name))
    services.log_event("site_login", tracker=tracker.name, status="saved", how="manual")
    try:
        out = services.run_check(apply=True, notify=True, ids=[str(tid)], ignore_cool=True, how="manual", wait=False)
    except CheckBusyError:
        return home_redirect("web.topics.login_check_busy", "warn")
    except SecretStoreError as exc:
        services.record_check_failure(exc, how="manual")
        try:
            restore_unverified_password(tracker.name, old_entry, username.strip(), password.strip())
        except SecretStoreError:
            services.log_event(
                "site_login_restore_failed", tracker=tracker.name, topic=tid, status="failed", how="manual"
            )
        return home_redirect("web.topics.login_blocked", "err", credential_topic=tid)
    except Exception as exc:  # noqa: BLE001 - the check after a saved login: any failure is shown, never a 500
        # A failed check of a topic TOW has (History: "check failed"), not of a new one ("add failed").
        services.log_event("check_fail", topic=tid, tracker=tracker.name, **error_fields(exc), how="manual")
        if error_class(exc) == "tracker_auth":
            try:
                restore_unverified_password(tracker.name, old_entry, username.strip(), password.strip())
            except SecretStoreError:
                services.log_event(
                    "site_login_restore_failed", tracker=tracker.name, topic=tid, status="failed", how="manual"
                )
        return home_redirect("web.login_retry.failed", "err", site=_title(tracker), error=exc, credential_topic=tid)
    row = topic_check_row(str(tid), out)
    flash, prompt, kind = _tracker_login_retry_flash(row, _title(tracker))
    if (
        prompt
        and row
        and (
            row_class(row) == "tracker_auth"
            or row.get("fallback_reason") == "tracker_auth"
            or row.get("source") in {"magnet", "matching_magnet"}
        )
    ):
        try:
            restored = restore_unverified_password(tracker.name, old_entry, username.strip(), password.strip())
            if not restored:
                flash += t("web.topics.password_not_restored")
                kind = "err"
        except SecretStoreError:
            services.log_event(
                "site_login_restore_failed", tracker=tracker.name, topic=tid, status="failed", how="manual"
            )
            flash, kind = t("web.topics.restore_failed", site=_title(tracker)), "err"
    return home_redirect(flash, kind, credential_topic=str(tid) if prompt else None)


def _title(tracker: GenericHttpTracker) -> str:
    """The site as the pages name it (its title), not its settings key."""
    return str(tracker.spec.get("title") or tracker.name)


def _tracker_login_retry_flash(row: CheckRow | None, site: str) -> tuple[str, bool, str]:
    """(message, ask for the login again, the message's kind) after a login was saved and checked."""
    if not row:
        return t("web.login_retry.no_result", site=site), True, "warn"
    if not row.get("ok"):
        error = str(row.get("error") or t("web.check.not_confirmed")).strip()
        if row_class(row) == "qbit":
            return t("web.login_retry.client_refused", error=error), False, "err"
        failed = t("web.login_retry.failed", site=site, error=error)
        return failed, row_class(row) == "tracker_auth", "err"
    if row.get("fallback_reason") == "tracker_auth" or row.get("source") in {"magnet", "matching_magnet"}:
        return t("web.login_retry.magnet", site=site), True, "warn"
    if row.get("added"):
        return t("web.login_retry.added", site=site), False, "ok"
    if row.get("skipped"):
        return t("web.login_retry.skipped", site=site), False, "ok"
    return t("web.login_retry.ok", site=site), False, "ok"


def _browser_auth_callback(
    topic_id: str, tracker_name: str, topic_url: str
) -> Callable[[dict[str, str], str], dict[str, Any]]:
    # The callback runs in the browser-auth thread: keep the language of the request that started it.
    lang = owner_language()

    def complete(cookies: dict[str, str], browser_user_agent: str) -> dict[str, Any]:
        origin = origin_key(topic_url)
        if not origin or not cookies:
            return {"ok": False, "message": i18n.t("web.browser_auth.no_cookie", lang)}
        with services.persistence_lock():
            if tracker_name not in (services.load_config().get("trackers") or {}):
                return {"ok": False, "message": i18n.t("web.browser_auth.site_deleted", lang)}
            old_secrets = copy.deepcopy(services.load_secrets())
            new_secrets = copy.deepcopy(old_secrets)
            tracker = new_secrets.setdefault("trackers", {}).setdefault(tracker_name, {})
            scoped = tracker.setdefault("cookies_by_origin", {})
            scoped[origin] = {str(key): str(value) for key, value in cookies.items() if key and value}
            if browser_user_agent.strip():
                tracker["browser_user_agent"] = browser_user_agent.strip()
            try:
                services.save_secrets(new_secrets)
            except SecretStoreError:
                return {"ok": False, "message": i18n.t("web.browser_auth.session_not_saved", lang)}

        def restore_session() -> None:
            with services.persistence_lock():
                current = services.load_secrets()
                current_trackers = current.setdefault("trackers", {})
                if current_trackers.get(tracker_name) == tracker:
                    old_entry = (old_secrets.get("trackers") or {}).get(tracker_name)
                    if old_entry is None:
                        current_trackers.pop(tracker_name, None)
                    else:
                        current_trackers[tracker_name] = old_entry
                    services.save_secrets(current)

        services.log_event("browser_auth", topic=topic_id, tracker=tracker_name, status="session_saved", how="manual")
        try:
            out = services.run_check(apply=True, notify=True, ids=[str(topic_id)], ignore_cool=True, how="manual")
        except SecretStoreError:
            try:
                restore_session()
            except SecretStoreError:
                services.log_event(
                    "browser_auth_restore_failed", topic=topic_id, tracker=tracker_name, status="failed", how="manual"
                )
            return {"ok": False, "message": i18n.t("web.browser_auth.check_blocked", lang)}
        except Exception as exc:  # noqa: BLE001 - the browser-login callback's boundary: the owner gets the reason
            if error_class(exc) == "tracker_auth":
                try:
                    restore_session()
                except SecretStoreError:
                    services.log_event(
                        "browser_auth_restore_failed",
                        topic=topic_id,
                        tracker=tracker_name,
                        status="failed",
                        how="manual",
                    )
            return {"ok": False, "message": i18n.t("web.browser_auth.check_not_confirmed", lang, error=exc)}
        row = topic_check_row(str(topic_id), out)
        magnet_fallback = bool(
            row and (row.get("fallback_reason") == "tracker_auth" or row.get("source") in {"magnet", "matching_magnet"})
        )
        if row and row.get("ok") and not magnet_fallback:
            services.log_event("browser_auth", topic=topic_id, tracker=tracker_name, status="verified", how="manual")
            spec = (services.load_config().get("trackers") or {}).get(tracker_name)
            site = str((spec.get("title") if isinstance(spec, Mapping) else "") or tracker_name)
            return {"ok": True, "message": i18n.t("web.browser_auth.verified", lang, site=site)}
        if not row:
            return {"ok": False, "message": i18n.t("web.browser_auth.no_result", lang)}
        error = str(row.get("error") or row.get("fallback_reason") or i18n.t("web.check.not_confirmed", lang)).strip()
        if magnet_fallback or row_class(row) == "tracker_auth":
            try:
                restore_session()
            except SecretStoreError:
                services.log_event(
                    "browser_auth_restore_failed", topic=topic_id, tracker=tracker_name, status="failed", how="manual"
                )
            return {"ok": False, "message": i18n.t("web.browser_auth.not_confirmed", lang, error=error)}
        return {"ok": False, "message": i18n.t("web.browser_auth.login_not_confirmed", lang, error=error)}

    return complete


def _browser_start_url(tracker: GenericHttpTracker, topic_url: str) -> str:
    hosts: list[str] = list(tracker.spec.get("login_hosts") or tracker.spec.get("fetch_hosts") or [])
    login_path = str(tracker.spec.get("login_path") or "")
    # Only an http(s) site: origin_key is None for anything else, and two Nones are no match (a
    # "host" like --gpu-launcher=… would reach the browser as a command-line switch).
    origin = origin_key(hosts[0]) if hosts else None
    if origin is None or not login_path or origin != origin_key(topic_url):
        raise ValueError(t("web.browser_auth.no_safe_login_url"))
    topic = urlparse(topic_url)
    login = urlparse(login_path)
    login_dir = str(PurePosixPath(login.path).parent).rstrip("/") + "/"
    redirect = topic.path
    redirect = redirect[len(login_dir) :] if redirect.startswith(login_dir) else redirect.lstrip("/")
    if topic.query:
        redirect += "?" + topic.query
    separator = "&" if login.query else "?"
    return hosts[0].rstrip("/") + login_path + separator + "redirect=" + quote(redirect, safe="")


def _start_browser_auth(topic_id: str, topic: Mapping[str, Any], tracker: GenericHttpTracker) -> RedirectResponse:
    topic_url = str(topic.get("url") or "")
    if not topic_url:
        return home_redirect("web.browser_auth.no_topic_url", "err", credential_topic=topic_id)
    try:
        result = services.browser_auth.start(
            topic_id=str(topic_id),
            tracker_name=tracker.name,
            topic_url=topic_url,
            start_url=_browser_start_url(tracker, topic_url),
            on_success=_browser_auth_callback(str(topic_id), tracker.name, topic_url),
            timeout_sec=900,
        )
    except TypeError, ValueError:
        return home_redirect("web.browser_auth.no_login_url", "err", credential_topic=topic_id)
    operation_id = str(result.get("operation_id") or "")
    return home_redirect("", credential_topic=topic_id, browser_auth_id=operation_id)
