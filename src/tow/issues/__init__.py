"""What needs the owner now, from saved state: one list for every place that says so.

Home's "Needs attention" banner and ``tow status`` show the same issues. ``collect(snapshot)``
asks every kind module of ``tow.issues.kinds`` (one file per kind, found by ``tow.plugins.scan``)
and returns their issues, worst first; it reads no file itself: the caller passes the state, the
config and the few facts kept elsewhere in a ``Snapshot``. An issue says where it is shown in full
(``Target``) and which control fixes it (``Fix``); turning those into a link and a focused
button is the web's job.

Kept apart from ``tow.doctor`` (probes on request) and ``tow.watchdog`` (messages to the
messengers when something changes): issues ask nothing and send nothing.
"""

from __future__ import annotations

from tow.issues.collect import NOTE_KINDS, SKIPPED, Kind, attention, collect, known_kinds
from tow.issues.model import SEVERITIES, Fix, Issue, Message, Snapshot, Target, fingerprint, worst

__all__ = [
    "NOTE_KINDS",
    "SEVERITIES",
    "SKIPPED",
    "Fix",
    "Issue",
    "Kind",
    "Message",
    "Snapshot",
    "Target",
    "attention",
    "collect",
    "fingerprint",
    "known_kinds",
    "worst",
]
