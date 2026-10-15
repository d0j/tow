"""The torrent clients that do not answer now, from saved health: one rule for the header, the
Settings cards and "Needs attention" (the ``client_down`` kind, and ``topic_error``, which leaves
their failed topics to it)."""

from __future__ import annotations

from collections.abc import Mapping

from tow.clients import factory
from tow.issues.model import Snapshot


def down(snapshot: Snapshot) -> tuple[str, ...]:
    """The configured clients that did not answer, the main one first: the main one by the last
    check (``qbit_ok``), another one by its own word from a check (``clients_ok``), and any of them
    by Settings' "Check" since then (``ping_failed``: a mark the next check drops, the header's
    evidence too). A client no longer configured is not said."""
    cfg = dict(snapshot.config)
    default = factory.default_client_id(cfg)
    others = [str(row["id"]) for row in factory.client_configurations(cfg) if str(row["id"]) != default]
    health = snapshot.health
    raw_marks = health.get("ping_failed")
    marks = raw_marks if isinstance(raw_marks, Mapping) else {}
    raw_answers = health.get("clients_ok")
    answers = raw_answers if isinstance(raw_answers, Mapping) else {}
    found = [default] if health.get("qbit_ok") is False or default in marks else []
    found += [client for client in others if answers.get(client) is False or client in marks]
    return tuple(found)
