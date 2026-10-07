"""Routes: tracker sites and doctor."""

from __future__ import annotations

import copy
import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.background import BackgroundTask

from tow import store_transaction, undo
from tow.guess import GuessError, guess_from_url
from tow.jsonish import as_dict
from tow.log import error_class
from tow.store_transaction import StoreTransaction
from tow.undo.snapshots import site_snapshot
from tow.web import _context, services
from tow.web.site_form import (
    split_lines,
    unresolved_note,
    valid_site_hosts,
    valid_site_name,
    valid_tracker_path,
    validate_tracker_regexes,
)
from tow.web.site_store import commit_site_stores, save_together, with_login
from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import add_draft, add_refused_redirect, flash_location, flash_redirect, request_flash

router = APIRouter()


@router.get("/sites", response_class=HTMLResponse)
def sites(request: Request) -> Response:
    state = _context.state()  # read-only snapshots of this request (tow.web._context)
    trackers = _context.config().get("trackers") or {}
    probes = [
        row
        for row in (state.get("doctor") or {}).get("probes") or []
        if isinstance(row, dict)
        and row.get("tracker") in trackers
        and row.get("host") in (trackers[row["tracker"]].get("fetch_hosts") or [])
    ]
    frozen = {k: bool(v.get("frozen")) for k, v in (state.get("mirrors") or {}).items() if isinstance(v, dict)}
    active = {
        k: (v or {}).get("active")
        if (v or {}).get("active") in (trackers.get(k, {}).get("fetch_hosts") or [])
        else None
        for k, v in (state.get("mirrors") or {}).items()
        if isinstance(v, dict)
    }
    open_hosts: dict[str, str] = {}
    for name, spec in trackers.items():
        hosts = spec.get("fetch_hosts") or []
        preferred = active.get(name)
        status = {row["host"]: bool(row.get("ok")) for row in probes if row["tracker"] == name}
        if preferred and status.get(preferred) is not False:
            open_hosts[name] = preferred
        else:
            open_hosts[name] = next(
                (host for host in hosts if status.get(host) is True), preferred or (hosts[0] if hosts else "")
            )
    return TEMPLATES.TemplateResponse(
        request,
        "sites.html",
        {
            "title": t("web.title.sites"),
            "trackers": trackers,
            "frozen": frozen,
            "probes": probes,
            "active": active,
            "open_hosts": open_hosts,
            "logins": {
                k: {
                    "user": (v or {}).get("username") or "",
                    "set": bool((v or {}).get("username") or (v or {}).get("password") or (v or {}).get("uid")),
                }
                for k, v in (_context.secrets().get("trackers") or {}).items()
            },
            "flash": request_flash(request),
            "add_draft": add_draft(request),
        },
    )


@router.post("/topics/guess-title")
def topics_guess_title(url: str = Form("")) -> Response:
    try:
        title = services.guess_topic_title(url.strip())
    except Exception as exc:  # noqa: BLE001 - a title guess never fails the form: the owner types one
        logging.getLogger("tow.web").warning("title not guessed: %s", type(exc).__name__)
        title = ""
    return JSONResponse({"ok": bool(title), "title": title})


@router.post("/sites/guess")
def sites_guess(url: str = Form("")) -> Response:
    try:
        g = guess_from_url(url)
    except GuessError as e:
        return JSONResponse({"ok": False, "error": t(e.key)})
    cfg = services.load_config()
    g["ok"] = True
    g["exists"] = g["name"] in (cfg.get("trackers") or {})
    return JSONResponse(g)


def _save_site(config: dict[str, Any] | None = None, secrets: dict[str, Any] | None = None) -> RedirectResponse | None:
    """A site's config and login as one transaction, which also ends an older undo: a later,
    independent change must not leave it actionable. None when saved, else the refusal."""
    state = services.load_state()

    def write(txn: StoreTransaction) -> None:
        if config is not None:
            txn.save_config(config)
        if secrets is not None:
            txn.save_secrets(secrets)
        if undo.invalidate(state, txn):
            txn.save_state(state)

    return save_together("/sites", write)


def _commit_site(
    config: dict[str, Any],
    state: dict[str, Any],
    secrets: dict[str, Any],
    undo_fields: dict[str, Any],
    snapshot: dict[str, Any],
) -> RedirectResponse | None:
    """One transaction for the site's config, mirrors, logins and undo; a refusal if it failed."""
    try:
        commit_site_stores(config, state, secrets, undo_fields=undo_fields, secret_undo_snapshot=snapshot)
    except store_transaction.RollbackError as exc:
        services.log_event("site_save_fail", status="rollback_failed", error=error_class(exc), how="manual")
        return flash_redirect("/sites", "web.common.rollback_incomplete", "err")
    except store_transaction.TransactionError as exc:
        services.log_event("site_save_fail", status="restored", error=error_class(exc.__cause__ or exc), how="manual")
        return flash_redirect("/sites", "web.sites.not_saved", "err")
    return None


def _saved_location(message: str, hosts: list[str]) -> str:
    """Back to Sites after a save; a warning names the mirrors whose names do not resolve now."""
    if note := unresolved_note(hosts):
        return flash_location("/sites", f"{t(message)}. {note}", "warn")
    return flash_location("/sites", message)


@router.post("/sites/new")
@services.locked_state_mutation
def sites_new(
    name: str = Form(""),
    url_regex: str = Form(""),
    fetch_hosts: str = Form(""),
    download_path: str = Form(""),
    from_url: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    login_path: str = Form(""),
    page_download: str = Form(""),
    topic_path: str = Form(""),
    download_href_regex: str = Form(""),
) -> Response:
    draft = {
        "name": name,
        "url_regex": url_regex,
        "fetch_hosts": fetch_hosts,
        "download_path": download_path,
        "from_url": from_url,
        "username": username,
        "login_path": login_path,
        "page_download": page_download,
        "topic_path": topic_path,
        "download_href_regex": download_href_regex,
    }

    def refused(problem: Any, field: str) -> RedirectResponse:
        """D2 for sites: the form comes back open with what was typed (never the password),
        the reason above it and the cursor in the field it is about."""
        return add_refused_redirect(str(problem), draft, kind=field, page="/sites")

    guessed = None
    if from_url.strip():
        try:
            guessed = guess_from_url(from_url)
        except GuessError as e:
            return refused(t(e.key), "from_url")
    try:
        key = valid_site_name(name or str((guessed or {}).get("name") or ""))
    except ValueError as exc:
        return refused(exc, "name")
    cfg = services.load_config()
    trackers = cfg.setdefault("trackers", {})
    typed_login = bool(username.strip() or password.strip())
    if key in trackers:
        if typed_login:
            if refused_save := _save_site(secrets=with_login(services.load_secrets(), key, username, password)):
                return refused_save
            return flash_redirect("/sites", "web.sites.login_saved", "ok")
        return refused(t("web.sites.exists", name=key), "name")
    try:
        hosts = valid_site_hosts(split_lines(fetch_hosts) or split_lines(str((guessed or {}).get("fetch_hosts") or "")))
    except ValueError as exc:
        return refused(exc, "fetch_hosts")
    regex = url_regex.strip() or str((guessed or {}).get("url_regex") or "")
    if not regex:
        return refused(t("web.sites.regex_missing"), "url_regex")
    paths = {}
    for field, typed, default, label in (
        ("download_path", download_path, "/download/{id}", "sites.download_path"),
        ("login_path", login_path, "", "sites.login_path"),
        ("topic_path", topic_path, "", "sites.topic_path"),
    ):
        try:
            paths[field] = valid_tracker_path(
                typed or str((guessed or {}).get(field) or default), label=t(label), needs_id=field != "login_path"
            )
        except ValueError as exc:
            return refused(exc, field)
    dl, lp, tp = paths["download_path"], paths["login_path"], paths["topic_path"]
    href = download_href_regex.strip() or str((guessed or {}).get("download_href_regex") or "")
    pd = page_download in ("1", "true", "on") or bool((guessed or {}).get("page_download"))
    for field, pair in (("url_regex", (regex, "")), ("download_href_regex", ("", href))):
        try:
            validate_tracker_regexes(*pair)
        except ValueError as exc:
            return refused(exc, field)
    spec = {
        "title": key,
        "url_regex": regex,
        "login_hosts": list(hosts),
        "fetch_hosts": hosts,
        "login_path": lp,
        "download_path": dl,
        "cookie_names": [],
        "fail_threshold": 3,
        "cooldown_sec": 3600,
    }
    if pd:
        spec["page_download"] = True
    if tp:
        spec["topic_path"] = tp
    if href:
        spec["download_href_regex"] = href
    trackers[key] = spec
    secrets = with_login(services.load_secrets(), key, username, password) if typed_login else None
    if not_saved := _save_site(config=cfg, secrets=secrets):
        return not_saved
    services.log_event("site_add", tracker=key, how="manual")
    return RedirectResponse(
        _saved_location("web.sites.added", hosts),
        status_code=303,
        background=BackgroundTask(services.doctor_report, probe=True, names=[key]),
    )


@router.post("/sites/{name}/freeze")
@services.locked_state_mutation
def sites_freeze(name: str) -> Response:
    if name not in (services.load_config().get("trackers") or {}):
        return flash_redirect("/sites", "web.sites.no_site", "err")
    state = services.load_state()
    b = state.setdefault("mirrors", {}).setdefault(name, {})
    b["frozen"] = not bool(b.get("frozen"))
    services.save_state(state)
    services.log_event("site_pause", tracker=name, how="manual", status="paused" if b["frozen"] else "resumed")
    return flash_redirect("/sites", "web.sites.paused" if b["frozen"] else "web.sites.resumed")


@router.post("/sites/{name}/delete")
@services.locked_state_mutation
def sites_delete(name: str) -> Response:
    old_config = services.load_config()
    new_config = copy.deepcopy(old_config)
    spec = (new_config.get("trackers") or {}).pop(name, None)
    if spec is None:
        return flash_redirect("/sites", "web.sites.no_site", "err")
    old_state = services.load_state()
    new_state = copy.deepcopy(old_state)
    mirrors = new_state.setdefault("mirrors", {})
    mirror_present = name in mirrors
    mirror = copy.deepcopy(mirrors.pop(name, None))
    old_secrets = services.load_secrets()
    new_secrets = copy.deepcopy(old_secrets)
    tracker_secrets = new_secrets.get("trackers")
    if isinstance(tracker_secrets, dict):
        tracker_secrets.pop(name, None)
    undo_fields = {"name": name, "spec": spec, "mirror": mirror, "mirror_present": mirror_present}
    if refused := _commit_site(new_config, new_state, new_secrets, undo_fields, site_snapshot(old_secrets, name)):
        return refused
    services.log_event("site_delete", tracker=name, how="manual")
    return flash_redirect("/sites", "web.sites.deleted", "ok")


@router.post("/sites/{name}")
@services.locked_state_mutation
def sites_save(
    name: str,
    fetch_hosts: str = Form(""),
    login_hosts: str | None = Form(None),
    login_hosts_present: str = Form(""),
    url_regex: str = Form(""),
    download_path: str = Form(""),
    login_path: str = Form(""),
    topic_path: str = Form(""),
    page_download: str = Form(""),
    download_href_regex: str = Form(""),
    new_name: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
) -> Response:
    old_config = services.load_config()
    new_config = copy.deepcopy(old_config)
    trackers = new_config.setdefault("trackers", {})
    current_spec = trackers.get(name)
    if not isinstance(current_spec, dict):
        return flash_redirect("/sites", "web.sites.no_site", "err")
    previous_spec = copy.deepcopy(current_spec)
    try:
        validate_tracker_regexes(
            url_regex.strip() or str(current_spec.get("url_regex") or ""), download_href_regex.strip()
        )
    except ValueError as exc:
        return flash_redirect("/sites", exc, "err")
    try:
        key = valid_site_name(new_name) if new_name.strip() else name
        new_hosts = valid_site_hosts(split_lines(fetch_hosts))
        # An emptied list arrives as no field at all: the form's marker says it was there.
        login_hosts_value = login_hosts if login_hosts is not None else "" if login_hosts_present == "1" else None
        chosen_login_hosts = (
            (
                valid_site_hosts(split_lines(login_hosts_value))
                if login_hosts_value and login_hosts_value.strip()
                else []
            )
            if login_hosts_value is not None
            else None
        )
        if chosen_login_hosts is not None and any(host not in new_hosts for host in chosen_login_hosts):
            raise ValueError(t("web.sites.login_host_not_mirror"))
    except ValueError as exc:
        return flash_redirect("/sites", exc, "err")
    if key != name and key in trackers:
        return flash_redirect("/sites", "web.sites.name_taken", "err")
    spec = trackers[name]
    old_login_hosts = list(spec.get("login_hosts") or [])
    spec["fetch_hosts"] = new_hosts
    # Removed mirrors must never receive credentials; new mirrors need explicit login setup.
    spec["login_hosts"] = (
        chosen_login_hosts
        if chosen_login_hosts is not None
        else [host for host in old_login_hosts if host in new_hosts]
    )
    added_login_hosts = set(spec["login_hosts"]) - set(old_login_hosts)
    stored_password = ((services.load_secrets().get("trackers") or {}).get(name) or {}).get("password")
    if added_login_hosts and stored_password and not password.strip():
        # A stored tracker password must never be posted to a host it was not entered for.
        return flash_redirect("/sites", "web.sites.password_again", "warn")
    if url_regex.strip():
        spec["url_regex"] = url_regex.strip()
    try:
        new_download_path = (
            valid_tracker_path(download_path, label=t("sites.download_path"), needs_id=True)
            if download_path.strip()
            else ""
        )
        new_login_path = valid_tracker_path(login_path, label=t("sites.login_path"))
        new_topic_path = valid_tracker_path(topic_path, label=t("sites.topic_path"), needs_id=True)
    except ValueError as exc:
        return flash_redirect("/sites", exc, "err")
    if new_download_path:
        spec["download_path"] = new_download_path
    spec["login_path"] = new_login_path
    if new_topic_path:
        spec["topic_path"] = new_topic_path
    else:
        spec.pop("topic_path", None)
    if page_download in ("1", "true", "on"):
        spec["page_download"] = True
    else:
        spec.pop("page_download", None)
    if download_href_regex.strip():
        spec["download_href_regex"] = download_href_regex.strip()
    else:
        spec.pop("download_href_regex", None)
    old_state = services.load_state()
    new_state = copy.deepcopy(old_state)
    old_mirrors = as_dict(old_state.get("mirrors"))
    mirror_present = name in old_mirrors
    mirror = copy.deepcopy(old_mirrors.get(name))
    mirrors = new_state.setdefault("mirrors", {})
    if key != name:
        trackers[key] = trackers.pop(name)
        trackers[key]["title"] = key
        if mirror_present:
            mirrors[key] = mirrors.pop(name)
        else:
            mirrors.pop(name, None)
    old_secrets = services.load_secrets()
    new_secrets = copy.deepcopy(old_secrets)
    tracker_secrets = new_secrets.setdefault("trackers", {})
    if key != name and name in tracker_secrets:
        tracker_secrets[key] = tracker_secrets.pop(name)
    if username.strip() or password.strip():
        entry = tracker_secrets.setdefault(key, {})
        if username.strip():
            entry["username"] = username.strip()
        if password.strip():
            entry["password"] = password.strip()
    undo_fields = {
        "name": name,
        "spec": previous_spec,
        "mirror": mirror,
        "mirror_present": mirror_present,
        "renamed_to": key if key != name else "",
    }
    if refused := _commit_site(new_config, new_state, new_secrets, undo_fields, site_snapshot(old_secrets, name)):
        return refused
    services.log_event("site_save", tracker=key, how="manual")
    return RedirectResponse(_saved_location("web.common.saved", new_hosts), status_code=303)


@router.post("/sites/{name}/login")
@services.locked_state_mutation
def sites_login(name: str, username: str = Form(""), password: str = Form("")) -> Response:
    if name not in (services.load_config().get("trackers") or {}):
        return flash_redirect("/sites", "web.sites.no_site", "err")
    if (username.strip() or password.strip()) and (
        refused := _save_site(secrets=with_login(services.load_secrets(), name, username, password))
    ):
        return refused
    return flash_redirect("/sites", "web.sites.login_saved", "ok")


@router.post("/sites/{name}/probe")
def sites_probe(name: str) -> Response:
    if name not in (services.load_config().get("trackers") or {}):
        return flash_redirect("/sites", "web.sites.no_site", "err")
    report = services.doctor_report(probe=True, names=[name])
    services.log_event("site_probe", tracker=name, how="manual")
    probes = [p for p in report.get("probes") or [] if p.get("tracker") == name]
    answering = sum(1 for p in probes if p.get("ok"))
    # D3: every mirror answering is ok; a mirror that does not answer is transport trouble -
    # amber (the status-colour contract), as the site's row shows it; no site checked is an error.
    kind = "ok" if probes and answering == len(probes) else "warn" if probes else "err"
    return flash_redirect("/sites", "web.sites.probe", kind, site=name, answering=answering, total=len(probes))


@router.post("/sites/{name}/prefer")
def sites_prefer(name: str, host: str = Form()) -> Response:
    from tow.mirrors import prefer_host

    ok = prefer_host(name, host)
    services.log_event("site_prefer", tracker=name, url=host, how="manual")
    return flash_redirect("/sites", "web.sites.preferred" if ok else "web.sites.no_mirror", "ok" if ok else "err")


# M4: the doctor's raw probe and ping results in the owner's words (the raw text stays in a
# tooltip). Checked in order; the first phrase found in the lower-cased text wins.
_DOCTOR_REASONS = (
    ("secrets missing", "doctor.reason.client_not_set"),
    ("10061", "doctor.reason.refused"),
    ("connection refused", "doctor.reason.refused"),
    ("actively refused", "doctor.reason.refused"),
    ("timed out", "doctor.reason.timeout"),
    ("timeout", "doctor.reason.timeout"),
    ("getaddrinfo", "doctor.reason.no_address"),
    ("name or service not known", "doctor.reason.no_address"),
    ("11001", "doctor.reason.no_address"),
    ("could not be resolved", "doctor.reason.no_address"),  # tow.net_guard: the name is not in DNS
    ("no usable address", "doctor.reason.no_address"),
    ("non-public address", "doctor.reason.home_address"),
    ("cloudflare", "doctor.reason.cloudflare"),
    ("redirect", "doctor.reason.redirect"),
    ("certificate", "doctor.reason.certificate"),
    ("ssl", "doctor.reason.certificate"),
    ("10054", "doctor.reason.reset"),
    ("connection reset", "doctor.reason.reset"),
)


def _doctor_reason(raw: str) -> str:
    text = str(raw or "").strip()
    low = text.lower()
    if low.startswith("http ") and low[5:].strip().isdigit():
        return t("doctor.reason.http", code=low[5:].strip())
    for phrase, key in _DOCTOR_REASONS:
        if phrase in low:
            return t(key)
    return text or t("doctor.reason.unknown")


def _doctor_view(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return report
    view = dict(report)
    qbit = report.get("qbit")
    if qbit is not None:
        raw = str(qbit)
        failed = raw.startswith("FAIL")
        view["qbit_ok"] = not failed
        view["qbit_text"] = (
            t("doctor.client_fail", reason=_doctor_reason(raw[4:])) if failed else t("doctor.client_ok", value=raw)
        )
    view["probes"] = [
        {
            **probe,
            "text": t("doctor.probe_ok", status=probe.get("status") or "")
            if probe.get("ok")
            else _doctor_reason(str(probe.get("error") or "")),
        }
        for probe in report.get("probes") or []
        if isinstance(probe, dict)
    ]
    return view


@router.get("/doctor", response_class=HTMLResponse)
def doctor_page(request: Request) -> Response:
    return TEMPLATES.TemplateResponse(
        request,
        "doctor.html",
        {
            "title": t("web.title.doctor"),
            "report": _doctor_view(services.doctor_report(probe=False)),
            "flash": request_flash(request),
        },
    )


@router.post("/doctor/run")
def doctor_run(request: Request) -> Response:
    report = services.doctor_report(probe=True)
    services.log_event("site_probe", how="manual")
    path = urlparse(request.headers.get("referer") or "").path or "/"
    if path not in ("/", "/sites", "/settings", "/doctor"):
        path = "/doctor"
    degraded = len(report.get("degraded") or [])
    kind = "warn" if degraded else "ok"
    if report.get("ok"):
        flash = t("web.doctor.ok") + (t("web.doctor.degraded", count=degraded) if degraded else "")
    else:
        # Sites that do not answer are amber (transport); a torrent client that does not is red.
        kind = "warn"
        problems = []
        if str(report.get("qbit") or "").startswith("FAIL"):
            kind = "err"
            problems.append(t("web.doctor.client_down"))
        by_site: dict[str, bool] = {}
        for probe in report.get("probes") or []:
            name = str(probe.get("tracker"))
            by_site[name] = by_site.get(name, False) or bool(probe.get("ok"))
        dead = sorted(name for name, ok in by_site.items() if not ok)
        if dead:
            problems.append(t("web.doctor.dead", sites=", ".join(dead)))
        if not problems:
            kind = "err"
        flash = t("web.doctor.prefix") + ("; ".join(problems) or t("web.doctor.problems"))
    return flash_redirect(path, flash, kind)
