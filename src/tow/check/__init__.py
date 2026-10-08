"""A check run: fetch every watched topic's current revision, hand it to its torrent client,
reconcile with the clients and commit the result.

- ``run``: the run itself, the commit of its result, the header's health record and the
  sites' daily download limits;
- ``topic``: one topic's check (fetch, identify, choose the files), ``apply`` what it then
  does in the client;
- ``client_ops``: what is asked of a client (hash identity, TOW's ownership mark, read-back
  confirmation, relocation, other topics' claims) and the clients one run talks to;
- ``reconcile``: progress and presence in the clients, and the download history's commit;
- ``space``: a revision that does not fit on the target drive waits in its client, stopped,
  and is started once there is room;
- ``notices``: the messages a run sends; ``rows``: a topic's result row.
"""

from tow.check.apply import FREE_SPACE_MARGIN, free_space_problem
from tow.check.client_ops import (
    PREVIOUS_REVISION_ACTIVE,
    await_relocation,
    blocked_by_previous_revision,
    client_owned_by_tow,
)
from tow.check.run import record_check_failure, run_check

__all__ = [
    "FREE_SPACE_MARGIN",
    "PREVIOUS_REVISION_ACTIVE",
    "await_relocation",
    "blocked_by_previous_revision",
    "client_owned_by_tow",
    "free_space_problem",
    "record_check_failure",
    "run_check",
]
