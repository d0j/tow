"""Routes: tracker sites and doctor."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tow.guess import GuessError, guess_from_url
from tow.web import _context, services
from tow.web.site_actions import add_site, delete_site, edit_site, save_site
from tow.web.site_form import NewSite, SiteEdit
from tow.web.site_store import with_login
from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import add_draft, flash_redirect, request_flash

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
    form = NewSite(
        name=name,
        url_regex=url_regex,
        fetch_hosts=fetch_hosts,
        download_path=download_path,
        from_url=from_url,
        username=username,
        password=password,
        login_path=login_path,
        page_download=page_download,
        topic_path=topic_path,
        download_href_regex=download_href_regex,
    )
    return add_site(form)


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
    return delete_site(name)


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
    form = SiteEdit(
        fetch_hosts=fetch_hosts,
        login_hosts=login_hosts,
        login_hosts_present=login_hosts_present,
        url_regex=url_regex,
        download_path=download_path,
        login_path=login_path,
        topic_path=topic_path,
        page_download=page_download,
        download_href_regex=download_href_regex,
        new_name=new_name,
        username=username,
        password=password,
    )
    return edit_site(name, form)


@router.post("/sites/{name}/login")
@services.locked_state_mutation
def sites_login(name: str, username: str = Form(""), password: str = Form("")) -> Response:
    if name not in (services.load_config().get("trackers") or {}):
        return flash_redirect("/sites", "web.sites.no_site", "err")
    if (username.strip() or password.strip()) and (
        refused := save_site(secrets=with_login(services.load_secrets(), name, username, password))
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
    ok = services.prefer_host(name, host)
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
