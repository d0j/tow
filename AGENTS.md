# Rules for contributors

The rules every change to TOW keeps. Read `docs/ROADMAP.md` for the current state and `docs/architecture.md` for
the structure, then the relevant source, tests and `git log` of the files before editing. `CONTRIBUTING.md` has the
development setup, the term table and the release steps.

## Product boundary

- TOW observes trackers, torrent clients, filesystem evidence, schedulers and notifications.
- TOW does not download, retrieve, copy or move media bytes itself.
- A normal `apply` may hand a torrent to the explicitly selected client and may request a user-requested client
  relocation through `set_location`.
- Deleting a TOW topic removes only the TOW observation. It does not delete the client torrent; history is retained.
- `dry-run` is preview-only: no client add/move, no TOW state/history/config/secrets/mirror/log writes, no cookie
  persistence or messenger notification, and no `.torrent` download from download-limited sites.
- A client add is successful only after read-back confirmation. Never report a failed or unconfirmed add as success.

## Home status-colour contract

The Home status dot and site icon describe the latest TOW observation, not whether a browser can open the topic page
and not whether media is complete. Precedence: `last_error` first, then `last_changed`, then `last_ok`, otherwise
unknown.

- **Green `ok`**: the latest check completed (`last_ok=true`) and there is no `last_error`.
- **Amber `warn`**: tracker/mirror transport is degraded (error classes `tracker` and `frozen`: mirror cooldown, all
  mirrors paused, cross-host redirect to an unconfigured host, Cloudflare, all hosts failed). It does not by itself
  prove the torrent or the client is broken.
- **Red `bad`**: a non-tracker error: torrent client, TOW validation, storage/path, secret/config, authentication,
  quota or unknown. Red is an actionable failure.
- **Grey `mut`**: no result yet (`last_error` absent, `last_changed` false, `last_ok` false/unknown); say "not
  checked yet", never "ok".
- **Blue `new`**: `last_changed=true` without an error: new data was observed. An event, not a health result.

The site icon shows the site alone: amber for transport problems (tracker, frozen, Cloudflare, login, daily limit),
red only for site-side problems (topic removed, not a torrent page, no site); a torrent-client, folder or disk error
does not colour it. The status dot keeps the precedence above.

The red delete button and the pause icon are controls, not health colours. Preserve the underlying `last_error`;
never clear it or claim green to make Home look healthy. If the same error appears on every row, distinguish
tracker degradation from a client or TOW failure before changing state.

## UI standard

- **Every action button is 26 px high, everywhere** (Save, Check, Add, Remove, Cancel…), with 12.5 px text. The size
  comes only from the shared `button, .btn` rule in `app.css` (`--btn-h: 1.625rem`); no page, card or form sizes a
  button itself, and there is no inline `style=`. Square icon buttons (`.ico`, `.row-ico`, `.flash-x`) are the only
  exception. Inputs and selects are 36 px with 14 px text, 44 px with 16 px text on phones and touch
  screens (`--field-h`, also from `app.css` only).
- **Fields are as wide as what they hold**, by one class from `app.css`: `.w-2xs` 72 px (attempts, order), `.w-xs`
  88 px (ports, intervals, days), `.w-s` 192 px, `.w-m` 240 px (user name, password, reminder, host), `.w-l` 416 px,
  `.w-xl` 576 px (a topic's name or folder, each on its own row). Fields that belong together share a `.field-row`,
  which wraps by those widths; on a phone (≤ 720 px) text fields fill the row and numbers stay short. A rule field is
  a one-row textarea that grows. Links, paths and rules carry `autocapitalize="off"`, `autocorrect="off"` and
  `spellcheck="false"`; an example in a field is `common.example` ("e.g. …"), a real default is said under it with
  `common.default_path` ("Default: …").
- **At most one solid button per card or form**; the others are `ghost`. Remove, Delete and Disconnect are
  `ghost danger`; Restore is `ghost`.
- **Confirmations** use TOW's own dialog, never `window.confirm`: `data-confirm="<text>"` and
  `data-confirm-label="<action>"` on the form or its submit button (`data-confirm-danger` for a destructive one: Cancel
  has the focus first), or `towConfirm({text, confirmLabel, danger})` in a script. Without JavaScript the form submits
  as before; a server confirmation page stays where one exists.
- A file is chosen with `file_input` from `_file.html` (buttons that need the file carry `data-needs-file`). A
  password, token or key field has the eye (`reveal`); a stored one gets `saved_hint` in its label, the placeholder
  `web.secret.keep_placeholder`, and one `keep_note` per form.
- `tests/test_ui_standard.py` enforces this; a new screen is also measured in a real browser before release.
- Settings spans the page on a desktop (its forms keep their own width); the page that deletes a backup copy
  is as wide. Its cards follow one pattern: a status pill (only when it says something
  the section does not), fields, then the buttons; "How to connect" steps sit above the fields and are open while the
  card is not set up. Messengers that are not connected and torrent clients that answer are folded to one line; the
  others are open.

## Changes and rollback

- Git is the change record. Preserve pre-existing worktree changes; before editing, note `git status --short`, the
  branch and HEAD.
- One logical change per commit, with a message that says what changed and why (the `commit-msg` hook checks its
  form); the gate passes before every commit. User-visible changes also get a line in `CHANGELOG.md` and
  `CHANGELOG.ru.md`, and a change to `README.md` or a guide with a Russian twin in `docs/ru/` the same change in
  `README.ru.md` or that twin.
- A release is a commit `release: vX.Y.Z - <summary>` (version in `pyproject.toml`, both changelogs) merged through
  a pull request, then an annotated tag `vX.Y.Z` with the message `TOW X.Y.Z` on that merge on `origin/main`,
  published with `uv run --frozen python scripts/publish-release.py vX.Y.Z`.
- When asked to undo changes or go back to a previous version: stop new work, `git revert` the commits in question
  (or redeploy the previous tag to the runtime install), rerun the gate and report. Never rewrite pushed history.
- If the checkout has a `backup` remote, the post-commit hook mirrors commits whose content passed the full gate.
- **One author.** The owner is the only author of TOW. No `Co-Authored-By` trailer, no "Generated with …" line,
  no name of a model, assistant or code generator, no signature, and no word that any of them wrote or developed
  TOW: not in commits, tags, pull requests, issues, release notes, code, comments, docs or this file, and no such
  account among the GitHub contributors. Tool folders (worktrees, scratch, settings) stay outside the repository
  or in `.git/info/exclude`, never in tracked files.
- Runtime state and credentials are never committed. `data/secrets.*`, `master.key`, tokens, cookies, real topic
  links, personal paths and IP addresses never appear in code, tests, logs, commits or chat. Test data is synthetic.

## Development vs runtime install

- A runtime install is one portable folder `<runtime>` with `app/` (a clone at a tag, or a release archive),
  `config.yaml`, `data/`, `keys/master.key`, `backup/` and `runtime/` (its Python and uv cache); TOW writes only
  inside it (`docs/PORTABLE.md`). Never edit files there: change the development checkout, commit, tag, then update
  the install (`scripts/deploy.ps1 -Ref <tag>` on Windows, the command `tow update --ref <tag>` prints on any OS).
  The oldest version an update goes back to is v1.18.0 (v1.22.0 for an install without git; v1.23.0 once v1.23
  has written the data, which older versions cannot read). The Windows bundle
  and the installers are built and tested by `.github/workflows/release.yml` (`docs/PORTABLE.md` §1a).
- The runtime is one supervised service, `tow run` (`tow.supervisor`): a managed web-server child,
  scheduled child jobs and watchdog duties. It is
  controlled through `data/run/control/{restart,stop}` (`tow restart`, `tow stop`, Settings), never by killing
  processes. Its web server never outlives it (`finally`, `tow serve --parent-pid`, a Windows job object, Linux
  `PR_SET_PDEATHSIG`); a server a dead supervisor left on the port is taken over at the next start, anything else
  on the port is never touched. The check cadence is its own (`data/run/schedule.json`), never `health.at_ts`.
  The watchdog only reports: nothing but `tow run` restarts the web server. Autostart (`tow.autostart`) is the
  only thing written outside the install, and only on request. The five Windows tasks of 1.17 and all code for
  them are gone since 1.21.
- All paths come from `tow.paths` (`root()`, `data_dir()`, `key_file()`, `tmp_dir()`…), all OS differences from
  `tow.platform` (`current()`, `is_windows()`, `this_os()`; tests inject a backend with `platform.use`). No
  `os.name`/`sys.platform` checks, system temp, `%LOCALAPPDATA%` or `~` paths elsewhere. `scripts/update.py` is
  the exception: standard library only and Python 3.11 syntax (it runs with the install's base Python, outside
  TOW); its uv/Python environment comes from `launcher_env`, kept in step with `scripts/tow` and
  `scripts/tow-env.cmd`.
- On a development machine never register autostart or change scheduled tasks (read-only queries are fine): the
  names are one per user and would take over a real install. Code refuses a development checkout, and tests inject
  the OS runner and an isolated home.
- In a development checkout the checkout itself is the install root: its `config.yaml`, `data/` and `keys/` are
  local scratch state. Tests start from `config.example.yaml` with `TOW_ROOT`/`TOW_HOME` in their own temp folder
  and write only there. A real smoke run of `tow run` or `update.py` belongs in a scratch install on another port.

## Portability and verification

- Do not hardcode a checkout path. `TOW_ROOT` names the install; `TOW_CONFIG`/`TOW_HOME` override the config file
  and the data folder.
- Use the locked environment: `uv run --frozen ...`. A bare system Python result is not evidence.
- Minimum after edits: the gate (frozen pytest, ruff, mypy, compileall, whitespace, wheel smoke) and rendered UI/API
  checks where relevant.
- Never call a live destructive client operation to prove a test. Use fake adapters and temp state. Say which
  evidence you have: source checkout, installed wheel, browser, or a real client.
- The test guard (`tests/conftest.py`) refuses processes, network, signals and writes outside the temp folder;
  `@pytest.mark.allow_git` lets only `git` through (the updater's tests on a throwaway clone).
  `tests/integration/` (real clients, real autostart) carries `@pytest.mark.real_system("TOW_REAL_...")`:
  skipped unless that variable is 1, which only `.github/workflows/real.yml` (and the `autostart.yml` it and
  `release.yml` run) sets on a runner.
- Web: one `APIRouter` per `tow/web/routes_*.py`, included by `tow.web.app.create_app()`; everything outside the web
  package is called through `tow.web.services`, which is where tests patch it (`docs/EXTENDING.md`, "Page or
  action"). `tow.web` itself exports only `app` and `create_app`.
- User-facing text is a catalog key (`src/tow/locales/*.json`), never a literal in code.

## Entry points

```sh
pwsh scripts/gate.ps1            # full gate: lock, ruff format + check, mypy, compileall, whitespace, pytest, wheel
pwsh scripts/gate.ps1 -Quick     # without the test run and the wheel smoke (the tests are only collected)
pwsh scripts/gate.ps1 -Staged    # what the next commit contains (the pre-commit hook)
```

The repository is movable: the launchers (`scripts/tow.cmd`, `scripts/tow`) derive the install from their own
location; `tow setup` rebuilds the environment after a move.
