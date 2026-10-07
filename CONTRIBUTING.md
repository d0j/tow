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
```

The hooks run it for you: **pre-commit** runs `-Staged`, **post-rewrite** runs `-Quick` after an amend or a
rebase (git ignores its result, so it can only warn, loudly), **pre-push** requires the full gate on exactly the
content being pushed. CI runs the same gate on Windows, Linux and macOS.

Linux checks and installer smoke tests run on both Ubuntu 24.04 and 26.04. The legacy
`ubuntu-latest` check name is retained for branch protection, but its runner is explicitly
`ubuntu-24.04`; GitHub's future migration of the floating label cannot remove older-LTS coverage.

## Tests

- `uv run --frozen pytest -q` for a quick run; the gate runs the suite in random order (the seed is printed) with
  branch coverage and a threshold in `pyproject.toml` — raise it as coverage grows, never lower it.
- The guard in `tests/conftest.py` refuses real network, processes, signals and writes outside the test's temp
  folder. Each test gets a throwaway master key and its own `TOW_ROOT`. `@pytest.mark.allow_git` lets only `git`
  through, on a throwaway repository.
- Use fake clients, fake HTTP and injected platform backends (`tow.platform.use`); never a live client, site or
  messenger.
- **Test data is synthetic**: made-up titles (`Show A`, `Сериал А`), topic ids (`1234567`), private-range or
  documentation IP addresses, paths like `D:\TV` or `/srv/media`. No real topic links, user names or addresses.

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

The release commit (version in `pyproject.toml`, changelogs) has the subject `release: vX.Y.Z - <summary>`.
Pull requests that touch the installers, the updater or the dependencies also run the `installers`
workflow: the Windows bundle and `install.ps1`, and the update of the latest release to the pull request
(`scripts/update-smoke.py`: a broken copy must roll back, then the real update) on Windows and Linux.

Merge the release commit through a pull request and wait for `ci` on `main`. Fetch `origin/main` and create
an annotated tag on the merge commit with the message `TOW X.Y.Z`
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

The release workflow does not run the gate again: it requires the tag on `origin/main` and a passed `ci`
run of its commit, builds and tests the bundle and the installers, updates the latest release to the tag,
then uploads everything to a draft release, reads it back and only then publishes it. Wait for it to
finish before updating a runtime installation.

Before a public-history audit, scan a separate complete clone holding only the public refs
(`uv run --frozen python scripts/history-scan.py --repo /path/to/public-clone`); exit 0 is a complete
scan without candidates, 1 needs review, 2 is incomplete. What it covers and its budgets: `--help`.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
