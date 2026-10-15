# Contributing

Thanks for helping. Bug reports, site presets, clients, messengers, translations and fixes are welcome. Read
[AGENTS.md](AGENTS.md) for the product rules every change keeps and
[docs/architecture.md](docs/architecture.md) for the structure.

## Setup

You need git, [uv](https://docs.astral.sh/uv/) ≥ 0.12 and [PowerShell 7](https://learn.microsoft.com/powershell/scripting/install/installing-powershell)
(`pwsh`, for the gate on any OS). uv installs Python 3.14 from `.python-version`.

```sh
git clone https://github.com/d0j/tow
cd tow
uv sync --frozen
cp config.example.yaml config.yaml        # Windows: Copy-Item config.example.yaml config.yaml
git config core.hooksPath .githooks
uv run --frozen tow serve                 # http://127.0.0.1:8787
```

In a development checkout the checkout itself is the install root: its `config.yaml`, `data/` and `keys/` are
local scratch state and are gitignored. Never point a development checkout at a real install, and never register
autostart from it.

## The gate

```sh
pwsh scripts/gate.ps1            # everything: uv.lock, ruff format + check, mypy, compileall, whitespace, pytest, wheel
pwsh scripts/gate.ps1 -Quick     # without the test run and the wheel (the tests are only collected)
pwsh scripts/gate.ps1 -Staged    # only what the next commit contains
pwsh scripts/gate.ps1 -Audit     # also checks the locked dependencies for known vulnerabilities (network)
```

Coverage comes from `pytest-cov` and `coverage`, locked dev dependencies. An environment without them runs the
tests without coverage and says so (`gate: SKIPPED coverage`); in CI (`CI=true`) or with
`TOW_GATE_REQUIRE_COVERAGE=1` the gate fails instead. The audit runs `pip-audit` at the version pinned in
`scripts/gate.ps1`. The test run has a 30-minute deadline, collection and worker
shutdown included (`TOW_GATE_PYTEST_DEADLINE_SEC` overrides it): past it the gate stops that run's own processes
and fails, and the tree is not recorded as passed. A test run (or the collection of `-Quick`) that leaves a new
untracked, not ignored file in the checkout fails the gate, which names the file.

The hooks run it for you: **pre-commit** runs `-Staged`, **post-rewrite** runs `-Quick` after an amend or a
rebase (git ignores its result, so it can only warn, loudly), **pre-push** requires the full gate on exactly the
content being pushed. CI runs the same gate on Windows, Linux and macOS.

Linux checks and the `install.sh` smoke tests run on both Ubuntu 24.04 and 26.04 (the update test of
the `installers` workflow on 24.04). The legacy
`ubuntu-latest` check name is retained for branch protection, but its runner is explicitly
`ubuntu-24.04`; GitHub's future migration of the floating label cannot remove older-LTS coverage.

## Tests

- `uv run --frozen pytest -q` for a quick run (`-n auto` spreads it over every core); the gate runs the suite on
  every core in random order (the seed is printed; every worker uses it) with branch coverage and a threshold in
  `pyproject.toml` — raise it as coverage grows, never lower it. Tests that share an expensive module fixture
  carry `@pytest.mark.xdist_group(...)` so that one worker runs them all.
- The guard in `tests/conftest.py` refuses real network, processes, signals and writes outside the test's temp
  folder. Each test gets a throwaway master key and its own `TOW_ROOT`. `@pytest.mark.allow_git` lets only `git`
  through, on a throwaway repository. After each test it also fails a test whose replacement stayed in a name
  another module copied at import (`from tow.x import name` in a module first imported during that test): read
  such names through their module ([docs/EXTENDING.md](docs/EXTENDING.md)).
- `tests/integration/` tests real torrent clients and the OS autostart. They are marked
  `@pytest.mark.real_system("TOW_REAL_...")` and skipped unless that variable is 1, which only
  `.github/workflows/real.yml` sets on a CI runner: never run them on your machine.
- Use fake clients, fake HTTP and injected platform backends (`tow.platform.use`); never a live client, site or
  messenger.
- Guard tests keep the structure while code moves: `test_layers.py` (which module may import which),
  `test_web_boundary.py` (the web reaches the rest through `tow.web.services`), `test_module_boundaries.py`
  (private names), `test_stdlib_only.py` (the updater and its worker), `test_updater_contract.py`,
  `test_public_api.py` (public names and signatures), `test_patch_targets.py` (a patched name is one the code
  reads when it runs) and the golden texts in `tests/golden/` (`tow --help` in both languages, the schedule on a
  fake clock, the Settings page). Their exception lists (`ALLOWED`, `HELPER_ALLOWED`…) may only shrink. After an
  intended change, regenerate a golden file with `uv run --frozen python tests/<its test>.py` and review the diff.
- **Test data is synthetic**: made-up titles (`Show A`, `Сериал А`), topic ids (`1234567`), private-range or
  documentation IP addresses, paths like `D:\TV` or `/srv/media`. No real topic links, user names or addresses.
  A test of the public-address guards needs an address TOW counts as public, which documentation ranges are
  not: it uses the named constants of `tests/helpers.py` (`PUBLIC_IPV4`, `PUBLIC_IPV4_B`, `PUBLIC_IPV4_C`,
  `PUBLIC_IPV6`), never another literal; nothing connects to them. `test_docs_facts.py` checks this.

## Code

- Python 3.14, strict mypy for the whole package, ruff (format and lint, McCabe ≤ 20, broad `except` only at a
  boundary with a reason).
- Paths only from `tow.paths`, OS differences only through `tow.platform`.
- The web layer calls everything outside `tow.web` through `tow.web.services`.
- How to add a client, messenger, site, language or page: [docs/EXTENDING.md](docs/EXTENDING.md).

## Texts and languages

- Every user-facing text is a key in `src/tow/locales/en.json` with its Russian counterpart in `ru.json`; code only
  holds keys. `tests/test_i18n.py` checks keys, placeholders, plural forms and inline markup.
- English is plain and short. Russian addresses the user formally (на «вы») and avoids English terms.
- Errors are `TowError("key", **params)`; their status colour never depends on the wording.
- One thing has one word in the interface, the guides, the changelogs and the messages:

| Thing | English | Русский |
|---|---|---|
| A tracker page TOW follows | topic | раздача |
| A tracker TOW can read | site | сайт |
| Another address of a site | mirror | зеркало |
| One `.torrent` of a topic | version | версия |
| qBittorrent, Transmission, Deluge | torrent client | торрент-клиент |
| A topic's torrent inside the torrent client | torrent | торрент (не «раздача») |
| Asking a site or the client | check | проверка |
| Which files to download | selection (all files, choose files, episodes by number, files by pattern) | выбор файлов (все файлы раздачи, выбрать файлы, серии по номерам, файлы по маскам) |
| The schedule of checks | global timer · personal timer | общий таймер · личный таймер |
| The daily copy of the data | nightly backup (Settings: Nightly backups) | ночная копия (в настройках: «Ночные копии») |
| A copy made by **Create a backup** or before a risky change | restore point (Settings: Backups made by hand) | точка восстановления (в настройках: «Копии по кнопке») |
| The encrypted transfer file | TOW file (`.towx`) | файл TOW (`.towx`) |
| `keys/master.key` | master key | мастер-ключ |
| Saved logins of sites, clients and messengers | passwords and tokens | пароли и токены (не «секреты») |
| Opening TOW from other devices | network access · sign in · sign out | доступ по сети · вход · выход |
| Signing in to a site | sign in (not "log in") | вход (не «авторизация») |
| Putting the last change back | Undo | Вернуть |
| The reporting duty of `tow run` | watchdog | сторож |
| Starting TOW with the computer | autostart | автозапуск (не «автозагрузка») |
| Installing another release | update · roll back | обновление · откат |
| The program that installs a release | updater | программа обновления |
| The list of topics | Home | Главная |
| Help inside TOW · the guide in `docs/` | Guide · user guide | Инструкция · Руководство |
| A torrent's identity | hash | хеш |
| Saved torrent contents kept for reuse | cache | кеш |

## Commits and pull requests

- One logical change per commit, with its test. The subject is `area: what changed` in English (`web: …`,
  `clients: …`, `docs: …`), at most 72 characters; then a blank line and a body that says why and how it was
  verified. The `commit-msg` hook in `.githooks` checks this form and refuses trailers such as `Co-Authored-By`.
- User-visible changes get a line in `CHANGELOG.md` and the same in `CHANGELOG.ru.md`. A change to `README.md`,
  `docs/guide.md` or `docs/install.md` makes the same change to `README.ru.md` or the file of that name in
  `docs/ru/`, in the same commit.
- Keep pull requests small and focused; fill in the template. Do not include personal data in code, tests,
  screenshots or logs.

## Publishing a release

A release is one pull request from a work branch. It holds the area commits (`web: …`, `clients: …`), each
with its tests and its lines under `## [Unreleased]` in both changelogs, and ends with one release commit
`release: vX.Y.Z - <summary>` that changes nothing but the version (`pyproject.toml`, `uv.lock`) and turns
`[Unreleased]` into the dated section of that version in `CHANGELOG.md` and `CHANGELOG.ru.md`. Code never goes
into the release commit.
Pull requests that touch the installers, the updater or the dependencies also run the `installers` workflow: the
Windows bundle and `install.ps1`, and the update of the latest release to the pull request
(`scripts/update-smoke.py`: a broken copy must roll back, then the real update) on Windows, Linux and macOS.

Merge the pull request with **Rebase and merge** once it is up to date with `main` (its last `ci` run then
covers the merge; otherwise wait for `ci` on `main`): `main` gets every commit as it is, with no merge commit,
and the release commit is its tip. GitHub cannot rebase-merge more than 100 commits; split a longer branch.
GitHub deletes the merged head branch (the repository setting); delete the local one. Fetch `origin/main` and create an annotated tag on the release commit with the message `TOW X.Y.Z`
(`git tag -a vX.Y.Z -m "TOW X.Y.Z" origin/main`). Publish it with:

```sh
uv run --frozen python scripts/publish-release.py vX.Y.Z
```

It refuses a tag that is not annotated, whose message is not `TOW X.Y.Z`, that does not match
`pyproject.toml`, that is not on `origin/main`, or that has no release commit. When a `backup` remote is
configured, it first atomically synchronizes its `main` and the release tag, then confirms both by reading
them back. Only then is the tag sent to `origin`; existing remote history is never overwritten. The
post-commit mirror alone is not enough: it cannot copy a later GitHub merge or a tag created after the
commit. Git's full pre-push gate still applies.

The release workflow does not run the gate again: it requires the tag on `origin/main` and a passed `ci` run of
its content (on its commit or any commit with the same tree, such as the head of the merged pull request: the
run of the same content on `main` is not waited for), builds and tests the bundle and the installers, updates
the latest release to the tag, then uploads everything to a draft release, reads it back and only then
publishes it. Wait for it to finish before updating a runtime installation.

Before a public-history audit, scan a separate complete clone holding only the public refs
(`uv run --frozen python scripts/history-scan.py --repo /path/to/public-clone`); exit 0 is a complete
scan without candidates, 1 needs review, 2 is incomplete. What it covers and its budgets: `--help`.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
