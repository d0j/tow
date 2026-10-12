"""Download progress: what a topic's torrent shows in its client and on disk, kept in the history.

The submodules, in the order the reconcile pass uses them: ``confirmation`` (a file on disk),
``labels`` (events and their wording), ``presence`` (the torrent in its client, superseded
revisions), ``expected`` (the selected files and the episodes expected), ``history`` (the index
and identity of history items), ``summary`` and ``reconcile`` (the pass itself).
"""

from __future__ import annotations

from tow.progress.confirmation import CONFIRMATION_REUSE_SEC, filesystem_confirmation, safe_relative_path
from tow.progress.reconcile import reconcile_topic
from tow.progress.summary import progress_summary

__all__ = [
    "CONFIRMATION_REUSE_SEC",
    "filesystem_confirmation",
    "progress_summary",
    "reconcile_topic",
    "safe_relative_path",
]
