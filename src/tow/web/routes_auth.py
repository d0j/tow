"""Routes: signing in and out from a device on the network (this computer needs no password).

The password itself is set on the first-start page and in Settings (``routes_password``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tow import __version__, access
from tow.auth import AuthConfigurationError
from tow.web import _context, services
from tow.web.templating import TEMPLATES
from tow.web.text import t

router = APIRouter()


def _login_response(request: Request, error: str = "", status_code: int = 200, **response: Any) -> Response:
    """The sign-in page: what the network signs in with, or why nobody can (a damaged password
    or unreadable secrets say so; nothing set up at all says where to set the password)."""
    enabled = access.network_open(_context.config())
    configured, kind, problem = False, "password", ""
    if enabled:
        try:
            kind = services.network_credential(_context.secrets_or_none()).kind
            configured = True
        except AuthConfigurationError as exc:
            problem = "" if exc.code == "auth.token_missing" else str(exc)
    return TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {
            "configured": configured,
            "enabled": enabled,
            "credential_kind": kind,
            "problem": problem,
            "error": "" if problem else error,  # the reason is already the page's alert
            "version": __version__,
        },
        status_code=status_code,
        **response,
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    return _login_response(request)


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, password: str = Form(""), token: str = Form("")) -> Response:
    if not access.network_open(_context.config()):
        return RedirectResponse("/", status_code=303)
    peer = request.client.host if request.client else "unknown"
    try:
        credential = services.network_credential(_context.secrets_or_none())
    except AuthConfigurationError:
        return _login_response(request, t("web.login.no_password"), status_code=503)
    # Reserved (counted as a failure) before the check, given back on success: parallel
    # wrong guesses all see each other's attempts.
    if wait := services.login_throttle.attempt(peer):
        return _login_response(
            request, t("web.login.too_many", sec=wait), status_code=429, headers={"Retry-After": str(wait)}
        )
    with services.login_throttle.verification:
        valid = credential.matches(password or token)
    if not valid:
        wrong = t("web.login.wrong_password") if credential.kind == "password" else t("web.login.wrong_key")
        return _login_response(request, wrong, status_code=401)
    services.login_throttle.success(peer)
    response = RedirectResponse("/", status_code=303)
    services.set_session_cookie(response, request, credential.session_key)
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    response = RedirectResponse("/login", status_code=303)
    services.sign_out(request, response)
    return response
