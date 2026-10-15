"""Current topic failures not already represented by a site sign-in or dead client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tow import i18n
from tow.clients.factory import default_client_id
from tow.errors import as_value, summary_record
from tow.issues.model import Fix, Issue, Message, Snapshot, Target
from tow.log import error_class
from tow.records import shown_title
from tow.status import TRACKER_WARNING_CLASSES, error_tone

KIND = "topic_error"
ORDER = 70
# The short site incidents of ``tow.errors.summary_record``: one entry per site, in their own words.
_SITE_MESSAGES = {"mirrors.access_retry": "issues.site_access", "mirrors.connection_retry": "issues.site_connection"}


def _error(topic: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    code = topic.get("last_error_code")
    raw = topic.get("last_error_params")
    params = dict(raw) if isinstance(raw, Mapping) else {}
    name = params.get("tracker")
    sites = config.get("trackers")
    spec = sites.get(name) if isinstance(sites, Mapping) and isinstance(name, str) else None
    if isinstance(spec, Mapping):
        params["tracker"] = spec.get("title") or name
    known = isinstance(code, str) and i18n.has(code)
    return as_value(
        summary_record({"code": code, "params": params}) if known else None, str(topic.get("last_error") or "")
    )


def collect(snapshot: Snapshot) -> list[Issue]:
    default = default_client_id(dict(snapshot.config))
    found = []
    for topic in snapshot.topics:
        identity = str(topic.get("id") or "")
        if not identity or topic.get("paused") or not topic.get("last_error"):
            continue
        cls = str(topic.get("last_error_class") or error_class(str(topic["last_error"]), topic.get("last_error_code")))
        if topic.get("last_error_class") == "tracker_auth":
            continue
        if (
            cls == "qbit"
            and snapshot.health.get("qbit_ok") is False
            and str(topic.get("client_id") or default) == default
        ):
            continue
        site = (topic.get("last_error_params") or {}).get("tracker")
        sites = snapshot.config.get("trackers") or {}
        normalized = summary_record({"code": topic.get("last_error_code"), "params": topic.get("last_error_params")})
        if (
            cls in TRACKER_WARNING_CLASSES
            and isinstance(site, str)
            and site in sites
            and normalized
            and normalized.get("code") in _SITE_MESSAGES
        ):
            title = sites[site].get("title") or site
            target = Target("home", open=identity, anchor=f"row-{identity}")
            found.append(
                Issue(
                    kind=KIND,
                    id=f"{KIND}:site:{site}",
                    severity="warn",
                    # No connection or limited access: one entry per site (the first topic decides
                    # the words when its topics differ).
                    message=Message(_SITE_MESSAGES[normalized["code"]], {"site": title, "count": 1}),
                    subjects=(identity,),
                    target=target,
                    target_many=Target(
                        "home", open=identity, anchor=f"row-{identity}", filter=(("f", "problem"), ("t", site))
                    ),
                    fix=Fix("check_topic", identity, label="site_access.retry_check"),
                )
            )
            continue
        found.append(
            Issue(
                kind=KIND,
                id=f"{KIND}:{identity}",
                severity=error_tone(cls),
                message=Message(
                    "issues.topic_error",
                    {"title": shown_title(topic) or identity, "error": _error(topic, snapshot.config)},
                ),
                subjects=(identity,),
                target=Target("home", open=identity, anchor=f"row-{identity}"),
            )
        )
    return found
