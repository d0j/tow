# Extending TOW

How to add a torrent client, a messenger, a site, a language, a page, a command or an issue. Each is a module that
TOW discovers by itself, plus its texts in the language files; a contract test checks every module that is found.
Clients and messengers are found by one scan (`tow.plugins`): a module that fails to load is skipped and named in
`SKIPPED` while the others keep working, and a module of the package that is not a plugin is listed in its `HELPERS`
(`clients/spec.py`, `notifiers/registry.py`). Background: [architecture.md](architecture.md). Development setup and
the gate: [CONTRIBUTING.md](../CONTRIBUTING.md).

| Add | Where | Contract | Checked by |
|---|---|---|---|
| [Torrent client](#torrent-client) | `src/tow/clients/<kind>.py` | `TorrentClientAdapter` (`clients/spec.py`) | `test_plugin_contracts.py`, `test_clients_managed.py` |
| [Messenger](#messenger) | `src/tow/notifiers/<kind>.py` | `Notifier` (`notifiers/base.py`) | `test_plugin_contracts.py`, `test_notifiers.py` |
| [Site](#site) | `src/tow/trackers/presets/<name>.py` | `SitePreset` (`trackers/presets/__init__.py`) | `test_tracker_presets.py` |
| [Access provider](#site-access-provider) | `src/tow/protection/solvers/<kind>.py` | `KIND`, `TITLE`, `solve` → `Clearance` | `test_protection_providers.py`, `test_protection.py` |
| [Language](#language) | `src/tow/locales/<code>.json` | the keys of `en.json` | `test_i18n.py` |
| [Page or action](#page-or-action) | `src/tow/web/routes_<topic>.py` | `APIRouter`, `tow.web.services` | `test_routes.py` |
| [Command](#command) | `src/tow/cli/commands/<group>.py` | `Command` (`cli/command.py`), `COMMANDS` | `test_golden_help.py`, `test_cli.py` |
| [Issue](#issue) | `src/tow/issues/kinds/<kind>.py` | `KIND`, `ORDER`, `collect(snapshot)` (`issues/model.py`) | `test_issues.py`, `test_attention_links.py` |

**Read shared names through their module.** A discovered module is imported when it is first needed, which in the
tests can be while a test has replaced a function of another module. A top-level `from tow.trackers import
load_trackers` keeps the object that stood there then, for the rest of the run. Import the module and call through
it each time: `from tow import trackers` … `trackers.load_trackers(config)`, `from tow import clock` …
`clock.local_zone()`. The suite fails the test that leaves its replacement in such a copy (`tests/conftest.py`).

## Torrent client

**Today:** qBittorrent, Transmission 3.0+, Deluge 2.x, all `READY = True`. A module with `READY = False` is found
but not offered in Settings.

A client module declares:

- `KIND`, `TITLE` (the name everywhere: Settings, the header and `tow status` show `client_name()`, which is the
  client's own title in the settings, else `TITLE`), `SHORT` (a short name the settings search and the log also
  recognise), `ORDER`, `SECRETS_KEY`, `READY`, `DEFAULT_PORT`;
- `FIELDS` — the settings card fields (address, port, login, password by default);
- `STEPS` — how to turn on the Web UI in the client; `NOTE` — its quirks (language-file keys `client.<kind>.*`);
- `from_secrets(secrets)` → an object satisfying `TorrentClientAdapter`.

The settings card is not the module's: `clients/view.py` builds every client's card from these constants and
the saved settings (`cards(cfg, secrets, topics)`, as `notifiers/view.py` does for messengers), and
`web/routes_clients.py` saves, checks, adds, removes and chooses the main client for every kind alike.

Discovery falls back to a default for a constant that is missing or wrong; `test_plugin_contracts.py` does not:
`client_module_problems(module)` (`clients/spec.py`) names a `TITLE`, `SECRETS_KEY`, `DEFAULT_PORT` or `ORDER`
that is missing or of the wrong kind, and `FIELDS` that are not a tuple of `ClientField` with `host` and `port`.

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

A long file check after a verified selection is deferred, not failed: `ClientVerificationPending` carries a
validated receipt binding file ids, names, sizes and wanted flags to the hash and folder. `finish_verification`
re-reads that evidence without re-adding, stopping or rewriting priorities. It clears the pending label only
after a real running/completed state; `on_start` records an explicitly allowed retry immediately before its
native request. Initial checks with an unverified partial selection remain paused, never promoted as success.
The orchestration retains the revision but not `once_done` or an added notification until final confirmation.

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
`MAX_UTF16`, `CHUNK_PAUSE`, `suggestion(field)`, `SERVER_FIELDS` (the fields that name a server TOW posts to, such
as ntfy's own server: the settings page refuses one in the home network) — see the `Notifier` docstring.

Everything else is shared: the settings card, encrypted storage, **Send a test** and **Disconnect** with undo, the bell
in the header, and delivery (`notifiers/outbox.py`): retries on a refused connection, 429 (honouring `Retry-After`) and
5xx, never after a request that went out without an answer (it may have arrived; it waits in the queue); a 2xx answer is
a delivered message, whatever its body; a per-channel queue of up to 50 messages; long messages split by lines; one
channel failing never blocks the others. Use `notifiers.base.request()` for HTTP. Tests replace HTTP; no test talks to a
real service.

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
| `infohash_path` | optional page with `{id}` reporting the current revision's labelled info-hash; ambiguous/unavailable answers fall back to the normal procedure |
| `public_probe_path` | audited anonymous page for independent access verification, `/` by default; the protection contract validates the allowlist, never a user-supplied topic or download URL |
| `download_path_aliases` | exact obsolete default routes mapped to their current equivalent; custom routes and stored owner settings stay unchanged |
| `maintenance_markers` | bounded literal markers for this site's maintenance page; never a reason to submit account credentials |
| `browser_login`, `browser_login_hint` | sign-in only through a browser window, and the hint key (`site.<name>.*`) |
| `spec` | the site's settings exactly as `config.example.yaml` ships them (the test compares them) |
| `guess(parts)` | its own rules for a pasted link → new site settings, or `None` |
| `canonical_url(url)` | a CDN or download link → the topic page |

A `config.yaml` without a `trackers:` section gets the `spec` of every preset site, so a new install
watches every known site until the owner edits the list.

Adding a site is that one module, its block in `config.example.yaml`, and its `site.<name>.*` texts if it has any.
`tests/test_tracker_presets.py` checks that it is found and consistent; add a `guess` case to `tests/test_guess.py`
with a made-up topic id.

## Site access provider

Add one module in `tow/protection/solvers/` with `KIND`, `TITLE` and
`solve(provider, origin, timeout) -> Clearance`. Reuse the bounded compatible API in
`protection.providers` where applicable; do not import other adapter modules. `providers.registry()` finds modules
through `tow.plugins.scan`; duplicate kinds are excluded and an unavailable kind does not consume an attempt.
The shared settings card and ordered chain require no adapter-name branches.

The contract is intentionally narrow: the caller supplies a validated local/private API endpoint and a public
origin, never login cookies, credentials or a topic/download path. The caller also supplies the internal
`_probe_path` hint from a trusted site preset; validate it with `validate_probe_path` and use that audited public
page, not an arbitrary configured path. The compatible API helper already does this without changing the
three-argument adapter contract. Return only a validated `cf_clearance`, its exact origin binding, matching
User-Agent, finite expiry and provider connection id. TOW independently replays the audited public page with its
pinned transport before trusting the result. A provider's HTML is never tracker metadata. Enforce the absolute request
budget and bounded response parsing inside the adapter, not only around it.

Raise `ProviderUnavailableError` only for an API-level outage, busy response or protocol mismatch. A site's refusal
or failed challenge must not trip the shared API fuse. The chain stamps an owned attempt identity on its validated
candidate; adapters must not generate one. Conditional acceptance, discard and late API outcomes compare it with
the durable reservation, including after reset. Test continuation to later providers when `max_attempts` is smaller
than the connection count, and the v1 optional-cache read without a write. No browser serialization using pickle.

API availability and verified site access are different observations: an API response is not proof that the
site accepts its clearance. Once the independent public probe succeeds, a challenge on the requested resource
must not discard that useful clearance or start another solve. A failed optional probe must preserve the original
resource error rather than misreport authentication, quota or transport failures as a provider failure.

Test wrong scope, expiry, user-agent, compression/size, redirects, finite time, request-secret isolation and
fallback after refusal. Settings and dry-run must not contact the service. In-process plugins are trusted code,
not sandboxed; keep browser implementations in a separately operated service. See [PROTECTION.md](PROTECTION.md).

## Language

**Today:** English (reference and fallback) and Russian. The language follows the browser (`Accept-Language`, RFC 4647
matching: `zh-Hant-TW` → `zh-Hant` → `zh`) or is chosen in Settings → Language and theme. Messenger messages and
background jobs use the chosen language, or in automatic mode the one the owner's browser last used. A command typed in
a terminal (`tow status`, `tow stop`, `tow doctor`…) uses the chosen language, or in automatic mode the operating
system's (Windows display language; `LC_ALL`/`LC_MESSAGES`/`LANG` on Linux and macOS).

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
4. Translate the time words: `time.month_short.1` … `12`, `time.this_year` and `time.other_year` write the short
   times (“15 Oct, 20:31”); `_meta.datetime` is used for full times. In Settings the new language sits after Auto,
   with English (the fallback) last.

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
- `routes_<topic>.py` — one `router = APIRouter()` per topic: `routes_home`, `routes_topics`, `routes_content`
  (a topic's contents: the file list from the site, a `.torrent` or a magnet), `routes_topic_login`, `routes_check`,
  `routes_undo`, `routes_sites`, `routes_settings`, `routes_clients`, `routes_notifiers`, `routes_backup`,
  `routes_service`, `routes_password`, `routes_auth`, `routes_health`, `routes_history`. A section of Settings
  with cards of its own has its own module (`routes_clients`, `routes_notifiers`); the cards themselves come
  from the family's `view.cards` (`tow.clients.view`, `tow.notifiers.view`).
  Importing a module registers nothing.
- `services.py` — the only way out of the package: config, state, secrets and history, checks, the service,
  restore points and night copies, diagnostics, browser sign-in, the log; also `login_throttle`,
  `locked_state_mutation` and `write_generation` (what `_context.py` drops its reads by). The helper modules use
  it too: `tests/test_web_boundary.py` holds what each still reads directly (`HELPER_ALLOWED`, which only shrinks).
- Helpers: `_context.py` (what a request reads once), `messages.py` (the message after an action: its record,
  its subject and its redirect), `views.py` (Home rows, whether the first-start page is due, the service and backup
  cards of Settings), `check_jobs.py` and `check_actions.py` (the manual check queue), `text.py`,
  `templating.py`, `site_form.py` (what the site forms accept, and the site's configuration from them),
  `site_store.py` (several stores in one transaction), `site_actions.py` and `topic_actions.py` (what the site
  and topic forms do; a topic's record itself comes from `tow.topic_form`). Anything another module uses has a
  name without a leading underscore (`tests/test_module_boundaries.py`), and what two route modules share lives
  in a helper: a route module never imports another (`tests/test_web_boundary.py`).

**Steps:**

1. Add a function to the right `routes_<topic>.py` (or a new module with `router = APIRouter()`):
   `@router.get(...)` / `@router.post(...)` returning `-> Response`; for JSON, `-> dict[str, Any]` and
   `response_model=None`.
2. Read or change data, clients, sites or the service only through `services`. A read-modify-write of the state
   gets `@services.locked_state_mutation` under the route decorator. From outside `tow.web` a route module (and
   a `*_actions.py` module) imports only the pure helpers `tests/test_web_boundary.py` lists; a new way out goes
   into `services`. A form with more than a few checks is handed to an action module, so the route only reads
   the form and answers.
3. Answer an action with `flash_redirect(url, "text.key", "ok" | "info" | "warn" | "err")` from `tow.web.messages`: the
   text stays on the server, only a token goes into the URL. A topic, site or client route gets its subject ("Harbour
   Clock — …") by itself; a background job passes `subject=`. In the page, `window.towMessages.showMessage`, from
   `static/messages.js`, shows a message the same way; a lasting problem is a note (`_note.html`), not a message. A page
   is a template in `src/tow/templates/` rendered with `TEMPLATES.TemplateResponse(request, "name.html", {...})`; its
   texts are catalog keys.
4. A new module goes into `ROUTERS` in `app.py`. Order is matching order: a fixed path before a parameterized one
   that would also match it (`/sites/new` before `/sites/{name}`), GET before POST for the same path.
5. Add the path to `ROUTES` in `tests/test_routes.py`; the test checks the set of paths and that none is shadowed.

In tests, replace the outside world in one place — `monkeypatch.setattr("tow.web.services.run_check", fake)` —
patch a web helper where it is looked up (`"tow.web.routes_home.topic_rows"`), and import route functions from
their modules. UI rules (26 px buttons from the shared `button, .btn` rule, no inline `style=`, no inline script)
are enforced by `tests/test_ui_standard.py` and the CSP.

## Issue

**Today:** `data_lost`, `check_stale`, `check_blocked`, `client_down`, `history_rebuilt`, `history_trimmed`,
`site_needs_login`.

An issue is something that needs the owner now, said from saved state: Home's "Needs attention" lists it as a
link to the problem, and `tow status` counts it. A kind is one module in `src/tow/issues/kinds/`, found by
`tow.plugins.scan` (a module that fails to load is skipped, named in `tow.issues.SKIPPED` and logged). It declares:

- `KIND` — its name; `ORDER` — its place among the kinds of the same severity;
- `collect(snapshot) -> list[Issue]` — pure: it reads only the `Snapshot` (`state`, `config`, `now`,
  `next_check_at`, `data_lost`, and `health`, `topics` from the state) and never a file, a client or a site. A fact
  kept elsewhere becomes a new `Snapshot` field, filled by the callers (`tow.web.views.issues_now` through
  `tow.web.services`, and `tow status`).

Each `Issue` (`issues/model.py`) carries:

- `kind`, and an `id` stable while the problem lasts (`<kind>` or `<kind>:<subject>`): two issues with one id are
  one problem; `collect` merges them, adds their `count`, joins their `subjects` and takes `target_many`;
- `severity` — a `tow.status` tone, `"bad"` or `"warn"`; from an error class, `tow.status.error_tone(cls)`;
- `message` — a catalog key (`issues.<kind>`, in every language file) and its values; a value named `at` is a time
  in epoch seconds. Say what is wrong, or the action the link takes ("Sign in to {site}"), never "open X";
- `target` — `Target(page, open, filter, anchor, note)`: the page (`home`, `settings`, `history`, `sites`), what it
  opens (a Settings section, a Home topic, a site), the page's own filters, and the id of the element that shows the
  problem in full.
  An empty `anchor` lands on the issue's own note on that page (History shows those). `note=True` when that element
  does not say what is wrong by itself (a Settings card): the page puts the issue's text inside it with
  `{{ issue_notes_at(notes, '<anchor>') }}` (`_issue_notes.html`, the page's `notes` from `views.issue_notes`);
- `fix` — `Fix(action, subject, label)`, the control that resolves it, or `None` when there is nothing to do. The
  page marks that control with `{{ fix_attrs('<action>', subject) }}` (and a `<details>` that holds it with
  `{{ fix_open(...) }}`); the link's `?fix=` focuses it, with or without a script.

**Steps:**

1. Add `src/tow/issues/kinds/<kind>.py` with `KIND`, `ORDER` and `collect`; kinds do not import each other
   (`tests/test_layers.py`).
2. Add `issues.<kind>` to every language file.
3. Make sure the target page has the anchor, the fix control (`fix_attrs`) and, for a `note`, the
   `issue_notes_at` there; a new page goes into `ISSUE_PAGES` in `tow/web/views.py`.
4. Test the rule in `tests/test_issues.py` and follow the link in `tests/test_attention_links.py` (`LANDINGS`
   names a test per kind and fails without one).

## Command

Every `tow` command is a row of one ordered table, `COMMANDS` in `src/tow/cli/commands/__init__.py`, in the
order `tow --help` lists them. A row is a `Command` (`cli/command.py`) declared in the module of its group
(`commands/service.py`, `data.py`, `security.py`, `checks.py`):

- `name`; `configure(sub)` adds its parser with `parser.add_command` (its line in `tow --help` and its
  description, both catalog keys under `cli.help.*`; `listed=False` keeps it out of the list) and its options,
  `--json` with `parser.json_option`;
- `run(args)` does it and returns an exit code (`EXIT_*` of `cli/command.py`). It imports what it needs inside
  the function, so `import tow.cli` stays cheap for every command; what it prints goes through `cli/render.py`
  (`render.show` with a `*_text` for a person, the whole result with `--json`);
- how it runs: `layout` (it finds its install itself, started without a launcher), `background` (a child of
  `tow run`: outside a terminal it speaks the message language), `warn_open_folders` (it starts or serves TOW),
  `protects_folders=False` (it handles the install's folders itself, as `permissions` does).

`tow.cli` runs the row the command line names behind one error boundary: an unexpected exception is exit code 3
and a sentence, never a traceback. A new command changes `tow --help`: regenerate the golden pages
(`uv run --frozen python tests/test_golden_help.py`) and review the diff. A module of `tow.cli` imports the
others, never the package itself; only `tow.__main__` imports the package (`tests/test_layers.py`).

## Periodic work and the runtime

Everything TOW does on its own is managed by one supervisor, `tow run` (`tow.supervisor`): the web server is its child,
the global check (`--global-only`), the personal timers (`--timer-only`), the progress pass, the disk-space pass
(every 5 minutes while a topic waits for space, `--space-only`) and the night copy are child jobs on a schedule, the
watchdog is a pass every 10 minutes inside the process. New background work goes there too, never into a task of
the OS:

- a new periodic job is a row in `JOBS` (`tow/supervisor/jobs.py`): its child's arguments, its time limit, its
  cadence and first delay, the key of `data/run/schedule.json` that keeps its last start. Table order initializes
  a new schedule and breaks ties; afterwards the oldest overdue child takes precedence. It remains the key order
  of `next` in `status.json`. `JOB_ARGS` and
  `JOB_TIMEOUT_SEC` in `tow/supervisor/core.py` and the cadences of `tow/supervisor/schedule.py` are read from it;
  a condition for being due at all (like the disk-space pass only while a topic waits) is code in
  `Schedule._due_at`, a special start (like the timers' reservation) code in `Supervisor._start_job`. Its
  arguments must parse as a `tow` command with the scope flag it means (`tests/test_supervisor_jobs.py`), its
  timing is tested with a fake clock in `tests/test_supervisor.py` and `tests/test_golden_schedule.py`; a short
  pass can run in a thread like the watchdog's (a row without arguments). Its "last run" is kept by the supervisor
  (`data/run/schedule.json`), never derived from a timestamp other code also moves (`health.at_ts` moves with
  every manual check);
- a daily slot is a local wall time built per calendar day with PEP 495 rules, and is due when the newest good
  result is older than the latest slot — not "every 24 hours", which repeats a night on the 25-hour DST day;
- anything that differs between Windows, Linux and macOS (starting and stopping processes, the port's owner, a job
  object, a signal on the parent's death) belongs in `tow.platform`; tests run the other systems' logic through
  `platform.use(platform.backend_for("linux"))`;
- other processes talk to `tow run` through files in `data/run/control/`, never through signals;
- the watchdog (`tow/watchdog.py` decides, `tow/pulse.py` reads the facts the OS keeps) only reports — only the
  supervisor restarts the web server.
