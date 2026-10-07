"""Route: "Undo" - the owner's last change put back (``tow.undo``)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from tow import access
from tow.auth import AuthConfigurationError
from tow.store import SecretStoreError
from tow.web import services
from tow.web.views import flash_redirect

router = APIRouter()


@router.post("/undo")
def undo_last(request: Request) -> Response:
    """Put the owner's last change back (``tow.undo``): every kind, one pipeline."""
    local = access.is_local(request)
    outcome = services.apply_undo(local=local)
    response = flash_redirect(outcome.target, outcome.text(), outcome.kind)
    if outcome.signed_out and not local:
        # The password came back and every device was signed out; this one, which undid it, stays
        # signed in (as after a password change), with a session for the password now in force.
        try:
            session_key = services.network_credential(services.load_secrets()).session_key
        except AuthConfigurationError, SecretStoreError:
            return response
        services.set_session_cookie(response, request, session_key)
    return response
