"""scripts/update.py as an install runs it: on the install's base Python (any 3.11+) with its
standard library alone, nothing of TOW and no pytest.

ci.yml runs this file with a real Python 3.11 in isolated mode (`python3.11 -I tests/updater_stdlib.py`);
the gate runs it too (tests/test_update_archive.py), so it cannot rot. It checks the import, the
unpack of a source archive, the launchers' environment for uv, the retried rename and the recovery
of a cut-off archive switch. Everything happens in a temporary folder; nothing runs a program.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

UPDATE = Path(__file__).resolve().parents[1] / "scripts" / "update.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("tow_updater_stdlib", UPDATE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


updater = _load()


def _archive(path: Path, members: dict[str, tuple[bytes, int]]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, (data, mode) in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return path


class Import(unittest.TestCase):
    def test_this_python_is_one_an_install_may_have(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))

    def test_the_updater_imports_only_the_standard_library(self):
        tree = ast.parse(UPDATE.read_text(encoding="utf-8"), feature_version=(3, 11))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        self.assertTrue(names)
        self.assertEqual(sorted(name for name in names if name not in sys.stdlib_module_names), [])
        self.assertNotIn("tow", sys.modules)


class Unpack(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="tow-updater-"))
        self.addCleanup(updater.remove_tree, self.folder)

    def unpack(self, members: dict[str, tuple[bytes, int]]) -> Path:
        archive = _archive(self.folder / "source.tar.gz", members)
        into = self.folder / "unpacked"
        into.mkdir(exist_ok=True)
        return updater.unpack(archive, into)

    def test_the_code_without_what_an_install_never_runs(self):
        top = self.unpack(
            {
                "tow-9.9.9/scripts/tow": (b"#!/bin/sh\n", 0o775),
                "tow-9.9.9/README.md": (b"TOW\n", 0o664),
                "tow-9.9.9/tests/journal_fixtures/deep/case.json": (b"{}", 0o644),
                "tow-9.9.9/.github/workflows/ci.yml": (b"name: ci\n", 0o644),
            }
        )
        self.assertEqual(top.name, "tow-9.9.9")
        self.assertEqual((top / "README.md").read_bytes(), b"TOW\n")
        self.assertFalse((top / "tests").exists())
        self.assertFalse((top / ".github").exists())
        if os.name != "nt":  # whatever the archive or the umask says
            self.assertEqual((top / "scripts" / "tow").stat().st_mode & 0o777, 0o755)
            self.assertEqual((top / "README.md").stat().st_mode & 0o777, 0o644)
            self.assertEqual((top / "scripts").stat().st_mode & 0o777, 0o755)

    def test_an_unsafe_entry_is_refused_even_where_nothing_is_unpacked(self):
        for name in ("tow-9.9.9/../evil.txt", "tow-9.9.9/tests/a:b.py", "tow-9.9.9/NUL.txt"):
            with self.subTest(name=name), self.assertRaises(updater.UpdateError):
                self.unpack({"tow-9.9.9/README.md": (b"x", 0o644), name: (b"x", 0o644)})
        self.assertFalse((self.folder / "evil.txt").exists())

    def test_two_names_one_folder_cannot_tell_apart_are_refused(self):
        with self.assertRaises(updater.UpdateError):
            self.unpack({"tow-9.9.9/Read.md": (b"a", 0o644), "tow-9.9.9/read.MD": (b"b", 0o644)})


class Environment(unittest.TestCase):
    def test_uv_gets_the_installs_places_and_none_of_the_accounts_python_settings(self):
        root = Path(tempfile.mkdtemp(prefix="tow-updater-"))
        self.addCleanup(updater.remove_tree, root)
        app = root / "app"
        base = {"UV_PYTHON": "3.12", "PYTHONHOME": "/x", "PYTHONPATH": "/y", "UV_OFFLINE": "1", "PATH": "/bin"}
        uv, env = updater.launcher_env(app, base)
        self.assertEqual(uv, "uv")  # no runtime/bin/uv in this install
        for name in ("UV_PYTHON", "PYTHONHOME", "PYTHONPATH"):
            self.assertNotIn(name, env)
        self.assertEqual(env["UV_OFFLINE"], "1")  # the owner's own choice stays
        self.assertEqual(env["TOW_ROOT"], str(root))
        self.assertEqual(env["UV_PYTHON_INSTALL_DIR"], str(root / "runtime" / "python"))
        self.assertEqual(env["UV_CACHE_DIR"], str(root / "runtime" / "cache"))
        self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(app / ".venv"))
        system = SimpleNamespace(root=root, env=env)
        temp = updater.System.uv_env(system)  # type: ignore[arg-type]
        self.assertEqual({temp[name] for name in ("TMP", "TEMP", "TMPDIR")}, {str(root / "runtime" / "uv-tmp")})
        self.assertTrue((root / "runtime" / "uv-tmp").is_dir())

    def test_uv_reads_the_installs_settings_written_again_with_the_owners_lines(self):
        # X39: uv reads <TOW>/runtime/uv.toml alone; the updater writes TOW's head from the code's
        # [tool.uv] (tomllib, Python 3.11) and keeps the owner's lines below the marker.
        root = Path(tempfile.mkdtemp(prefix="tow-updater-"))
        self.addCleanup(updater.remove_tree, root)
        app = root / "app"
        app.mkdir()
        _uv, env = updater.launcher_env(app, {"UV_NO_CONFIG": "1"})
        self.assertEqual(env["UV_CONFIG_FILE"], str(root / "runtime" / "uv.toml"))
        self.assertNotIn("UV_NO_CONFIG", env)
        manifest = "https://example.test/m.json"
        (app / "pyproject.toml").write_text(
            f'[project]\nversion = "9.9.9"\n[tool.uv]\nrequired-version = ">=0.12"\n'
            f'python-downloads-json-url = "{manifest}"\n',
            encoding="utf-8",
        )
        config = root / "runtime" / "uv.toml"
        config.parent.mkdir()
        config.write_text(f'required-version = ">=0.1"\n{updater.UV_CONFIG_MARKER}\nnative-tls = true\n', "utf-8")
        self.assertTrue(updater.write_uv_config(root, app))
        text = config.read_text(encoding="utf-8")
        self.assertNotIn("[tool", text)
        self.assertIn(f'python-downloads-json-url = "{manifest}"\n', text)
        self.assertIn('required-version = ">=0.12"\n', text)
        self.assertTrue(text.endswith(f"{updater.UV_CONFIG_MARKER}\nnative-tls = true\n"))
        self.assertFalse(updater.write_uv_config(root, app))  # the same again: nothing written
        config.write_text(f'{updater.UV_CONFIG_MARKER}\nrequired-version = ">=0.1"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            updater.write_uv_config(root, app)  # TOW's key among the owner's lines


class Owner(unittest.TestCase):
    def test_the_terminal_starts_a_copy_outside_app_and_ends_with_its_code(self):
        # X41: the update's own process is a copy of this file in runtime/update-run/<id>/, started
        # with the base Python (this one); the terminal shows its log and takes its exit code.
        root = Path(tempfile.mkdtemp(prefix="tow-updater-"))
        self.addCleanup(updater.remove_tree, root)
        app = root / "app"
        app.mkdir()
        seen: list[list[str]] = []

        def spawn_owner(argv: list[str]) -> SimpleNamespace:
            seen.append(argv)
            folder = Path(argv[3]).parent
            (folder / updater.OWNER_LOG).write_text("stopping TOW…\n", encoding="utf-8")
            (folder / updater.OWNER_RESULT).write_text('{"code": 1}', encoding="utf-8")
            return SimpleNamespace(exit_code=lambda: 1)

        saved = updater.OWNER_POLL
        updater.OWNER_POLL = 0.0
        self.addCleanup(setattr, updater, "OWNER_POLL", saved)
        system = SimpleNamespace(app=app, root=root, spawn_owner=spawn_owner)
        with open(os.devnull, "w", encoding="utf-8") as quiet:
            stdout, sys.stdout = sys.stdout, quiet
            try:
                code = updater.start_owner(["--ref", "v9.9.9"], system)
            finally:
                sys.stdout = stdout
        self.assertEqual(code, 1)
        [argv] = seen
        self.assertEqual(argv[1:3], ["-I", "-u"])
        self.assertEqual(Path(argv[3]).parent.parent, root / "runtime" / updater.RUNS)
        self.assertEqual(Path(argv[3]).read_bytes(), UPDATE.read_bytes())
        self.assertEqual(argv[4:], ["--owner", "--app", str(app), "--ref", "v9.9.9"])


class Retry(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="tow-updater-"))
        self.addCleanup(updater.remove_tree, self.folder)
        self.paused: list[float] = []
        real_replace, real_pause, waited = os.replace, updater._pause, updater._replace_waited[0]
        self.calls = 0

        def restore():
            os.replace = real_replace
            updater._pause = real_pause
            updater._replace_waited[0] = waited

        self.addCleanup(restore)
        updater._pause = self.paused.append
        updater._replace_waited[0] = 0.0
        self.real_replace = real_replace

    def held(self, times: int):
        def replace(source, target):
            self.calls += 1
            if self.calls <= times:
                raise PermissionError(13, "held by another program")
            self.real_replace(source, target)

        os.replace = replace

    def test_a_file_held_for_a_moment_is_moved_after_a_pause(self):
        (self.folder / "new").write_text("new", encoding="utf-8")
        self.held(2)
        updater.replace(self.folder / "new", self.folder / "app")
        self.assertEqual((self.folder / "app").read_text(encoding="utf-8"), "new")
        self.assertEqual(self.paused, [updater.REPLACE_PAUSE] * 2)

    def test_a_file_held_too_long_is_an_error_and_nothing_moves(self):
        (self.folder / "new").write_text("new", encoding="utf-8")
        (self.folder / "app").write_text("old", encoding="utf-8")
        self.held(10**6)
        with self.assertRaises(PermissionError):
            updater.replace(self.folder / "new", self.folder / "app")
        self.assertEqual(self.calls, updater.REPLACE_TRIES)
        self.assertEqual((self.folder / "app").read_text(encoding="utf-8"), "old")
        self.assertLessEqual(sum(self.paused), updater.REPLACE_BUDGET)


class Recovery(unittest.TestCase):
    def test_a_cut_off_switch_puts_the_previous_code_back_and_can_run_again(self):
        root = Path(tempfile.mkdtemp(prefix="tow-updater-"))
        self.addCleanup(updater.remove_tree, root)
        app, prev = root / "app", root / "app.prev"
        # Cut off half way: src/ of the new code is in app, scripts/ still waits in app.prev,
        # only.txt (new) arrived, old.txt (old) is already back in app.
        for folder, name, text in (
            (app, "src/code.py", "new"),
            (app, "only.txt", "new"),
            (app, "old.txt", "old"),
            (prev, "src/code.py", "old"),
            (prev, "scripts/tow", "old"),
        ):
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding="utf-8")
        journal = root / ".update-switch.json"
        record = {"format": "tow-update-switch/v1", "phase": "switching"}
        journal.write_text(
            json.dumps({**record, "old": ["src", "scripts", "old.txt"], "new": ["src", "scripts", "only.txt"]}),
            encoding="utf-8",
        )
        code = updater.ArchiveCode(SimpleNamespace(sys=None, app=app, root=root))  # type: ignore[arg-type]
        self.assertEqual(code.recover_pending(), "restored")
        self.assertEqual((app / "src" / "code.py").read_text(encoding="utf-8"), "old")
        self.assertEqual((app / "scripts" / "tow").read_text(encoding="utf-8"), "old")
        self.assertEqual((app / "old.txt").read_text(encoding="utf-8"), "old")
        self.assertFalse((app / "only.txt").exists())
        self.assertFalse(journal.exists())
        self.assertEqual(sorted(path.name for path in root.iterdir()), ["app"])
        self.assertIsNone(code.recover_pending())  # nothing left to do the next time

    def test_an_unreadable_record_is_an_error_and_nothing_moves(self):
        root = Path(tempfile.mkdtemp(prefix="tow-updater-"))
        self.addCleanup(updater.remove_tree, root)
        (root / "app").mkdir()
        (root / "app" / "a.txt").write_text("new", encoding="utf-8")
        (root / ".update-switch.json").write_text('{"format": "tow-update-switch/v1", "old": ["../x"]}', "utf-8")
        code = updater.ArchiveCode(SimpleNamespace(sys=None, app=root / "app", root=root))  # type: ignore[arg-type]
        with self.assertRaises(updater.UpdateError):
            code.recover_pending()
        self.assertEqual((root / "app" / "a.txt").read_text(encoding="utf-8"), "new")


if __name__ == "__main__":
    unittest.main(verbosity=2)
