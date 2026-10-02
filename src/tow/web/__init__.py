"""TOW's web interface: ``app`` is the ASGI app (``uvicorn tow.web:app``, what ``tow serve`` runs),
built by ``create_app`` in ``tow/web/app.py``. docs/EXTENDING.md says how to add a page."""

from tow.web.app import app, create_app

__all__ = ["app", "create_app"]
