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
pwsh scripts/gate.ps1 -Quick     # without tests and the wheel
pwsh scripts/gate.ps1 -Staged    # only what the next commit contains
```

The hooks run it for you: **pre-commit** runs `-Staged`, **pre-push** requires the full gate on exactly the content
being pushed. CI runs the same gate on Windows, Linux and macOS.

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

## Commits and pull requests

- One logical change per commit, with its test. The subject is `area: what changed` in English (`web: …`,
  `clients: …`, `docs: …`); the body says why.
- User-visible changes get a line in `CHANGELOG.md`.
- Keep pull requests small and focused; fill in the template. Do not include personal data in code, tests,
  screenshots or logs.

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).

## Publishing a release

Merge the gated release commit through a pull request, fetch `origin/main`, and create an annotated
`vX.Y.Z` tag on that commit (matching `pyproject.toml`). Publish it with:

```sh
uv run --frozen python scripts/publish-release.py vX.Y.Z
```

When a `backup` remote is configured, this first atomically synchronizes its `main` and the release tag,
then confirms both by reading them back. Only then is the tag sent to `origin`; existing remote history
is never overwritten. The post-commit mirror alone is not enough: it cannot copy a later GitHub merge
or a tag created after the commit. Git's full pre-push gate still applies. Wait for the release workflow
and its installer checks to finish before updating a runtime installation.
