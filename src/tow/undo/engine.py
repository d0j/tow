"""The undo engine: one record, one registry of kinds, one way to put a change back.

Every change the owner can undo stamps one record into state.json (``stamp``); a newer change
replaces it. ``apply`` puts the last change back through one pipeline for every kind:

1. the record must still be live (the "Undo" bar's time) and complete for its kind;
2. its encrypted secret snapshot, if it has one, is read and checked for its kind;
3. the kind restores what it changed (``tow.undo.kinds``) into new contents of the stores; an
   outside effect it needs first (the torrent client moving files back)
   happens here, and a refusal writes nothing;
4. config, state, secrets and the snapshot's removal are written as one transaction
   (``tow.store_transaction``): a failure or a crash leaves every store as it was;
5. what was said to the owner is what happened: done, not applied (and why), or "rollback
   incomplete" when even the restore failed.

The secret snapshot (secrets-undo.enc) holds old passwords and tokens, so it never outlives its
record: it goes when the undo is applied, when it expires, and when a change without secrets
replaces it. A removal that fails is noted in state.json (``secret_undo_cleanup_pending``) and
retried before later requests (``cleanup``).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tow import store, store_transaction
from tow.clock import iso_now, parse_timestamp
from tow.config import flash_ttl, load_config
from tow.log import error_class, log_event, owner_language
from tow.undo.records import secret_refs

PENDING = "secret_undo_cleanup_pending"
_HINT_SEC = 15  # the "you can undo" hint belongs to the page right after the action
_MAX_CLEANUP_ATTEMPTS = 3


@dataclass(frozen=True)
class Outcome:
    """What the owner is told, and where: a catalog key (with its values) and its kind."""

    message: str
    kind: str = "ok"  # ok | warn | err
    target: str = "/"
    params: dict[str, Any] = field(default_factory=dict)
    signed_out: bool = False  # every device signs in again (the password's key changed)
    note: str = ""  # a catalog key said after the message

    def text(self) -> str:
        return t(self.message, **self.params) + (t(self.note) if self.note else "")


class Refused(Exception):
    """Raised by a kind before anything is written: the undo stays for another try."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.message)
        self.outcome = outcome


@dataclass
class Context:
    """What a kind gets: its record, a copy of state.json to change, the snapshot, the requester."""

    record: dict[str, Any]
    state: dict[str, Any]
    snapshot: dict[str, Any] | None
    local: bool


@dataclass
class Restore:
    """A kind's answer: the stores' new contents and what to say."""

    done: Outcome
    config: dict[str, Any] | None = None
    interval_sec: int | None = None  # set in place in config.yaml (comments kept)
    flash_ttl_sec: int | None = None
    secrets: dict[str, Any] | None = None
    failed: Outcome | None = None  # said when the transaction failed (default: the kind's)
    compensate: Callable[[], bool] | None = None  # undoes an outside effect; False if it could not
    after_commit: Callable[[], object] | None = None
    after_commit_failed: str = ""  # a catalog key said when ``after_commit`` failed
    event: tuple[str, dict[str, Any]] | None = None


@dataclass(frozen=True)
class Kind:
    """One kind of undo: how to check, restore and name its record."""

    restore: Callable[[Context], Restore]
    valid: Callable[[dict[str, Any]], bool]
    label: Callable[[dict[str, Any]], str]
    target: str = "/"
    snapshot: Callable[[object, dict[str, Any]], bool] | None = None  # is this its snapshot?
    secrets_failed: Outcome | None = None
    failed: str = "web.undo.apply_failed"


KINDS: dict[str, Kind] = {}  # filled by tow.undo.kinds


def t(key: str, /, **params: Any) -> str:
    from tow import i18n

    return i18n.translate(key, owner_language(), **params)


# --------------------------------------------------------------------------- the undo window


def ttl_sec() -> int:
    """How long the "Undo" bar stays (config ``flash_ttl_sec``)."""
    try:
        return flash_ttl(load_config().get("flash_ttl_sec"))
    except Exception:  # noqa: BLE001 - a broken config must not break the undo bar; the default time applies
        return flash_ttl(None)


def _seconds_left(record: object) -> float | None:
    if not isinstance(record, dict) or not record.get("kind") or not record.get("ts"):
        return None
    try:
        stamped = parse_timestamp(str(record["ts"]))  # aware; naive means local time, as everywhere in TOW
    except TypeError, ValueError:
        return None
    return ttl_sec() - (datetime.now(UTC) - stamped).total_seconds()


def is_live(record: object) -> bool:
    left = _seconds_left(record)
    return left is not None and left > 0


def _record(state: dict[str, Any] | None) -> object:
    return (store.load_state() if state is None else state).get("undo")


def can_undo(state: dict[str, Any] | None = None) -> bool:
    """The "Undo" bar is shown (``state``: state.json as already read; None reads it)."""
    return is_live(_record(state))


def undo_left_sec(state: dict[str, Any] | None = None) -> int:
    left = _seconds_left(_record(state))
    return max(0, int(left)) if left is not None and left > 0 else 0


def undo_just_made(state: dict[str, Any] | None = None) -> bool:
    """The undo belongs to the action whose result is shown now: its hint must not stick to
    unrelated messages for the rest of the undo window."""
    record = _record(state)
    left = _seconds_left(record)
    return left is not None and left > 0 and left >= ttl_sec() - _HINT_SEC


def undo_label(state: dict[str, Any] | None = None) -> str:
    """What "Undo" will put back, in words (the button's tooltip)."""
    record = _record(state)
    kind = KINDS.get(str(record.get("kind") or "")) if isinstance(record, dict) else None
    if kind is None or not isinstance(record, dict):
        return t("web.undo.last")
    try:
        return kind.label(record)
    except Exception:  # noqa: BLE001 - a label must never break a page; the generic label is shown
        return t("web.undo.last")


# --------------------------------------------------------------------------- stamping


def _cleanup_marker(reference: str, exc: BaseException, attempts: int = 1) -> dict[str, Any]:
    return {"reference": reference, "attempts": attempts, "last_error": error_class(exc), "ts": iso_now()}


def release(
    state: dict[str, Any],
    record: object,
    txn: store_transaction.StoreTransaction | None = None,
    *,
    how: str = "manual",
) -> list[str]:
    """Remove the secret snapshot ``record`` points at (inside ``txn`` when given: it is gone
    with the transaction, or back with it). A removal that fails is noted in ``state`` for the
    retry; returns the references it could not remove yet."""
    postponed = []
    for reference in secret_refs(record):
        try:
            if txn is not None:
                txn.delete_secret_undo(reference)
            else:
                store.delete_secret_undo(reference)
        except Exception as exc:  # noqa: BLE001 - a failed cleanup is recorded in the state and retried later
            state[PENDING] = _cleanup_marker(reference, exc)
            postponed.append(reference)
            log_event("secret_undo_cleanup_fail", status="pending", error=error_class(exc), how=how)
    return postponed


def stamp(
    state: dict[str, Any],
    /,
    kind: str,
    *,
    txn: store_transaction.StoreTransaction | None = None,
    snapshot: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    """Make ``kind`` the change "Undo" puts back; it replaces the previous one.

    ``snapshot`` (the secrets the change replaces) is written encrypted inside ``txn``, over the
    previous snapshot. A change without secrets removes the previous record's snapshot instead:
    nothing points at it any more.
    """
    previous = state.get("undo")
    record: dict[str, Any] = {"kind": kind, **fields}
    if snapshot is not None:
        if txn is None:
            raise ValueError("a secret undo snapshot is written inside a store transaction")
        record["secret_undo_ref" if kind == "site" else "secrets_undo_ref"] = txn.save_secret_undo(snapshot)
    references = secret_refs(record)
    if references:
        pending = state.get(PENDING)
        if isinstance(pending, dict) and pending.get("reference") in references:
            state.pop(PENDING)  # the file holds the new snapshot now: nothing left to remove
    elif secret_refs(previous):
        release(state, previous, txn)
    record["ts"] = datetime.now(UTC).isoformat()
    state["undo"] = record


def invalidate(state: dict[str, Any], txn: store_transaction.StoreTransaction | None = None) -> bool:
    """A later, independent change must not leave an older undo actionable; True if one was."""
    record = state.pop("undo", None)
    if record is None:
        return False
    release(state, record, txn)
    return True


# --------------------------------------------------------------------------- cleanup


def cleanup_needed(state: dict[str, Any] | None = None) -> bool:
    """Cheap test (no lock) whether ``cleanup`` has work: a removal to retry, or an expired
    undo still holding secrets."""
    try:
        state = store.load_state(quarantine=False) if state is None else state
    except Exception:  # noqa: BLE001 - a request is never failed by this pre-check; the next one retries
        return False
    pending = state.get(PENDING)
    if isinstance(pending, dict) and pending.get("reference") and _attempts(pending) < _MAX_CLEANUP_ATTEMPTS:
        return True
    record = state.get("undo")
    return bool(secret_refs(record)) and not is_live(record)


def _attempts(pending: dict[str, Any]) -> int:
    try:
        return int(pending.get("attempts") or 0)
    except TypeError, ValueError:
        return 0


def cleanup() -> bool:
    """Remove secret snapshots nothing needs any more; True when a postponed removal succeeded.

    An expired undo goes with its snapshot. A removal noted as pending is retried (at most three
    times), unless the live undo points at that snapshot again.
    """
    with store.persistence_lock():
        state = store.load_state()
        changed = False
        record = state.get("undo")
        if secret_refs(record) and not is_live(record):
            state.pop("undo")
            release(state, record, how="automatic")
            changed = True
            log_event("undo_expired", how="automatic")
        done = False
        pending = state.get(PENDING)
        reference = str(pending.get("reference") or "") if isinstance(pending, dict) else ""
        attempts = _attempts(pending) if isinstance(pending, dict) else 0
        in_use = reference in secret_refs(state.get("undo"))
        if reference and not in_use and attempts < _MAX_CLEANUP_ATTEMPTS and isinstance(pending, dict):
            try:
                store.delete_secret_undo(reference)
            except Exception as exc:  # noqa: BLE001 - a failed cleanup retry is recorded and logged; tried again later
                state[PENDING] = _cleanup_marker(reference, exc, attempts + 1)
                log_event("secret_undo_cleanup_retry_fail", status="pending", error=error_class(exc), how="automatic")
            else:
                state.pop(PENDING)
                done = True
                log_event("secret_undo_cleanup_retry", status="succeeded", how="automatic")
            changed = True
        if changed:
            store.save_state(state)
        return done


# --------------------------------------------------------------------------- applying


def _load_snapshot(kind: Kind, record: dict[str, Any]) -> dict[str, Any] | None:
    references = secret_refs(record)
    if kind.snapshot is None or not references:
        return None
    snapshot = store.load_secret_undo(references[0])
    if not kind.snapshot(snapshot, record):
        raise store.SecretStoreError("the secret undo snapshot is not this undo's")
    return snapshot


def _write(restore: Restore, state: dict[str, Any], record: dict[str, Any]) -> list[str]:
    """Every store of the undo as one transaction; returns the snapshots left for the retry."""
    with store_transaction.transaction() as txn:
        if restore.secrets is not None:
            txn.save_secrets(restore.secrets)
        if restore.config is not None:
            txn.save_config(restore.config)
        if restore.interval_sec is not None:
            txn.set_interval_sec(restore.interval_sec)
        if restore.flash_ttl_sec is not None:
            txn.set_flash_ttl_sec(restore.flash_ttl_sec)
        state.pop("undo", None)
        postponed = release(state, record, txn)  # the snapshot goes with the undo it belonged to
        txn.save_state(state)
    return postponed


def _expired(state: dict[str, Any], record: dict[str, Any]) -> Outcome:
    state.pop("undo", None)
    release(state, record)  # the window is over: its saved secrets go too
    store.save_state(state)
    log_event("undo_expired", how="manual")
    return Outcome("web.undo.expired", "warn")


def _nothing(state: dict[str, Any], record: object, target: str = "/") -> Outcome:
    if state.pop("undo", None) is not None:
        release(state, record)
        store.save_state(state)
    return Outcome("web.undo.nothing", "warn", target)


def apply(*, local: bool = True) -> Outcome:
    """Put the last change back (``local``: the request comes from the computer running TOW)."""
    with store.persistence_lock():
        state = store.load_state()
        record = state.get("undo")
        if not isinstance(record, dict) or not record:
            return _nothing(state, record)
        if not is_live(record):
            return _expired(state, record)
        log_event("undo", how="manual")
        kind = KINDS.get(str(record.get("kind")))
        if kind is None:
            return _nothing(state, record)
        if not kind.valid(record):
            return _nothing(state, record, kind.target)
        try:
            context = Context(record, copy.deepcopy(state), _load_snapshot(kind, record), local)
            restore = kind.restore(context)
        except Refused as refused:
            return refused.outcome  # nothing written: the undo stays for another try
        except store.SecretStoreError:
            return kind.secrets_failed or Outcome("web.undo.secrets_failed", "err", kind.target)
        return _commit(kind, record, restore, context.state)


def _commit(kind: Kind, record: dict[str, Any], restore: Restore, state: dict[str, Any]) -> Outcome:
    try:
        postponed = _write(restore, state, record)
    except store_transaction.TransactionError as exc:
        compensated = True
        if restore.compensate is not None:
            try:
                compensated = restore.compensate()
            except Exception:  # noqa: BLE001 - a failed compensation is reported as an incomplete rollback
                compensated = False
        incomplete = isinstance(exc, store_transaction.RollbackError) or not compensated
        status = "rollback_incomplete" if incomplete else "restored"
        log_event("undo_fail", undo=record.get("kind"), status=status, error=error_class(exc.__cause__ or exc))
        if incomplete:
            return Outcome("web.common.rollback_incomplete", "err", kind.target)
        return restore.failed or Outcome(kind.failed, "err", kind.target)
    done = restore.done
    note = "web.undo.cleanup_later" if postponed else ""  # done; its secrets go a little later
    if restore.after_commit is not None:
        try:
            restore.after_commit()
        except Exception as exc:  # noqa: BLE001 - the undo is committed; a failed follow-up is logged and named in the message
            log_event("undo_fail", undo=record.get("kind"), status="after_commit", error=error_class(exc))
            note = restore.after_commit_failed or note
    if restore.event is not None:
        name, fields = restore.event
        log_event(name, **fields, how="manual")
    if note:
        return Outcome(done.message, "warn", done.target, done.params, done.signed_out, note)
    return done
