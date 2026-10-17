"""One state on every open page: what ``GET /sync.json`` and ``/topics/rows`` answer
(``routes_sync``), and how it stays cheap and read-only.

Sources. The install's files a page shows are sampled by one shared cycle, at most once per
``SAMPLE_SEC`` for every tab and device (``Sampler``), or at once after this process wrote one.
A sample reads each file whole, without a lock, and keeps its bytes' SHA-256: a stamp (mtime,
size, file id) is only a hint, the digest decides (``tow.sync_sources``). The pages' own parsed
caches are keyed by stamps: a digest that changed under the same stamp drops them, so no page
shows the older content either. A missing file is a fact (its default); a file there but not
readable now keeps the last good reading, if any; a damaged one, or a store transaction in
progress (its journal), is "unavailable" - never a mix of old and new stores.

Areas. Each area is a projection of what pages show of it - built from one sample, in the
request's language and access - and its revision is the SHA-256 (128 bits, 32 hex digits) of
that projection with the area, the projection's ``SCHEMA`` and that context. A revision is an
opaque token: equal means the same content, never "newer". What changes by the clock alone is
recomputed at its moment (``deadline``): an Undo that runs out, a check that becomes late in
"Needs attention", a topic timer that becomes due, a date label at midnight. What a page counts
down itself (the timers, the Undo's seconds) is published as a fixed moment, never as "now".

Area        Built from (sources)                                             Clock
topics      state, download history, config, supervisor status; language     midnight (date labels)
jobs        the manual check queue of this server (generation)               -
header      state, config, secrets, supervisor status and schedule; language -
attention   state, config, supervisor status and schedule; language          the stale-check moment
timers      state, supervisor status and schedule                            the next due timer
messages    the event log (its tail marker), config; language                (feed: P3)
undo        state, config, secrets; language, access                         its end
config      config; the request's language                                   -
version     the code and the static files                                    -
release     the published release answer, the update job                     -
page        Sites, Settings: the digests of the fragments the page draws (tow.web.page_sync),
            drawn when config, state, secrets, status or schedule (Settings: also the backup
            inventory, the service) changed - a write that changes nothing drawn, none; History:
            event log, config, names, its filters; language, access              midnight
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets as random_tokens
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request

from tow import __version__, access, i18n
from tow.auth import SESSION_COOKIE
from tow.clock import local_zone, parse_timestamp
from tow.config import flash_ttl, interval_sec_of
from tow.records import shown_title
from tow.store import SecretStoreError
from tow.web import _context, check_jobs, page_sync, services

SCHEMA = 1
# A file another process wrote (a scheduled check, the supervisor) reaches a page within one
# poll (5 s) plus one sample: at most 6 s. A sample of 2,000 topics costs about 7 ms.
SAMPLE_SEC = 1.0
REV_HEX = 32  # 128 bits of the SHA-256
AREAS = ("topics", "jobs", "header", "attention", "timers", "messages", "undo", "config", "version", "release", "page")
PAGES = ("/", "/sites", "/settings", "/history")
RING_SIZE = 16
MAX_IDS = 200
ROWS_BYTE_CAP = 1024 * 1024  # row HTML per /topics/rows answer; the rest is "deferred"
MAX_HAVE_CHARS = 1024
TOPIC_ID = re.compile(r"[A-Za-z0-9._~:-]{1,128}")
REVISION = re.compile(rf"[0-9a-f]{{{REV_HEX}}}")
STORE_LIMIT = 256 * 1024 * 1024
SMALL_LIMIT = 4 * 1024 * 1024
RETRY_AFTER_SEC = 2
# A rate per session (a device on the network) or per tab of this computer: a burst for a Home
# of 2,000 topics refreshed in ten batches, then a few requests a second.
RATE_BURST = 40.0
RATE_PER_SEC = 4.0
MAX_BUCKETS = 1024
# What one server process is: an ETag of an earlier process never matches.
SERVER_EPOCH = random_tokens.token_hex(8)


class Unavailable(Exception):
    """The data cannot be shown consistently now: ``reason`` "recovery" (a store transaction is
    being written or waits for recovery), "busy" (the stores kept changing while read), "data" (a
    damaged or unreadable store), "config" (config.yaml)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()


def revision(area: str, value: Any, ctx: Mapping[str, Any] | None = None) -> str:
    """The opaque revision of ``value`` (an area's projection) in ``ctx``."""
    payload = _canonical({"area": area, "schema": SCHEMA, "ctx": dict(ctx or {}), "value": value})
    return hashlib.sha256(payload).hexdigest()[:REV_HEX]


# --------------------------------------------------------------------------- the shared sample


@dataclass(frozen=True)
class Sample:
    """One consistent reading of the install's files (never changed once taken)."""

    seq: int
    taken: float  # monotonic
    wall: float
    write_generation: int
    digests: Mapping[str, str]
    state: dict[str, Any]
    history: dict[str, Any]
    config: dict[str, Any]
    secrets: tuple[bool, Any]  # (True, secrets) or (False, the SecretStoreError)
    running: bool
    paths: Mapping[str, Any]
    readings: Mapping[str, Any] = field(default_factory=dict)
    update: Mapping[str, Any] = field(default_factory=dict)

    def key(self, *names: str) -> tuple[str, ...]:
        return tuple(self.digests.get(name, "") for name in names)

    def seeded(self) -> Any:
        """The request context's readers answer from this sample (``_context.seeded``)."""
        return _context.seeded(
            {
                "config": (True, self.config),
                "state": (True, self.state),
                "history": (True, self.history),
                "secrets": self.secrets,
            }
        )


class Sampler:
    """One sample at a time for every request; a new one once ``period`` has passed (monotonic
    clock: a wall clock set back changes nothing) or this process wrote a file. Its lock is its
    own: never the persistence, check or log lock."""

    def __init__(self, *, period: float = SAMPLE_SEC, clock: Callable[[], float] = time.monotonic) -> None:
        self._period = period
        self._clock = clock
        self._lock = threading.Lock()
        self._sample: Sample | None = None
        self._seq = 0
        self._seen: dict[str, str] = {}

    def forget(self) -> None:
        with self._lock:
            self._sample = None
            self._seen.clear()

    def _fresh(self, sample: Sample | None) -> bool:
        return (
            sample is not None
            and self._clock() - sample.taken < self._period
            and sample.write_generation == services.write_generation()
            and sample.paths.get("state") == services.sync_source_paths()["state"]  # the same install
        )

    def current(self) -> Sample:
        sample = self._sample
        if self._fresh(sample):
            assert sample is not None
            return sample
        with self._lock:
            if not self._fresh(self._sample):
                self._sample = self._take(self._sample)
            assert self._sample is not None
            return self._sample

    def _take(self, previous: Sample | None) -> Sample:
        generation = services.write_generation()
        for _attempt in range(3):
            paths = services.sync_source_paths()
            # Every transaction's marker (the site settings', a check's commit, an import or a
            # restore point, a night-copy restore): while one is there the stores may not belong
            # together, and its recovery is the data lock's (``recover_later``), never the poll's.
            if services.sync_recovery_pending():
                raise Unavailable("recovery")
            readings = {
                "state": services.sync_read(paths["state"], STORE_LIMIT),
                "history": services.sync_read(paths["history"], STORE_LIMIT),
                "config": services.sync_read(paths["config"], SMALL_LIMIT * 4),
                "secrets": services.sync_read(paths["secrets"], SMALL_LIMIT),
                "status": services.sync_read(paths["status"], SMALL_LIMIT),
                "schedule": services.sync_read(paths["schedule"], SMALL_LIMIT),
            }
            readings["legacy_secrets"] = services.sync_stat(paths["legacy_secrets"])
            readings["log"] = services.sync_log_marker(paths["log"])
            # The journal of a transaction is made before its first write and removed after its
            # last: none before and none after, and the stores unchanged since read, is one state.
            if services.sync_recovery_pending():
                raise Unavailable("recovery")
            stores = ("state", "history", "config", "secrets")
            if all(
                readings[name].status == "error" or services.sync_stat(paths[name]).stamp == readings[name].stamp
                for name in stores
            ):
                break
        else:
            raise Unavailable("busy")  # written again and again while read: the next poll looks again
        readings = self._held(readings, previous)
        digests = {name: reading.digest or reading.status for name, reading in readings.items()}
        self._forget_stale_caches(paths, digests)

        def same(*names: str) -> bool:  # parsed already, from the same bytes
            return previous is not None and all(previous.digests.get(name) == digests[name] for name in names)

        state = previous.state if previous is not None and same("state") else None
        if state is None:
            state = self._parsed(readings["state"], services.state_from_bytes, paths["state"])
        history = previous.history if previous is not None and same("history") else None
        if history is None:
            history = self._parsed(readings["history"], services.download_history_from_bytes, paths["history"])
        if readings["config"].status != "ok":
            raise Unavailable("config")
        if previous is not None and same("config"):
            config = previous.config
        else:
            try:
                config = services.config_from_bytes(readings["config"].content or b"")
            except Exception as exc:  # every page refuses this config too (tow.web.app)
                raise Unavailable("config") from exc
        secrets_entry = (
            previous.secrets if previous is not None and same("secrets", "legacy_secrets") else self._secrets(readings)
        )
        # Kept without their bytes: a held reading's content is already parsed in this sample.
        readings = {name: replace(reading, content=None) for name, reading in readings.items()}
        self._seq += 1
        with services.read_only_paths():
            running = services.sync_supervisor_running()
            try:
                update: Mapping[str, Any] = dict(services.web_update_status())
            except Exception:  # noqa: BLE001 - an unreadable update job is said as unknown, never a failed poll
                update = {"status": "unknown", "active": False}
        return Sample(
            seq=self._seq,
            taken=self._clock(),
            wall=time.time(),
            write_generation=generation,
            digests=digests,
            state=state,
            history=history,
            config=config,
            secrets=secrets_entry,
            running=running,
            paths=dict(paths),
            readings=dict(readings),
            update=update,
        )

    @staticmethod
    def _held(readings: dict[str, Any], previous: Sample | None) -> dict[str, Any]:
        """A file there but unreadable now (a sharing violation, a permission, a file that kept
        changing) keeps the previous sample's reading; without one the store is unavailable."""
        for name, reading in readings.items():
            if reading.status != "error":
                continue
            held = previous.readings.get(name) if previous is not None else None
            if held is None:
                if name in {"state", "history", "config"}:
                    raise Unavailable("data")
                continue
            readings[name] = held
        return readings

    def _forget_stale_caches(self, paths: Mapping[str, Any], digests: Mapping[str, str]) -> None:
        """Another digest than the last sample's: the stamp-keyed parsed copies of the pages may
        hold the older content (the same stamp), so they are dropped."""
        for name in ("state", "history"):
            if self._seen.get(name) != digests[name]:
                services.forget_parsed(paths[name])
        if self._seen.get("config") != digests["config"]:
            services.forget_config_cache()
        self._seen = dict(digests)

    @staticmethod
    def _parsed(reading: Any, parse: Callable[[bytes | None], dict[str, Any]], path: Any) -> dict[str, Any]:
        if reading.status == "missing" and services.sync_quarantined(path):
            raise Unavailable("data")  # the pages refuse too: never "no topics" over the user's data
        if reading.status == "error":
            raise Unavailable("data")  # held from a sample that is gone: nothing parsed to keep
        try:
            return parse(reading.content if reading.status == "ok" else None)
        except Exception as exc:
            raise Unavailable("data") from exc

    @staticmethod
    def _secrets(readings: Mapping[str, Any]) -> tuple[bool, Any]:
        if readings["legacy_secrets"].status == "ok":
            return (False, SecretStoreError("store.migrate_needed"))
        reading = readings["secrets"]
        if reading.status == "error":  # there but unreadable now: not "no secrets"
            return (False, SecretStoreError(f"secrets unreadable now: {reading.error}"))
        try:
            return (True, services.secrets_from_bytes(reading.content if reading.status == "ok" else None))
        except Exception as exc:  # noqa: BLE001 - as the pages' reader: a broken store is said, not raised
            return (False, exc if isinstance(exc, SecretStoreError) else SecretStoreError(type(exc).__name__))


# --------------------------------------------------------------------------- areas


@dataclass(frozen=True)
class Computed:
    value: Any
    rev: str
    deadline: float | None  # wall-clock seconds; None: only a source change changes it
    extra: Any = None


def _next_midnight(now: float) -> float:
    zone = local_zone()
    moment = datetime.fromtimestamp(now, zone)
    following = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return following.timestamp()


def _today(now: float) -> str:
    return datetime.fromtimestamp(now, local_zone()).date().isoformat()


def _earliest(*moments: float | None) -> float | None:
    found = [moment for moment in moments if moment is not None]
    return min(found) if found else None


# Exactly what ``_topic_row.html`` reads of a row (``views.topic_rows``), besides its timer's
# minutes, the site's title and the day its date labels are written for. ``manual_job`` is the
# jobs area; the timer's moment and phase are the timers area: a row never changes by the clock.
ROW_FIELDS = (
    "id",
    "name",
    "series",
    "title",
    "tracker",
    "tracker_title",
    "url",
    "save_path",
    "paused",
    "last_changed",
    "download_summary",
    "error_line",
    "verification_pending",
    "last_event_label",
    "last_event_at",
    "last_event_iso",
    "last_error",
    "last_error_human",
    "torrent_tone",
    "torrent_label",
    "tracker_tone",
    "tracker_label",
)


def row_projection(row: Mapping[str, Any], *, site_title: str, day: str) -> dict[str, Any]:
    """The stable projection of a Home row: no ``now``, no countdown."""
    value = {name: row.get(name) for name in ROW_FIELDS}
    value["verification_pending"] = bool(row.get("verification_pending"))
    timer = row.get("timer") or {}
    value["timer_minutes"] = timer.get("minutes") if isinstance(timer, Mapping) and timer else None
    value["site_title"] = site_title
    if row.get("last_event_iso"):
        value["day"] = day  # "20:31" today is "16 Oct, 20:31" tomorrow
    return value


def _row_hash(projection: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(projection)).hexdigest()[:REV_HEX]


def row_hash(row: Mapping[str, Any], *, site_title: str, now: float | None = None) -> str:
    """The digest of a Home row's stable projection: the topics area's ``rows`` value, and the
    ``data-row-hash`` a page draws the row with (``templating.row_hash``)."""
    return _row_hash(row_projection(row, site_title=site_title, day=_today(time.time() if now is None else now)))


@dataclass
class _Memo:
    key: Any
    computed: Computed


class Areas:
    """The areas' projections and revisions, computed once per change of what they are built
    from (or at their deadline) for every request with the same context."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._memo: OrderedDict[Any, _Memo] = OrderedDict()
        self._rings: dict[Any, deque[tuple[str, dict[str, str]]]] = {}
        self._inventory: tuple[int, Any] | None = None

    def forget(self) -> None:
        with self._lock:
            self._memo.clear()
            self._rings.clear()
            self._inventory = None

    def _area_lock(self, name: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(name, threading.Lock())

    def get(self, name: str, ctx: tuple[Any, ...], key: Any, now: float, build: Callable[[], Computed]) -> Computed:
        memo_key = (name, ctx)
        with self._lock:
            known = self._memo.get(memo_key)
        if (
            known is not None
            and known.key == key
            and (known.computed.deadline is None or now < known.computed.deadline)
        ):
            return known.computed
        with self._area_lock(name):
            with self._lock:
                known = self._memo.get(memo_key)
            if (
                known is not None
                and known.key == key
                and (known.computed.deadline is None or now < known.computed.deadline)
            ):
                return known.computed
            computed = build()
            with self._lock:
                self._memo[memo_key] = _Memo(key, computed)
                self._memo.move_to_end(memo_key)
                while len(self._memo) > 256:
                    self._memo.popitem(last=False)
            return computed

    def remember_rows(self, ctx: Any, rev: str, rows: dict[str, str]) -> None:
        with self._lock:
            ring = self._rings.setdefault(ctx, deque(maxlen=RING_SIZE))
            if not ring or ring[-1][0] != rev:
                ring.append((rev, rows))
            while len(self._rings) > 8:
                del self._rings[next(iter(self._rings))]

    def rows_at(self, ctx: Any, rev: str) -> dict[str, str] | None:
        with self._lock:
            for known, rows in self._rings.get(ctx, ()):
                if known == rev:
                    return rows
        return None

    def inventory(self, sample: Sample, build: Callable[[], Any]) -> Any:
        """The backup inventory of Settings, read once per sample."""
        with self._lock:
            known = self._inventory
        if known is not None and known[0] == sample.seq:
            return known[1]
        value = build()
        with self._lock:
            self._inventory = (sample.seq, value)
        return value


SAMPLER = Sampler()
AREAS_MEMO = Areas()


def reset() -> None:
    """Forget every sample and projection (a test, a restore)."""
    SAMPLER.forget()
    AREAS_MEMO.forget()
    with _BUCKETS_LOCK:
        _BUCKETS.clear()


@dataclass(frozen=True)
class Viewer:
    """Who asks: the language the page is in, this computer or the network, the session."""

    lang: str
    local: bool
    session: str  # a hash of the session id ("" on this computer)


def viewer(request: Request) -> Viewer:
    cookie = str(request.cookies.get(SESSION_COOKIE) or "")
    local = access.is_local(request)
    session = "" if local or not cookie else hashlib.sha256(cookie.split(".", 1)[0].encode()).hexdigest()[:32]
    return Viewer(lang=i18n.current(), local=local, session=session)


def _topics(sample: Sample, who: Viewer, now: float) -> Computed:
    key = (*sample.key("state", "history", "config", "status"), sample.running)

    def build() -> Computed:
        from tow.web.templating import site_title
        from tow.web.views import topic_rows

        with sample.seeded():
            rows = topic_rows(sample.state, local=who.local)  # a network device's words are scrubbed
            day = _today(now)
            hashes = {
                str(row["id"]): _row_hash(
                    row_projection(row, site_title=site_title(row["tracker"]) if row.get("tracker") else "", day=day)
                )
                for row in rows
            }  # = row_hash(row, ...) for each row, the day computed once
        order = [str(row["id"]) for row in rows]
        value = {"order": order, "rows": hashes}
        rev = revision("topics", value, {"lang": who.lang, "local": who.local})
        AREAS_MEMO.remember_rows((who.lang, who.local), rev, hashes)
        return Computed(value, rev, _next_midnight(now))

    return AREAS_MEMO.get("topics", (who.lang, who.local), key, now, build)


def _jobs(now: float) -> Computed:
    found = check_jobs.snapshot()
    key = (found["generation"], len(found["waiting"]), len(found["ended"]))

    def build() -> Computed:
        value = {"waiting": found["waiting"], "ended": found["ended"]}
        return Computed(value, revision("jobs", value), None)

    return AREAS_MEMO.get("jobs", (), key, now, build)


def _health(sample: Sample, who: Viewer, now: float) -> Computed:
    """The header and Needs attention, computed together (the header's own computation does both)."""
    key = (*sample.key("state", "config", "secrets", "legacy_secrets", "status", "schedule"), sample.running)

    def build() -> Computed:
        from tow.issues.kinds.check_stale import STALE_SLACK_SEC
        from tow.web.templating import header_health, site_title

        with sample.seeded():
            health = header_health()
            # The header names a site by its title: a site a new topic brings is drawn with it.
            health["site_titles"] = {name: site_title(name) for name in health.get("sites") or {}}
        attention = health.pop("attention")
        health.pop("ver", None)  # the version area
        header_rev = revision("header", health, {"lang": who.lang})
        attention_rev = revision("attention", attention, {"lang": who.lang})
        # A scheduled check becomes late (tow.issues check_stale) at next + interval + slack.
        deadline = None
        if health.get("next_at_ts"):
            stale_at = float(health["next_at_ts"]) + float(health.get("interval_sec") or 0) + STALE_SLACK_SEC + 1
            deadline = stale_at if stale_at > now else None
        return Computed({"header": health, "attention": attention}, header_rev, deadline, extra=attention_rev)

    return AREAS_MEMO.get("health", (who.lang,), key, now, build)


def _timers(sample: Sample, now: float) -> Computed:
    key = (*sample.key("state", "status", "schedule"), sample.running)

    def build() -> Computed:
        with services.read_only_paths():
            found = services.topic_timer_status(sample.state)
        value = {
            tid: {"minutes": item.get("minutes"), "state": item.get("state"), "next_at": item.get("next_at")}
            for tid, item in sorted(found.items())
        }
        # A timer "scheduled" becomes "queued" when its moment comes.
        due = [
            float(item["next_at"])
            for item in value.values()
            if item["state"] == "scheduled" and isinstance(item["next_at"], (int, float)) and item["next_at"] > now
        ]
        return Computed(value, revision("timers", value), min(due) if due else None)

    return AREAS_MEMO.get("timers", (), key, now, build)


FEED_WAIT_SEC = 0.3


def _messages(sample: Sample, who: Viewer, now: float) -> Computed:
    """Recent messages: the shared feed's own revision (entries, words, read state), so a routine
    log line that is no message changes nothing; due again when its oldest entry expires. Without
    the feed (its reader not started yet) the log's tail stands in for it."""
    from tow.web.messages import feed_payload

    try:
        # Up to 0.3 s for the feed's own reader (it takes no lock), so a new message is in this very
        # poll rather than the next one, five seconds later.
        feed = feed_payload(wait=FEED_WAIT_SEC)
    except Exception:  # noqa: BLE001 - the poll answers with the stand-in rather than failing
        key = sample.key("log", "config")

        def build() -> Computed:
            value = {"log": sample.digests.get("log", ""), "config": sample.digests.get("config", "")}
            return Computed(value, revision("messages", value, {"lang": who.lang}), None)

        return AREAS_MEMO.get("messages", (who.lang,), key, now, build)
    value = {"feed": str(feed.get("revision") or "")}
    expires = feed.get("expires_at")
    deadline = float(expires) if isinstance(expires, int | float) and expires > 0 else None
    return Computed(value, revision("messages", value, {"lang": who.lang}), deadline)


def _undo(sample: Sample, who: Viewer, now: float) -> Computed:
    key = sample.key("state", "config", "secrets", "legacy_secrets")

    def build() -> Computed:
        record = sample.state.get("undo")
        value: dict[str, Any] | None = None
        deadline = None
        if isinstance(record, dict) and record.get("kind") and record.get("ts"):
            try:
                stamped = parse_timestamp(str(record["ts"])).timestamp()
            except TypeError, ValueError:
                stamped = None
            if stamped is not None:
                until = stamped + flash_ttl(sample.config.get("flash_ttl_sec"))
                if until > now:
                    with sample.seeded():
                        label = services.undo_label(sample.state)
                    local_only = False
                    if not who.local:  # what the engine refuses the network (an unreadable secret store too)
                        ok, secrets = sample.secrets
                        local_only = not ok or services.undo_local_only(record, sample.config, secrets)
                    value = {
                        "stamp": str(record["ts"]),
                        "until_ts": until,
                        "kind": str(record.get("kind")),
                        "label": label,
                        "local_only": bool(local_only),
                    }
                    deadline = until
        return Computed(value, revision("undo", value, {"lang": who.lang, "local": who.local}), deadline)

    return AREAS_MEMO.get("undo", (who.lang, who.local), key, now, build)


def _config(sample: Sample, who: Viewer, now: float) -> Computed:
    key = sample.key("config")

    def build() -> Computed:
        from tow.config import as_bool

        theme = sample.config.get("theme")
        value = {
            "theme": theme if theme in ("light", "dark") else "auto",
            "language": i18n.setting(sample.config),
            "language_resolved": who.lang,
            "interval_sec": interval_sec_of(sample.config),
            "check_updates": as_bool(sample.config.get("check_updates", True)),
        }
        return Computed(value, revision("config", value), None)

    return AREAS_MEMO.get("config", (who.lang,), key, now, build)


def _version(now: float) -> Computed:
    from tow.web.templating import STATIC_VERSION

    def build() -> Computed:
        value = {"app": __version__, "static": STATIC_VERSION}
        return Computed(value, revision("version", value), None)

    return AREAS_MEMO.get("version", (), (__version__, STATIC_VERSION), now, build)


def _release(sample: Sample, now: float) -> Computed:
    from tow.config import as_bool

    enabled = as_bool(sample.config.get("check_updates", True))
    published = services.release_cached(enabled=enabled)
    update = sample.update
    job = None
    if update.get("status") not in (None, "", "idle"):
        job = {"id": update.get("id"), "status": update.get("status"), "active": bool(update.get("active"))}
    value = {
        key: published.get(key)
        for key in ("current", "latest", "available", "comparable", "checks_enabled", "ok", "checked_at", "url")
    }
    value["job"] = job
    key = _canonical(value)

    def build() -> Computed:
        return Computed(value, revision("release", value), None)

    return AREAS_MEMO.get("release", (), key, now, build)


def page_revision(who: Viewer, page: str, value: Any, filters: Mapping[str, str] | None = None) -> str:
    """The page area's revision of ``value`` for this viewer (and History's filters)."""
    ctx: dict[str, Any] = {"lang": who.lang, "local": who.local, "page": page}
    if page == "/history":
        ctx.update(filters or {"group": "", "q": ""})
    return revision("page", value, ctx)


def _page(
    sample: Sample,
    who: Viewer,
    page: str,
    now: float,
    request: Request,
    filters: Mapping[str, str] | None = None,
) -> Computed:
    """Sites and Settings: the digests of the fragments the page draws (``page_sync``), drawn from
    this sample for this viewer - a write that changes nothing they show (a routine check's times,
    the supervisor's plan) changes no revision. ``extra``: the fragments themselves, so that
    ``/page/fragments`` hands out exactly the revision named. History: its log, the names it
    writes and its filters."""
    if page == "/":
        value: Any = {"page": "/"}  # Home: the topics area
        return Computed(value, page_revision(who, page, value), None)
    if page in page_sync.FRAGMENT_PAGES:
        key: Any = (*sample.key("state", "config", "secrets", "legacy_secrets", "status", "schedule"), sample.running)
        if page == "/settings":
            inventory = AREAS_MEMO.inventory(sample, lambda: _backup_inventory(sample.config))
            key = (*key, _canonical(inventory))

        def build_fragments() -> Computed:
            with sample.seeded():
                found = page_sync.fragments(page_sync.render(request, page))
            value = page_sync.projection(found)
            # A date label ("20:31" today) is another one tomorrow.
            return Computed(value, page_revision(who, page, value), _next_midnight(now), extra=found)

        return AREAS_MEMO.get(f"page:{page}", (who.lang, who.local), key, now, build_fragments)
    shown = dict(filters or {"group": "", "q": ""})
    key = sample.key("log", "config", "state")

    def build_history() -> Computed:
        names = {str(topic.get("id")): shown_title(topic) for topic in sample.state.get("topics") or []}
        value = {
            "log": sample.digests.get("log", ""),
            "names": names,
            "sites": {
                str(name): (spec or {}).get("title") if isinstance(spec, Mapping) else None
                for name, spec in (sample.config.get("trackers") or {}).items()
            },
            "day": _today(now),
        }
        return Computed(value, page_revision(who, page, value, shown), _next_midnight(now))

    return AREAS_MEMO.get("page:/history", (who.lang, who.local, shown["group"], shown["q"]), key, now, build_history)


def annotated_page(request: Request, page: str, html: str) -> str:
    """A Sites or Settings page as its route drew it, knowing its own revision and each fragment's
    digest (``page_sync.annotate``): it asks for nothing until what it shows changes."""
    found = page_sync.fragments(html)
    return page_sync.annotate(html, page_revision(viewer(request), page, page_sync.projection(found)), found)


def page_fragments(request: Request, page: str) -> dict[str, Any]:
    """Sites or Settings: every fragment of the page's current revision, in its order. Raises
    ``Unavailable``."""
    sample = SAMPLER.current()
    computed = _page(sample, viewer(request), page, time.time(), request)
    found: Mapping[str, tuple[str, str]] = computed.extra
    return {
        "ok": True,
        "page": page,
        "rev": computed.rev,
        "order": list(found),
        "fragments": [{"id": fid, "hash": item[0], "html": item[1]} for fid, item in found.items()],
    }


def history_fragments(request: Request, filters: Mapping[str, str]) -> dict[str, Any]:
    """History's newest lines (at most ``page_sync.HISTORY_LIMIT``) as its filters choose them,
    each with its key and digest, and the revision of the sample they go with. Raises
    ``Unavailable``."""
    from tow.log import index_event_titles

    sample = SAMPLER.current()
    computed = _page(sample, viewer(request), "/history", time.time(), request, filters)
    with sample.seeded():
        events = services.history_events(
            group=filters["group"],
            text=filters["q"],
            limit=page_sync.HISTORY_LIMIT,
            title_index=index_event_titles(sample.state.get("topics") or []),
            lock_live=False,  # a snapshot: the checker appending meanwhile is never waited for
        )
        rows = page_sync.history_rows(events, sample.state, sample.config)
        entries = page_sync.history_entries(page_sync.page_request(request, "/history"), rows)
    return {"ok": True, "page": "/history", "rev": computed.rev, "entries": entries, "limit": page_sync.HISTORY_LIMIT}


def _backup_inventory(cfg: dict[str, Any]) -> Any:
    """What Settings shows of the copies, their cleanups and the service, as data (read-only):
    the copies' files themselves, not their folder's stamp (a folder's mtime misses a file
    rewritten inside it)."""
    found: dict[str, Any] = {}
    with services.read_only_paths():
        for name, read in (
            ("night", lambda: services.snapshot_status()),
            ("copies", lambda: services.list_snapshots(limit=None)),
            # Without the data lock (whose taking runs the recovery steps): a poll only looks.
            ("night_cleanup", lambda: services.night_cleanup_status_unlocked(cfg=cfg)),
            ("points", lambda: services.list_restore_points()),
            ("points_cleanup", lambda: services.restore_point_cleanup_status(cfg=cfg)),
            ("service", lambda: services.service_status()),
        ):
            try:
                found[name] = read()
            except Exception as exc:  # noqa: BLE001 - an unreadable part is a part that says so
                found[name] = {"unreadable": type(exc).__name__}
    return json.loads(_canonical(found))


# --------------------------------------------------------------------------- the answer


def parse_have(raw: str) -> dict[str, str]:
    """``have``: the page's revisions, ``area:rev`` pairs separated by commas. Raises ValueError
    for anything else (an unknown area, a malformed revision, too long)."""
    if len(raw) > MAX_HAVE_CHARS:
        raise ValueError("have too long")
    found: dict[str, str] = {}
    for part in filter(None, raw.split(",")):
        area, sep, rev = part.partition(":")
        if not sep or area not in AREAS or not REVISION.fullmatch(rev) or area in found:
            raise ValueError("malformed have")
        found[area] = rev
    return found


def parse_page(raw: str) -> str:
    if raw and raw not in PAGES:
        raise ValueError("unknown page")
    return raw


@dataclass(frozen=True)
class Answer:
    revs: dict[str, str]
    etag: str
    body: Callable[[], dict[str, Any]]


def answer(
    request: Request,
    have: Mapping[str, str],
    page: str,
    *,
    now: float | None = None,
    filters: Mapping[str, str] | None = None,
) -> Answer:
    """The revisions for this request and, built only when sent, the areas whose revision
    differs from ``have``. ``filters``: History's (its group and search), part of the exact
    request the ETag names. Raises ``Unavailable``."""
    sample = SAMPLER.current()
    now = time.time() if now is None else now
    who = viewer(request)
    topics = _topics(sample, who, now)
    health = _health(sample, who, now)
    computed: dict[str, Computed] = {
        "topics": topics,
        "jobs": _jobs(now),
        "header": health,
        "attention": Computed(health.value["attention"], health.extra, health.deadline),
        "timers": _timers(sample, now),
        "messages": _messages(sample, who, now),
        "undo": _undo(sample, who, now),
        "config": _config(sample, who, now),
        "version": _version(now),
        "release": _release(sample, now),
    }
    shown = dict(filters or {}) if page == "/history" else {}
    if page:
        computed["page"] = _page(sample, who, page, now, request, shown or None)
    revs = {name: item.rev for name, item in computed.items()}
    base_rows = AREAS_MEMO.rows_at((who.lang, who.local), have["topics"]) if have.get("topics") else None
    variant = {
        "schema": SCHEMA,
        "install": _install(),
        "epoch": SERVER_EPOCH,
        "local": who.local,
        "session": who.session,
        "lang": who.lang,
        "page": page,
        "filters": dict(sorted(shown.items())),
        "have": dict(sorted(have.items())),
        "topics_base": base_rows is not None,
        "revs": revs,
    }
    etag = 'W/"' + hashlib.sha256(_canonical(variant)).hexdigest()[:REV_HEX] + '"'

    def body() -> dict[str, Any]:
        from tow.web.text import zone_minutes

        areas: dict[str, Any] = {}
        for name, item in computed.items():
            if have.get(name) == item.rev:
                continue
            areas[name] = _payload(name, item, base_rows, page)
        return {
            "ok": True,
            "schema": SCHEMA,
            "now_ts": time.time(),
            "tz_now": zone_minutes(),
            "rev": revs,
            "areas": areas,
        }

    return Answer(revs, etag, body)


def _install() -> str:
    try:
        return services.install_id()
    except OSError, RuntimeError, ValueError:
        return ""


def _payload(name: str, item: Computed, base_rows: dict[str, str] | None, page: str) -> dict[str, Any]:
    value = item.value
    if name == "topics":
        rows: dict[str, str] = value["rows"]
        if base_rows is None:
            return {"rev": item.rev, "full": True, "order": value["order"], "rows": rows}
        changed = [tid for tid in value["order"] if base_rows.get(tid) != rows[tid]]
        removed = [tid for tid in base_rows if tid not in rows]
        return {"rev": item.rev, "full": False, "order": value["order"], "changed": changed, "removed": removed}
    if name == "header":
        return {"rev": item.rev, **value["header"]}
    if name == "attention":
        return {"rev": item.rev, **value}
    if name == "undo":
        return {"rev": item.rev, "undo": value}
    if name == "timers":
        return {"rev": item.rev, "timers": value}
    if name == "page":
        return {"rev": item.rev, "page": page}
    if name == "messages":
        return {"rev": item.rev}
    return {"rev": item.rev, **value}


def etag_matches(header: str | None, etag: str) -> bool:
    """``If-None-Match`` names ``etag`` (weak comparison, as HTTP says for If-None-Match)."""
    if not header:
        return False
    if header.strip() == "*":
        return True
    wanted = etag.removeprefix("W/")
    return any(part.strip().removeprefix("W/") == wanted for part in header.split(",")[:16])


# --------------------------------------------------------------------------- Home rows


def parse_ids(values: Iterable[object]) -> list[str]:
    """At most MAX_IDS topic ids, each valid, in their order and once. Raises ValueError."""
    found: dict[str, None] = {}
    for value in values:
        if not isinstance(value, str) or not TOPIC_ID.fullmatch(value):
            raise ValueError("malformed id")
        found.setdefault(value)
        if len(found) > MAX_IDS:
            raise ValueError("too many ids")
    return list(found)


def rows(request: Request, ids: list[str]) -> dict[str, Any]:
    """Home rows of ``ids`` drawn from one sample, with that sample's topics revision and its
    whole order (the manifest: an id not in it was removed). Rows past ROWS_BYTE_CAP are
    ``deferred``: asked again. Raises ``Unavailable``."""
    from tow.web.templating import TEMPLATES
    from tow.web.views import topic_rows

    sample = SAMPLER.current()
    now = time.time()
    who = viewer(request)
    topics = _topics(sample, who, now)
    wanted = set(ids)
    by_id = {
        str(topic.get("id")): topic for topic in sample.state.get("topics") or [] if str(topic.get("id")) in wanted
    }
    drawn: list[dict[str, str]] = []
    deferred: list[str] = []
    size = 0
    template = TEMPLATES.env.get_template("_topic_row.html")
    with sample.seeded():
        present = [tid for tid in ids if tid in by_id]
        for row in topic_rows({**sample.state, "topics": [by_id[tid] for tid in present]}, local=who.local):
            tid = str(row["id"])
            if size >= ROWS_BYTE_CAP:
                deferred.append(tid)
                continue
            row["manual_job"] = check_jobs.pending_topic(tid)
            html = template.render(topic=row, landing_topic="", request=request)
            size += len(html.encode())
            drawn.append({"id": tid, "html": html, "hash": topics.value["rows"].get(tid, "")})
    return {
        "ok": True,
        "rev": topics.rev,
        "order": topics.value["order"],
        "rows": drawn,
        "missing": [tid for tid in ids if tid not in by_id],
        "deferred": deferred,
    }


# --------------------------------------------------------------------------- rate limit


@dataclass
class _Bucket:
    tokens: float
    at: float


_BUCKETS_LOCK = threading.Lock()
_BUCKETS: OrderedDict[str, _Bucket] = OrderedDict()


def rate_key(request: Request) -> str:
    """A bucket per session of a device on the network; on this computer one per tab when the
    page names it (``X-TOW-Tab``), else one for the computer."""
    who = viewer(request)
    if not who.local:
        return "session:" + (who.session or "none")
    tab = str(request.headers.get("x-tow-tab") or "")
    return "local:" + (tab if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tab) else "")


def take_token(key: str, *, now: float | None = None) -> float:
    """0 when the request may go on, else the seconds until it may (a token bucket per ``key``)."""
    moment = time.monotonic() if now is None else now
    with _BUCKETS_LOCK:
        bucket = _BUCKETS.get(key)
        if bucket is None:
            bucket = _BUCKETS[key] = _Bucket(RATE_BURST, moment)
            while len(_BUCKETS) > MAX_BUCKETS:
                _BUCKETS.popitem(last=False)
        _BUCKETS.move_to_end(key)
        bucket.tokens = min(RATE_BURST, bucket.tokens + max(0.0, moment - bucket.at) * RATE_PER_SEC)
        bucket.at = moment
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return 0.0
        return max(1.0, math.ceil((1 - bucket.tokens) / RATE_PER_SEC))


# --------------------------------------------------------------------------- recovery, outside the poll

_RECOVERY_LOCK = threading.Lock()
_recovery: threading.Thread | None = None


def recover_later() -> threading.Thread | None:
    """Run the usual recovery (an interrupted store transaction, a pending secret-undo cleanup) in
    a thread of its own, once at a time: the poll that found it waiting never waits for it."""
    global _recovery
    with _RECOVERY_LOCK:
        if _recovery is not None and _recovery.is_alive():
            return _recovery

        def run() -> None:
            try:
                # Taking the data lock runs every recovery step (a check's commit, an import, a
                # night-copy restore, the site journal: tow.storage.locks.RECOVERY_STEPS).
                with services.persistence_lock():
                    pass
                services.recover_store_transaction()
                if services.undo_cleanup_needed():
                    services.cleanup_secret_undo()
            except Exception as exc:  # noqa: BLE001 - the next page or poll tries again; the reason is logged
                import logging

                logging.getLogger("tow.web").warning("recovery for the poll failed: %s", type(exc).__name__)

        _recovery = threading.Thread(target=run, name="tow-sync-recovery", daemon=True)
        _recovery.start()
        return _recovery
