"""runtime/uv.toml (X39): uv reads the install's own settings, never the account's uv.toml.

The updater writes the target's [tool.uv] settings before the target's sync, the previous code's
before a rollback's sync, repairs the file where no uv runs (an install an older updater brought,
a recovered switch) and refuses an unusable file before TOW stops. The launchers write the same
head from scripts/runtime-uv.toml (tests/test_launchers.py); the last test runs the real uv.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import test_update_archive as archive
from test_update import SCRIPT, updater
from test_update_archive import Machine, run, tarball

ROOT = SCRIPT.parents[1]
MANIFEST = "https://example.test/pinned/download-metadata.json"
# The archive tests' install and the local GitHub, as fixtures of this module too.
install = archive.install
github = archive.github
OWN = "native-tls = true\n"


def _pyproject(version: str, tool_uv: str) -> str:
    return f'[project]\nname = "tow"\nversion = "{version}"\n\n[tool.uv]\n{tool_uv}'


TARGET_UV = f'required-version = ">=0.12"\npython-downloads-json-url = "{MANIFEST}"\n'
PREVIOUS_UV = 'required-version = ">=0.11"\n'


class Recording(Machine):
    """Records runtime/uv.toml as each uv sync finds it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[dict[str, Any]] = []

    def uv_sync(self) -> None:
        path = Path(self.env["UV_CONFIG_FILE"])
        self.seen.append(tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})
        super().uv_sync()


def _config(install) -> Path:
    return install["root"] / "runtime" / "uv.toml"


def test_the_launchers_template_is_pyprojects_tool_uv_as_the_updater_writes_it():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    template = (ROOT / "scripts" / "runtime-uv.toml").read_text(encoding="utf-8")
    # Byte for byte: the launchers' copy and the updater's never take turns rewriting the file.
    assert updater.uv_settings_text(pyproject) == template
    # Every operational setting of the project, the required uv and the manifest among them.
    settings = tomllib.loads(pyproject)["tool"]["uv"]
    assert tomllib.loads(template) == settings
    assert {"required-version", "python-downloads-json-url"} <= set(settings)
    # A standalone uv.toml: uv refuses a [tool.uv] header there (measured).
    assert "[tool" not in template
    assert template.splitlines()[-1] == updater.UV_CONFIG_MARKER


def test_the_updaters_uv_reads_only_the_installs_settings(tmp_path):
    app = tmp_path / "TOW" / "app"
    _uv, env = updater.launcher_env(app, {"UV_NO_CONFIG": "1", "UV_OFFLINE": "1", "UV_INDEX_URL": "https://x"})
    assert env["UV_CONFIG_FILE"] == str(tmp_path / "TOW" / "runtime" / "uv.toml")
    assert "UV_NO_CONFIG" not in env  # would drop .python-version (measured)
    assert (env["UV_OFFLINE"], env["UV_INDEX_URL"]) == ("1", "https://x")  # the owner's controls stay


def test_the_owners_lines_stay_and_tows_are_written_again():
    head = updater.uv_settings_text(_pyproject("1.33.1", TARGET_UV))
    stale = '# old\nrequired-version = ">=0.1"\n' + updater.UV_CONFIG_MARKER + "\n" + OWN
    assert updater.uv_config_text(_pyproject("1.33.1", TARGET_UV), stale) == head + OWN
    assert updater.uv_config_text(_pyproject("1.33.1", TARGET_UV), None) == head
    # A file without the marker (written by hand): all of it is the owner's.
    assert updater.uv_config_text(_pyproject("1.33.1", TARGET_UV), OWN) == head + OWN
    # Windows line ends of an editor are read the same.
    crlf = stale.replace("\n", "\r\n")
    assert tomllib.loads(updater.uv_config_text(_pyproject("1.33.1", TARGET_UV), crlf))["native-tls"] is True


@pytest.mark.parametrize(
    ("own", "words"),
    [
        ('required-version = ">=0.1"\n', "required-version: TOW's own setting"),
        ("native-tls = \n", "Invalid value"),
        ("native-tls = true\nnative-tls = false\n", "Cannot overwrite"),
        ("[tool.uv]\nnative-tls = true\n", None),  # readable TOML; uv refuses the header itself
    ],
)
def test_unusable_owner_lines_are_refused(own, words):
    current = updater.UV_CONFIG_MARKER + "\n" + own
    if words is None:
        text = updater.uv_config_text(_pyproject("1.33.1", TARGET_UV), current)
        assert text.endswith(own)
        return
    with pytest.raises(ValueError, match=words.replace("(", r"\(")):
        updater.uv_config_text(_pyproject("1.33.1", TARGET_UV), current)


@pytest.mark.parametrize(
    "tool_uv",
    [
        'required-version = ">=0.12"\nrequired-version = ">=0.12"\n',  # a duplicate key
        "required-version = \n",  # malformed
        "cache-keys = [{ file = 'x' }]\n",  # a value of a kind not copied
        "[tool.uv.pip]\nnative-tls = true\n",  # a nested table
    ],
)
def test_project_settings_that_cannot_be_copied_whole_are_refused(tool_uv):
    with pytest.raises(ValueError, match=r"pyproject\.toml"):
        updater.uv_settings_text(_pyproject("1.33.1", tool_uv))


def test_the_target_settings_are_written_before_its_sync(install, github):
    (install["app"] / "pyproject.toml").write_text(_pyproject("1.22.0", PREVIOUS_UV), encoding="utf-8")
    _config(install).parent.mkdir(parents=True)
    _config(install).write_text(updater.UV_CONFIG_MARKER + "\n" + OWN, encoding="utf-8")
    github.release("v1.23.0", tarball("1.23.0", extra={"pyproject.toml": _pyproject("1.23.0", TARGET_UV).encode()}))
    machine = Recording(install["app"], github)

    code, lines = run(machine, "v1.23.0")

    assert code == 0, lines
    assert machine.seen == [{"required-version": ">=0.12", "python-downloads-json-url": MANIFEST, "native-tls": True}]
    assert _config(install).read_text(encoding="utf-8").endswith(OWN)


def test_a_rollback_puts_the_previous_settings_back_before_its_sync(install, github):
    (install["app"] / "pyproject.toml").write_text(_pyproject("1.22.0", PREVIOUS_UV), encoding="utf-8")
    github.release("v1.23.0", tarball("1.23.0", extra={"pyproject.toml": _pyproject("1.23.0", TARGET_UV).encode()}))
    machine = Recording(install["app"], github, silent_version="1.23.0")

    code, _lines = run(machine, "v1.23.0")

    assert code == 1
    assert [seen.get("required-version") for seen in machine.seen] == [">=0.12", ">=0.11"]
    assert "python-downloads-json-url" not in machine.seen[1]
    assert tomllib.loads(_config(install).read_text(encoding="utf-8")) == {"required-version": ">=0.11"}


def test_an_unusable_settings_file_is_refused_before_tow_stops(install, github):
    _config(install).parent.mkdir(parents=True)
    bad = updater.UV_CONFIG_MARKER + '\nrequired-version = ">=0.1"\n'
    _config(install).write_text(bad, encoding="utf-8")
    github.release("v1.23.0", tarball("1.23.0", extra={"pyproject.toml": _pyproject("1.23.0", TARGET_UV).encode()}))
    machine = Recording(install["app"], github)

    code, lines = run(machine, "v1.23.0")

    assert code == 2
    assert machine.calls == []  # TOW was never stopped
    assert _config(install).read_text(encoding="utf-8") == bad
    assert any("uv cannot use" in line and "TOW's own setting" in line for line in lines), lines
    assert not (install["root"] / "app.new").exists()


def test_a_target_whose_settings_cannot_be_copied_is_refused_before_tow_stops(install, github):
    broken = _pyproject("1.23.0", 'required-version = ">=0.12"\nrequired-version = ">=0.13"\n')
    github.release("v1.23.0", tarball("1.23.0", extra={"pyproject.toml": broken.encode()}))
    machine = Recording(install["app"], github)

    code, lines = run(machine, "v1.23.0")

    assert code == 2
    assert machine.calls == []
    assert any("the uv settings of v1.23.0 cannot be written" in line for line in lines), lines


def test_an_install_an_older_updater_brought_gets_its_file_where_no_uv_runs(install, github):
    # An updater of 1.33.0 never writes the file; the next run, up to date, writes it for this code.
    (install["app"] / "pyproject.toml").write_text(_pyproject("1.23.0", TARGET_UV), encoding="utf-8")
    github.latest("v1.23.0")
    machine = Recording(install["app"], github)

    code, lines = run(machine)

    assert code == 0, lines
    assert machine.calls == []
    assert tomllib.loads(_config(install).read_text(encoding="utf-8"))["python-downloads-json-url"] == MANIFEST


@pytest.mark.allow_system  # the real uv (offline, every folder in the temp folder), if there is one
def test_the_real_uv_reads_the_file_and_not_the_accounts(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("no uv on PATH")
    root = tmp_path / "TOW"
    app = root / "app"
    app.mkdir(parents=True)
    bogus = (tmp_path / "no-manifest.json").as_posix()
    (app / "pyproject.toml").write_text(
        _pyproject("1.33.1", f'required-version = ">=0.9"\npython-downloads-json-url = "{bogus}"\n'),
        encoding="utf-8",
    )
    (app / ".python-version").write_text("3.14.99\n", encoding="utf-8")
    assert updater.write_uv_config(root, app)
    profile = tmp_path / "profile"
    for name in ("APPDATA", "XDG_CONFIG_HOME", "PROGRAMDATA"):
        (profile / name / "uv").mkdir(parents=True)
        (profile / name / "uv" / "uv.toml").write_text('required-version = ">=999"\n', encoding="utf-8")
    base = {key: value for key, value in os.environ.items() if not key.upper().startswith(("UV_", "VIRTUAL_ENV"))}
    base.update({name: str(profile / name) for name in ("APPDATA", "XDG_CONFIG_HOME", "PROGRAMDATA")})
    base.update(UV_NO_CONFIG="1", UV_OFFLINE="1")
    _uv, env = updater.launcher_env(app, base)

    def uv_says(*args: str) -> str:
        done = subprocess.run(
            [uv, *args], cwd=app, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False
        )
        return done.stdout + done.stderr

    found = uv_says("python", "find")
    assert ">=999" not in found  # neither the account's nor the system's uv.toml
    assert "3.14.99" in found  # .python-version still counts
    assert "no-manifest.json" in uv_says("python", "list", "--only-downloads")  # the file's manifest
