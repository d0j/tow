# Extending TOW

How to add a torrent client, a messenger, a site, a language or a page. Each is a module that TOW discovers by
itself, plus its texts in the language files; a contract test checks every module that is found. Background:
[architecture.md](architecture.md). Development setup and the gate: [CONTRIBUTING.md](../CONTRIBUTING.md).

| Add | Where | Contract | Checked by |
|---|---|---|---|
| [Torrent client](#torrent-client) | `src/tow/clients/<kind>.py` | `TorrentClientAdapter` (`clients/spec.py`) | `test_plugin_contracts.py`, `test_clients_managed.py` |
| [Messenger](#messenger) | `src/tow/notifiers/<kind>.py` | `Notifier` (`notifiers/base.py`) | `test_plugin_contracts.py`, `test_notifiers.py` |
| [Site](#site) | `src/tow/trackers/presets/<name>.py` | `SitePreset` (`trackers/presets/__init__.py`) | `test_tracker_presets.py` |
| [Language](#language) | `src/tow/locales/<code>.json` | the keys of `en.json` | `test_i18n.py` |
| [Page or action](#page-or-action) | `src/tow/web/routes_<topic>.py` | `APIRouter`, `tow.web.services` | `test_routes.py` |

## Torrent client

**Today:** qBittorrent, Transmission 3.0+, Deluge 2.x (`READY = True`); a µTorrent placeholder (`READY = False`).

A client module declares:

- `KIND`, `TITLE`, `SHORT` (header label), `ORDER`, `SECRETS_KEY`, `READY`, `DEFAULT_PORT`;
- `FIELDS` — the settings card fields (address, port, login, password by default);
- `STEPS` — how to turn on the Web UI in the client; `NOTE` — its quirks (language-file keys `client.<kind>.*`);
- `from_secrets(secrets)` → an object satisfying `TorrentClientAdapter`.

Every client follows the same read-back contract, the shared flow in `clients/managed.py`; a new client uses
`ManagedClient` rather than duplicate it (qBittorrent overrides single steps, such as its add, never the
guarantees):

1. the torrent is added **stopped** with the labels `tow` + `tow-pending`;
2. files are selected by path and size, never by index;
3. the selection is read back, then the torrent starts and `tow-pending` is removed; on failure TOW stops what it
   added;
4. success is only what the client confirms by read-back;
5. torrents without the `tow` label are never stopped or changed;
6. states are reported in qBittorrent's words (`stoppedDL`, `downloading`, `uploading`, `moving`, `error`…), so the
   rest of TOW does not depend on the client.

**Steps:**

1. Subclass `ManagedClient` and implement only the primitives: `ping`, `inspect_torrent`, `_add_stopped`,
   `_set_wanted`, `_stop`, `_start`, `_set_labels`, `_move` and, if the client can, `materialize_magnet`.
   Override `_owner_tags` with a query that reads only the torrent's labels: the owner mark is re-read
   before every mutation, and the default reads the whole torrent through `inspect_torrent`.
2. Declare optional abilities in `capabilities` (names in `CAPABILITIES`), never by leaving a method out: a client
   without magnet support sets `magnet_metadata: False` and its `materialize_magnet` refuses.
   Graphical metadata preview is a separate `metadata_preview` capability and `preview_magnet`
   method. It may use an explicit native peer-metadata API but must never add, change or delete
   transfer tasks, switch an attached daemon, or fall back to `materialize_magnet`. Keep it out
   of dry-run; validate both hashes and retain strict v2 metadata/layer validation.
3. Add a fake server to `tests/test_clients_managed.py`; the shared contract suite runs against it automatically.
4. Run the full cycle against a real, isolated client instance (its own port and folder, a test torrent): add
   stopped, partial selection, read-back, start, re-add refused, selection change, stop, move, completion time,
   refusal to touch a foreign torrent, wrong password. Only then set `READY = True`.

## Messenger

**Today:** Telegram, Discord, WhatsApp (CallMeBot), ntfy.

A messenger module declares `KIND`, `TITLE`, `ORDER`, `MAX_LEN`, `STEPS`, `NOTE`, `FIELDS` (each a `Field`: `text`,
`secret` or `list`, with a placeholder and an error key) and `send(settings, text)`, which raises `DeliveryError`
with a reason that holds no tokens or addresses. Optional: `STORAGE`, `targets(settings)`, `MAX_BYTES`,
`MAX_UTF16`, `CHUNK_PAUSE`, `suggestion(field)` — see the `Notifier` docstring.

Everything else is shared: the settings card, encrypted storage, **Check** and **Disconnect** with undo, the bell
in the header, and delivery (`notifiers/outbox.py`): retries on a refused connection, 429 (honouring `Retry-After`)
and 5xx, never after a request that went out without an answer (it may have arrived; it waits in the queue);
a 2xx answer is a delivered message, whatever its body; a per-channel queue of up to 50 messages; long messages split by lines; one channel failing never blocks the
others. Use `notifiers.base.request()` for HTTP. Tests replace HTTP; no test talks to a real service.

## Site

**Today:** rutor, kinozal, nnmclub, rutracker, tapochek, unionpeer, fast_torrent (and `tparser`, a search site
whose name is only stripped from titles). Any site can also be configured by hand on the Sites page: that is the
generic tracker (`trackers/generic.py`); a preset only adds defaults, and the site's own settings always win.

A preset module declares `PRESET = SitePreset(...)`:

| Field | Meaning |
|---|---|
| `name` | the key under `trackers:` in `config.yaml` |
| `label` | how messages name it (`"RuTracker"`) |
| `brands` | regex parts that name the site in a host or page title (stripped from topic titles) |
| `search_path` | a search link with `{q}` — TOW only links to it, never fetches results |
| `daily_limit` | its accounts have a daily `.torrent` limit (a preview must not spend it) |
| `browser_login`, `browser_login_hint` | sign-in only through a browser window, and the hint key (`site.<name>.*`) |
| `spec` | the site's settings exactly as `config.example.yaml` ships them (the test compares them) |
| `guess(parts)` | its own rules for a pasted link → new site settings, or `None` |
| `canonical_url(url)` | a CDN or download link → the topic page |

A `config.yaml` without a `trackers:` section gets the `spec` of every preset site, so a new install
watches every known site until the owner edits the list.

Adding a site is that one module, its block in `config.example.yaml`, and its `site.<name>.*` texts if it has any.
`tests/test_tracker_presets.py` checks that it is found and consistent; add a `guess` case to `tests/test_guess.py`
with a made-up topic id.

## Language

**Today:** English (reference and fallback) and Russian. The language follows the browser (`Accept-Language`,
RFC 4647 matching: `zh-Hant-TW` → `zh-Hant` → `zh`) or is chosen in Settings → Language. Messenger messages and
background jobs use the chosen language, or in automatic mode the one the owner's browser last used. A command
typed in a terminal (`tow status`, `tow stop`, `tow doctor`…) uses the chosen language, or in automatic mode the
operating system's (Windows display language; `LC_ALL`/`LC_MESSAGES`/`LANG` on Linux and macOS).

One language is one file, `src/tow/locales/<code>.json` (regional: `pt-BR.json`; case does not matter), with nested
sections and `_meta`:

```json
{"_meta": {"name": "German", "native": "Deutsch", "plural": "one_other",
           "datetime": "%d.%m.%Y %H:%M:%S", "decimal": ","},
 "common": {"save": "Speichern"}}
```

1. Copy `en.json` to `<code>.json`.
2. Translate the values; never change the keys.
3. Set `_meta.plural`: `one_other`, `east_slavic`, `west_slavic`, `polish`, `french` or `none`. Optionally
   `datetime` (numeric `%d %m %Y %y %H %I %M %S %p` only), `datetime_short` and `decimal`.

The language appears in the list by itself; a missing key falls back to English. A damaged file is skipped with a
warning and never breaks a page.

Rules for every text (enforced by `tests/test_i18n.py`):

- Code holds keys only: `t("key", name=value)` in Python, `{{ t('key') }}` in templates, `t("js.key")` in
  `app.js`. A key built from parts (`t(f"notify.action.{kind}")`) has its values listed in the test.
- Same keys and the same `{placeholders}` as `en.json`, both ways; every plural form of the language's rule.
- A sentence with emphasis is one value with inline markup, so a translation can reorder words:
  `"<b>No password set</b> — set it below"`, rendered with `tm('key', ...)`. Allowed: `<b>`, `<strong>`, `<em>`,
  `<i>`, `<code>`, `<kbd>` and `<a href="{name}">`; the same tags in every language.
- Plurals: `{"one": "{n} client", "other": "{n} clients"}` and `t("settings.clients.count", n=3)`.
- A plugin keeps its texts in its own section (`client.deluge.*`, `notifier.whatsapp.*`).
- Errors are keys too: `raise TowError("selection.too_long", limit=8192)`. The status colour comes from the key or
  the raise site (`CLASSES` in `tow/errors.py`), never from the wording.
- Russian text addresses the user formally (на «вы»).

## Page or action

The web package (`src/tow/web/`):

- `app.py` — `create_app()`: the middleware (`middleware.py`), error handlers, static files, template globals and
  the routers in `ROUTERS` order. `tow.web.app` is the built app (`tow serve`, `tow run`, tests).
- `routes_<topic>.py` — one `router = APIRouter()` per topic: `routes_home`, `routes_topics`,
  `routes_topic_login`, `routes_check`, `routes_undo`, `routes_sites`, `routes_settings`, `routes_notifiers`,
  `routes_backup`, `routes_service`, `routes_password`, `routes_auth`, `routes_health`, `routes_history`.
  Importing a module registers nothing.
- `services.py` — the only way out of the package: config, state, secrets and history, checks, the service,
  restore points, diagnostics, browser sign-in, the log; also `login_throttle` and `locked_state_mutation`.
- Helpers: `_context.py` (what a request reads once), `views.py` (flash messages, Home rows), `text.py`,
  `templating.py`, `site_form.py`, `site_store.py` (several stores in one transaction), `topic_actions.py` (what
  the topic add and edit forms do; the record itself comes from `tow.topic_form`). Anything another module uses
  has a name without a leading underscore (`tests/test_module_boundaries.py`).

**Steps:**

1. Add a function to the right `routes_<topic>.py` (or a new module with `router = APIRouter()`):
   `@router.get(...)` / `@router.post(...)` returning `-> Response`; for JSON, `-> dict[str, Any]` and
   `response_model=None`.
2. Read or change data, clients, sites or the service only through `services`. A read-modify-write of the state
   gets `@services.locked_state_mutation` under the route decorator.
3. Answer an action with `flash_redirect(url, "text.key", "ok" | "warn" | "err")`: the text stays on the server,
   only a token goes into the URL. A page is a template in `src/tow/templates/` rendered with
   `TEMPLATES.TemplateResponse(request, "name.html", {...})`; its texts are catalog keys.
4. A new module goes into `ROUTERS` in `app.py`. Order is matching order: a fixed path before a parameterized one
   that would also match it (`/sites/new` before `/sites/{name}`), GET before POST for the same path.
5. Add the path to `ROUTES` in `tests/test_routes.py`; the test checks the set of paths and that none is shadowed.

In tests, replace the outside world in one place — `monkeypatch.setattr("tow.web.services.run_check", fake)` —
patch a web helper where it is looked up (`"tow.web.routes_home.topic_rows"`), and import route functions from
their modules. UI rules (26 px buttons from the shared `button, .btn` rule, no inline `style=`, no inline script)
are enforced by `tests/test_ui_standard.py` and the CSP.

## Periodic work and the runtime

Everything TOW does on its own is managed by one supervisor, `tow run` (`tow.supervisor`): the web server is its child,
scheduled checks, the progress pass and the night copy are child jobs on a schedule, the watchdog is a pass every
10 minutes inside the process. New background work goes there too, never into a task of the OS:

- a new periodic job is an entry in `JOB_ARGS` (`tow/supervisor/core.py`) and its timing in
  `tow/supervisor/schedule.py`, tested with a fake clock in `tests/test_supervisor.py`; a short pass can run in a
  thread like the watchdog's. Its "last run" is kept by the supervisor (`data/run/schedule.json`), never derived
  from a timestamp other code also moves (`health.at_ts` moves with every manual check);
- a daily slot is a local wall time built per calendar day with PEP 495 rules, and is due when the newest good
  result is older than the latest slot — not "every 24 hours", which repeats a night on the 25-hour DST day;
- anything that differs between Windows, Linux and macOS (starting and stopping processes, the port's owner, a job
  object, a signal on the parent's death) belongs in `tow.platform`; tests run the other systems' logic through
  `platform.use(platform.backend_for("linux"))`;
- other processes talk to `tow run` through files in `data/run/control/`, never through signals;
- the watchdog (`tow/watchdog.py` decides, `tow/pulse.py` reads the facts the OS keeps) only reports — only the
  supervisor restarts the web server.
