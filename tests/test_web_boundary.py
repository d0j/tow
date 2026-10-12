"""A route module - and the actions it hands a form to (``*_actions.py``) - reaches everything
outside ``tow.web`` through ``tow.web.services`` (AGENTS.md).

Directly from its home module such a module may import only a pure helper - a constant, an error
type, or a function that formats, parses, validates or computes on what it is given and reads or
writes nothing of the install, no client, no site and no service. Those are listed below. Anything
else is ``services.<name>``, where the tests replace it; a route that needs a new one adds it to
``tow.web.services``, not to this list.

The web's helper modules (views, templating, middleware...) are held to the same rule with a
ratchet of their own (``HELPER_ALLOWED``). A route module does not import another route module,
and every route module is in ``tow.web.app.ROUTERS``.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
WEB = SRC / "tow" / "web"

# Module -> the pure helpers route modules import from it (or use as ``module.name``).
PURE: dict[str, set[str]] = {
    "tow": {"__version__"},
    # Who asks (this computer or the network), the password's record and the dependency that
    # keeps a route to this computer; signing in and out (the session store) is in services.
    "tow.access": {
        "RECORD_KEY",
        "is_local",
        "network_open",
        "new_record",
        "password_is_set",
        "password_record",
        "require_local",
    },
    "tow.auth": {
        "AuthConfigurationError",
        "MAX_HINT_LENGTH",
        "lan_password_matches",
        "lan_password_session_key",
        "password_hint",
        "with_hint",
    },
    # network_bind: the address network access listens on, from the configured one; no I/O.
    "tow.bind": {"network_bind"},
    "tow.bundle": {"MAX_BUNDLE_BYTES"},
    "tow.check": {"blocked_by_previous_revision"},
    # The client list in the configuration (a dict): read and changed in memory, saved by the route.
    "tow.clients.factory": {
        "add_client",
        "client_configuration",
        "client_configurations",
        "client_secret_block",
        "default_client_id",
        "ready",
        "remove_client",
        "set_default_client",
    },
    "tow.clients.managed": {"ClientError"},
    # DEFAULT_KIND: the client kind a form that names none means (a constant).
    "tow.clients.spec": {"DEFAULT_KIND", "get"},
    "tow.clock": {"format_ui_timestamp", "iso_now", "machine_now"},
    # whole_number: reads a typed number, no I/O.
    "tow.config": {
        "INTERVAL_MAX_MINUTES",
        "INTERVAL_MIN_MINUTES",
        "THEMES",
        "as_bool",
        "flash_ttl",
        "interval_sec_of",
        "whole_number",
    },
    "tow.content": {"describe"},
    "tow.diagnostic_json": {"epoch"},
    "tow.episodes": {"parse_season_hint"},
    "tow.errors": {"TowError"},
    # Save folders: the one a form names and the recent ones kept in the state (a dict).
    "tow.folders": {"paths_equal", "recent_save_roots", "remember_save_root", "resolve_save_path"},
    "tow.guess": {"GuessError", "canon_watch_url", "guess_from_url"},
    "tow.i18n": {
        "AUTO",
        "available",
        "canonical",
        "codes",
        "current",
        "for_request",
        "setting",
        "t",
        "translate",
        "use",
    },
    # A backup folder's place and the reason it is refused, by its name only.
    "tow.locations": {"LOCATIONS", "is_network_share", "problem", "resolve"},
    "tow.log": {
        "BACKUPS",
        "HISTORY_GROUPS",
        "MAX_BYTES",
        "error_class",
        "error_fields",
        "format_event",
        "index_event_titles",
        "owner_language",
    },
    "tow.jsonish": {"as_dict"},
    "tow.mirrors": {"origin_key"},
    # The messengers' settings in the secrets (a dict) and their cards; a test message is in services.
    # storage_scope: where a messenger's settings sit in the secrets (from its module), no I/O.
    "tow.notifiers": {"cards", "health", "kinds", "remove", "storage_scope", "store", "title", "validate"},
    # TOW's own temporary folder (AGENTS.md: every path from tow.paths).
    "tow.paths": {"tmp_dir"},
    "tow.pulse": {"clock"},
    # shown_title: the name a topic record is shown by, no I/O.
    "tow.records": {"CheckRow", "shown_title"},
    "tow.restore_points": {"CREATE_FAILED", "INVALID_FILE", "ROLLBACK_FAILED", "RestorePointError", "invalid_file"},
    "tow.selection": {"SelectionPendingError", "normalize_policy", "resolve_selection"},
    "tow.snapshots": {"SnapshotError"},
    "tow.store": {"CheckBusyError", "SecretStoreError", "StoreCorruptionError"},
    "tow.store_transaction": {"RollbackError", "StoreTransaction", "TransactionError"},
    "tow.title": {"title_is_placeholder"},
    "tow.topic_form": {
        "TopicForm",
        "apply_selection",
        "bind_content",
        "new_topic",
        "pending_move",
        "previous_selection",
        "save_path_refusal",
        "selection_policy",
    },
    "tow.topic_timers": {"parse_interval", "set_interval"},
    "tow.torrent": {"MAX_TORRENT_BYTES", "TorrentMetadata", "TorrentPathConflictError", "parse_torrent_metadata"},
    "tow.trackers": {"GenericHttpTracker", "load_trackers", "match_tracker"},
    # The change "Undo" puts back (or an older one ended), in the state the route saves - its
    # secrets only inside the route's own store transaction; undoing and the cleanup are in services.
    "tow.undo": {"invalidate", "stamp"},
    "tow.undo.snapshots": {"site_snapshot"},
    "tow.web_update": {"WebUpdateError"},
}

# Helper module of tow.web -> what it uses from outside the web beyond PURE, directly instead of
# through services: what they used when the rule came. May only shrink; a new need goes through
# tow.web.services (or, for a pure helper, into PURE with why).
HELPER_ALLOWED: dict[str, set[str]] = {
    "_context": {"tow.store.write_generation"},
    "middleware": {
        "tow.access.credential",
        "tow.access.session_is_valid",
        "tow.auth.SESSION_COOKIE",
        "tow.bind.origin_matches_request",
        "tow.i18n.remember_browser_language",
    },
    "site_form": {"tow.net_guard.is_public", "tow.trackers.generic.validate_tracker_regex"},
    "site_store": {"tow.store_transaction.transaction", "tow.undo.cleanup_needed"},
    "templating": {
        "tow.auth.SESSION_COOKIE",
        "tow.clients.factory.client_name",
        "tow.clock.parse_timestamp",
        "tow.config.port_of",
        "tow.i18n.datetime_pattern",
        "tow.i18n.texts_with_prefix",
        "tow.notifiers.summary",
        "tow.paths.launcher",
        "tow.platform.is_windows",
        "tow.status.site_check_tone",
        "tow.undo.can_undo",
        "tow.undo.stamp_of",
        "tow.undo.undo_just_made",
        "tow.undo.undo_label",
        "tow.undo.undo_left_sec",
    },
    "text": {"tow.i18n.fill", "tow.i18n.format_decimal"},
    "views": {
        "tow.adopt.unmarked_hash",
        "tow.backup_retention.retention_settings",
        "tow.check.PREVIOUS_REVISION_ACTIVE",
        "tow.episodes.expected_for_topic",
        "tow.errors.Msg",
        "tow.errors.render_stored",
        "tow.i18n.datetime_pattern",
        "tow.i18n.has",
        "tow.locations.free_bytes",
        "tow.locations.is_default",
        "tow.log.CLS_RU",
        "tow.net_errors.humanize",
        "tow.progress.progress_summary",
        "tow.snapshots.list_snapshots",
        "tow.snapshots.status",
        "tow.snapshots.status_failed",
        "tow.status.TRACKER_WARNING_CLASSES",
        "tow.status.project_home_status",
        "tow.trackers.presets.search_path",
    },
}
# Route module -> the other route modules it imports: what existed when the rule came; may only shrink.
ROUTE_IMPORTS_ALLOWED: dict[str, set[str]] = {
    "routes_home": {"routes_password"},
    "routes_settings": {"routes_service"},
}
# Not helpers: the package, the app factory and the gateway itself.
NOT_HELPERS = {"__init__", "app", "services"}


def _outside(module: str) -> bool:
    return (module == "tow" or module.startswith("tow.")) and not (module == "tow.web" or module.startswith("tow.web."))


def _is_module(dotted: str) -> bool:
    base = SRC.joinpath(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _outside_names(path: Path) -> list[tuple[str, str, int]]:
    """(module, name, line) for each name from outside ``tow.web`` the file uses: imported from
    its module, or used as an attribute of an imported module (``access.is_local``)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, str, int]] = []
    modules: dict[str, str] = {}  # local name -> outside module it stands for
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and node.module and _outside(node.module):
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if _is_module(full):
                    modules[alias.asname or alias.name] = full
                else:
                    found.append((node.module, alias.name, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _outside(alias.name):
                    if alias.asname:
                        modules[alias.asname] = alias.name
                    else:
                        found.append((alias.name, "*", node.lineno))  # import tow.x: a whole module
    found.extend(
        (modules[node.value.id], node.attr, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in modules
    )
    return found


def _route_modules() -> list[Path]:
    web = SRC / "tow" / "web"
    return sorted([*web.glob("routes_*.py"), *web.glob("*_actions.py")])


def _helper_modules() -> list[Path]:
    web = SRC / "tow" / "web"
    return sorted(
        path
        for path in web.glob("*.py")
        if path.stem not in NOT_HELPERS and not path.stem.startswith("routes_") and not path.stem.endswith("_actions")
    )


def _web_modules_imported(path: Path) -> set[str]:
    """The modules of tow.web (by their name in the package) the file imports, at any depth."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            if node.module == "tow.web":
                found |= {alias.name for alias in node.names if _is_module(f"tow.web.{alias.name}")}
            elif node.module.startswith("tow.web."):
                found.add(node.module.split(".")[2])
        elif isinstance(node, ast.ImportFrom) and node.level == 1:
            found |= {node.module.split(".")[0]} if node.module else {alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            found |= {alias.name.split(".")[2] for alias in node.names if alias.name.startswith("tow.web.")}
    return found


def test_route_modules_call_the_outside_only_through_services():
    unexpected = sorted(
        f"{path.relative_to(SRC).as_posix()}:{line} uses {module}.{name}"
        for path in _route_modules()
        for module, name, line in _outside_names(path)
        if name not in PURE.get(module, set())
    )
    assert unexpected == [], (
        "call it through tow.web.services (or, for a pure helper, add it to PURE with why):\n" + "\n".join(unexpected)
    )


def test_every_listed_helper_is_still_used():
    used = {(module, name) for path in _route_modules() for module, name, _line in _outside_names(path)}
    stale = sorted(f"{module}.{name}" for module, names in PURE.items() for name in names if (module, name) not in used)
    assert stale == [], "no route module uses these any more; remove them from PURE:\n" + "\n".join(stale)


def test_the_scan_sees_every_way_out(tmp_path, monkeypatch):
    package = tmp_path / "tow"
    (package / "web").mkdir(parents=True)
    for name in ("__init__.py", "store.py", "config.py", "web/__init__.py", "web/services.py"):
        (package / name).write_text("", encoding="utf-8")
    route = package / "web" / "routes_x.py"
    route.write_text(
        "from tow import store\n"
        "from tow.config import as_bool, load_config\n"
        "from tow.web import services\n"
        "import tow.config\n"
        "\n"
        "def page():\n"
        "    from tow.web_update import WebUpdateError\n"
        "    return store.load_state(), services.load_state()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(f"{__name__}.SRC", tmp_path)
    assert sorted(_outside_names(route)) == [
        ("tow.config", "*", 4),
        ("tow.config", "as_bool", 2),
        ("tow.config", "load_config", 2),
        ("tow.store", "load_state", 8),
        ("tow.web_update", "WebUpdateError", 7),
    ]


def _helper_uses() -> dict[str, set[str]]:
    """Helper module -> "module.name" it uses from outside the web that PURE does not cover."""
    return {
        path.stem: {
            f"{module}.{name}" for module, name, _line in _outside_names(path) if name not in PURE.get(module, set())
        }
        for path in _helper_modules()
    }


def test_helper_modules_reach_the_outside_only_through_services_or_their_ratchet():
    unexpected = sorted(
        f"tow/web/{helper}.py uses {name}"
        for helper, names in _helper_uses().items()
        for name in names - HELPER_ALLOWED.get(helper, set())
    )
    assert unexpected == [], (
        "call it through tow.web.services (or, for a pure helper, add it to PURE with why):\n" + "\n".join(unexpected)
    )


def test_every_helper_exception_is_still_used():
    uses = _helper_uses()
    stale = sorted(
        f"{helper}: {name}" for helper, names in HELPER_ALLOWED.items() for name in names - uses.get(helper, set())
    )
    assert stale == [], (
        "no longer used (or now in PURE); remove from HELPER_ALLOWED (it may only shrink):\n" + "\n".join(stale)
    )


def test_the_helper_scan_covers_the_helpers():
    helpers = {path.stem for path in _helper_modules()}
    assert {"_context", "middleware", "site_form", "site_store", "templating", "text", "views"} <= helpers
    assert set(HELPER_ALLOWED) <= helpers
    assert not any(stem.startswith("routes_") or stem in NOT_HELPERS for stem in helpers)


def _route_imports() -> dict[str, set[str]]:
    return {
        path.stem: {name for name in _web_modules_imported(path) if name.startswith("routes_") and name != path.stem}
        for path in sorted((SRC / "tow" / "web").glob("routes_*.py"))
    }


def test_a_route_module_does_not_import_another():
    found = _route_imports()
    unexpected = sorted(
        f"tow/web/{route}.py imports {other}"
        for route, others in found.items()
        for other in others - ROUTE_IMPORTS_ALLOWED.get(route, set())
    )
    assert unexpected == [], "share it through a helper module (views, site_store...):\n" + "\n".join(unexpected)
    stale = sorted(
        f"{route} -> {other}"
        for route, others in ROUTE_IMPORTS_ALLOWED.items()
        for other in others - found.get(route, set())
    )
    assert stale == [], "gone; remove from ROUTE_IMPORTS_ALLOWED (it may only shrink):\n" + "\n".join(stale)


def test_the_web_import_scan_sees_every_form(tmp_path, monkeypatch):
    web = tmp_path / "tow" / "web"
    web.mkdir(parents=True)
    for name in ("__init__", "routes_a", "routes_b", "routes_c", "routes_d", "routes_e", "views"):
        (web / f"{name}.py").write_text("", encoding="utf-8")
    route = web / "routes_x.py"
    route.write_text(
        "from tow.web import routes_a, views\n"
        "from tow.web.routes_b import router\n"
        "import tow.web.routes_c\n"
        "from . import routes_d\n"
        "def lazy():\n"
        "    from .routes_e import helper\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(f"{__name__}.SRC", tmp_path)
    assert _web_modules_imported(route) == {"routes_a", "routes_b", "routes_c", "routes_d", "routes_e", "views"}


def test_every_route_module_is_in_routers():
    app = importlib.import_module("tow.web.app")
    stems = sorted(path.stem for path in WEB.glob("routes_*.py"))
    routers = {stem: importlib.import_module(f"tow.web.{stem}").router for stem in stems}
    included = sorted(stem for stem, router in routers.items() if any(router is entry for entry in app.ROUTERS))
    assert included == stems, "add the router of every routes_*.py to tow.web.app.ROUTERS"
    assert len(app.ROUTERS) == len(stems), "ROUTERS holds a router twice, or one of no routes_*.py"
