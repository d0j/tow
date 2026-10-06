from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import urljoin

import httpx

from tow import http as thttp
from tow.clients import factory as client_factory
from tow.config import as_bool, load_config
from tow.http import client as http_client
from tow.http import is_cloudflare
from tow.i18n import t
from tow.jsonish import as_dict
from tow.log import owner_language
from tow.mirrors import origin_key
from tow.store import load_secrets, load_state, persistence_lock, save_state

_PROBE_MAX_BYTES = thttp.MAX_HTML_RESPONSE_BYTES
_REDIRECT_CODES = {301, 302, 303, 307, 308}


def _probe_root(client: httpx.Client, host: str) -> httpx.Response:
    url = host.rstrip("/") + "/"
    origin = origin_key(host)
    for _ in range(4):
        response = thttp.get_limited(client, url, max_bytes=_PROBE_MAX_BYTES)
        if response.status_code not in _REDIRECT_CODES:
            return response
        location = response.headers.get("location") or ""
        if not location:
            raise RuntimeError("redirect without Location")
        next_url = urljoin(str(response.url), location)
        if origin_key(next_url) != origin:
            raise RuntimeError("redirect outside configured mirror")
        url = next_url
    raise RuntimeError("redirect limit exceeded")


def _inventory(cfg: dict[str, Any], secrets: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    from tow.clients.factory import client_configurations, client_secret_block

    try:
        qbit_host_set = any(
            bool(client_secret_block(cfg, secrets, str(row["id"]).strip()).get("host"))
            for row in client_configurations(cfg)
        )
    except RuntimeError, ValueError:
        qbit_host_set = bool((secrets.get("qbittorrent") or {}).get("host"))
    return {
        "python": _py(),
        "trackers": list((cfg.get("trackers") or {}).keys()),
        "qbit_host_set": qbit_host_set,
        "notify_set": bool(_connected_messengers(secrets)),
        "topics": len(state.get("topics") or []),
        "qbit": None,
        "probes": [],
        "autostart": _autostart(),
        "open_folders": _open_folders(),
        "ok": True,
    }


def _open_folders() -> list[str]:
    """keys/ and data/ by name when other accounts of this computer can open them."""
    from tow.platform import private_folders
    from tow.store import install_folders

    try:
        return [folder.name for folder in private_folders(install_folders(), repair=False)]
    except OSError, RuntimeError:
        return []


def _autostart() -> dict[str, Any]:
    """The autostart of this OS (task "TOW", tow.service or io.tow), read back; never raises."""
    from tow.autostart import backend

    try:
        return backend().status()
    except Exception:  # noqa: BLE001 - a diagnostic: a failed read-back is the report's finding
        return {"on": False, "error": t("settings.service.autostart_unread")}


def _connected_messengers(secrets: dict[str, Any]) -> list[Any]:
    from tow.notifiers import connected

    return connected(secrets)


def doctor_report(*, probe: bool = True, names: list[str] | None = None) -> dict[str, Any]:
    state = load_state()
    if not probe:
        cached = state.get("doctor")
        if isinstance(cached, dict) and cached.get("probes") is not None:
            report = deepcopy(cached)
            inventory = _inventory(load_config(), load_secrets(), state)
            # Only network observations are cached. Local facts can change after an
            # update or settings edit without a new client/site probe.
            report.update({key: value for key, value in inventory.items() if key not in {"qbit", "probes", "ok"}})
            return report
        cfg = load_config()
        return _inventory(cfg, load_secrets(), state)
    cfg = load_config()
    secrets = load_secrets()
    trackers = cfg.get("trackers") or {}
    want = {str(n) for n in names} if names else None
    out: dict[str, Any] = _inventory(cfg, secrets, state)
    if want is None:
        try:
            out["qbit"] = client_factory.from_secrets(cfg, secrets).ping()
        except Exception as e:  # noqa: BLE001 - a diagnostic: any failure is the report's finding
            out["qbit"] = f"FAIL {e}"
            out["ok"] = False
    ua = cfg.get("user_agent")
    new_probes: list[dict[str, Any]] = []
    with http_client(
        ua=ua,
        follow_redirects=False,
        public_only=not as_bool(cfg.get("allow_private_tracker_hosts")),
    ) as c:
        for name, spec in trackers.items():
            if want is not None and name not in want:
                continue
            for host in spec.get("fetch_hosts") or []:
                row = {"tracker": name, "host": host, "ok": False}
                try:
                    r = _probe_root(c, host)
                    try:
                        text = r.text[:800]
                    except LookupError, ValueError:  # an unknown or wrong charset: read as bytes
                        text = r.content[:800].decode("latin-1", "replace")
                    if is_cloudflare(r.status_code, text):
                        row["error"] = "cloudflare"
                        out["ok"] = False
                    elif r.status_code >= 400:
                        row["error"] = f"http {r.status_code}"
                        out["ok"] = False
                    else:
                        row["ok"] = True
                        row["status"] = r.status_code
                except Exception as e:  # noqa: BLE001 - a diagnostic: any failure is the report's finding
                    row["error"] = str(e)
                    out["ok"] = False
                new_probes.append(row)
    with persistence_lock():
        disk = load_state()
        if want is not None:
            prev = as_dict(disk.get("doctor"))
            kept = [
                p
                for p in (prev.get("probes") or [])
                if isinstance(p, dict) and str(p.get("tracker")) not in want and str(p.get("tracker")) in trackers
            ]
            out["probes"] = kept + new_probes
            if "qbit" in prev:
                out["qbit"] = prev["qbit"]
            for k, v in prev.items():
                if k not in ("probes", "trackers", "ok", "degraded"):
                    out.setdefault(k, v)
        else:
            out["probes"] = new_probes
        _summarize(out)
        disk["doctor"] = out
        save_state(disk)
    return out


def _summarize(report: dict[str, Any]) -> None:
    """``ok`` over the whole report: qBit answers and every probed tracker has a working
    mirror. One dead mirror among working ones is "degraded", not a failure - mirrors
    exist for exactly that. A partial run used to judge only its own new probes."""
    by_tracker: dict[str, bool] = {}
    for probe in report.get("probes") or []:
        name = str(probe.get("tracker"))
        by_tracker[name] = by_tracker.get(name, False) or bool(probe.get("ok"))
    qbit_ok = not str(report.get("qbit") or "").startswith("FAIL")
    report["ok"] = qbit_ok and all(by_tracker.values())
    report["degraded"] = sorted(
        f"{probe.get('tracker')} {probe.get('host')}" for probe in report.get("probes") or [] if not probe.get("ok")
    )


def doctor_text(report: dict[str, Any] | None = None) -> str:
    """The report in plain lines (printed, and sent to the messengers with ``--notify``)."""
    r = report or doctor_report()
    lang = owner_language()
    degraded = r.get("degraded") or []
    verdict = t("doctor_report.yes", lang) if r.get("ok") else t("doctor_report.no", lang)
    lines = [
        t("doctor_report.ok", lang, value=verdict)
        + (t("doctor_report.degraded", lang, count=len(degraded)) if degraded else ""),
        t("doctor_report.python", lang, value=r.get("python")),
        t("doctor_report.trackers", lang, value=r.get("trackers")),
        t("doctor_report.qbit_host_set", lang, value=r.get("qbit_host_set")),
        t("doctor_report.qbit_ping", lang, value=r.get("qbit")),
        t("doctor_report.messengers", lang, value=r.get("notify_set")),
        t("doctor_report.topics", lang, value=r.get("topics")),
    ]
    if r.get("open_folders"):
        lines.append(t("doctor_report.open_folders", lang, value=", ".join(map(str, r["open_folders"]))))
    for p in r.get("probes") or []:
        mark = t("doctor_report.probe_ok", lang) if p.get("ok") else p.get("error")
        lines.append(t("doctor_report.probe", lang, tracker=p.get("tracker"), host=p.get("host"), result=mark))
    return "\n".join(lines)


def _py() -> str:
    import sys

    return sys.version.split()[0]
