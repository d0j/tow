"""History diagnostics must distinguish candidates from incomplete local scans."""

import codecs
import contextlib
import importlib.util
import io
import os
import random
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/history-scan.py"
pytestmark = pytest.mark.allow_git


@pytest.fixture
def repository(tmp_path, monkeypatch):
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    repo = tmp_path / "История с пробелами"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    for key, value in {
        "core.hooksPath": str(hooks),
        "user.name": "Owner",
        "user.email": "1234567+owner@users.noreply.github.com",
        "commit.gpgsign": "false",
        "tag.gpgsign": "false",
    }.items():
        git(repo, "config", key, value)
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "history-scan.py").write_bytes(SCRIPT.read_bytes())
    return repo


def git(repo, *args, input=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=input, capture_output=True, check=True).stdout


def commit(repo, path, data=b"ordinary contents"):
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)
    git(repo, "add", "--", path)
    git(repo, "commit", "-m", "synthetic history")


def scan(repo, monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["history-scan.py", "--repo", str(repo), *args])
    output = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            runpy.run_path(str(repo / "scripts/history-scan.py"), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
    return code, output.getvalue()


def test_cli_counts_blobs_not_trees_and_omits_author_address(repository, monkeypatch):
    commit(repository, "folder/ordinary.txt")
    code, output = scan(repository, monkeypatch)
    assert code == 0
    assert "blobs scanned: 1" in output
    assert "1234567+owner@users.noreply.github.com" not in output
    assert "Owner" in output
    assert "scan complete" in output


def test_a_real_author_or_committer_address_needs_review_and_is_never_printed(repository, monkeypatch):
    commit(repository, "ordinary.txt")
    code, output = scan(repository, monkeypatch)
    assert code == 0
    assert "e-mails not on GitHub's noreply domain: none" in output
    (repository / "second.txt").write_bytes(b"ordinary contents")
    git(repository, "add", "second.txt")
    git(repository, "commit", "-m", "synthetic", "--author", "Owner <owner@example.invalid>")
    (repository / "third.txt").write_bytes(b"ordinary contents")
    git(repository, "add", "third.txt")
    git(repository, "-c", "user.email=committer@example.invalid", "commit", "-m", "synthetic")
    code, output = scan(repository, monkeypatch)
    assert code == 1
    assert "e-mails not on GitHub's noreply domain: 2 in 2 commits" in output
    assert "example.invalid" not in output
    assert "review required" in output


def test_github_web_merges_and_noreply_addresses_are_fine(scanner):
    log = (
        b"1+a@users.noreply.github.com\x00noreply@github.com\n"
        b"B@Users.NoReply.GitHub.com\x00b@users.noreply.github.com\n"
    )
    assert scanner.exposed_identities(log) == (0, 0)
    assert scanner.exposed_identities(b"\x00noreply@github.com\n") == (1, 1)  # an empty address is no noreply one
    with pytest.raises(scanner.ScanError):
        scanner.exposed_identities(b"only-one-field\n")


def test_matches_are_candidates_not_clean_success_and_never_print_values(repository, monkeypatch):
    commit(repository, "ordinary.txt", b"password='fixture-sentinel-secret'")
    code, output = scan(repository, monkeypatch)
    assert code == 1
    assert "password value: 1" in output
    assert "fixture-sentinel-secret" not in output
    assert "review required" in output


def test_attribution_trailers_are_counted_in_blobs_and_messages_never_printed(repository, monkeypatch):
    commit(repository, "ordinary.txt")
    code, output = scan(repository, monkeypatch)
    assert code == 0
    assert "attribution trailers in commit messages: none" in output

    assistant = codecs.decode("pynhqr", "rot13")  # a model name, spelled so that this file does not carry it
    commit(repository, "notes.txt", f"Generated with fixture-tool, reviewed by {assistant.title()}\n".encode())
    (repository / "third.txt").write_bytes(b"ordinary contents")
    git(repository, "add", "third.txt")
    git(repository, "commit", "-m", "synthetic\n\nCo-Authored-By: Fixture Helper <1+fixture@users.noreply.github.com>")
    code, output = scan(repository, monkeypatch)
    assert code == 1
    assert "attribution trailer: 2 matches in 1 historical path hints" in output
    assert "attribution trailers in commit messages: 1 commits" in output
    for text in ("fixture-tool", assistant, assistant.title(), "Fixture Helper"):
        assert text not in output


def test_pull_request_refs_need_a_mirror_clone_says_the_help(scanner, capsys):
    with pytest.raises(SystemExit):
        scanner.main(["--help"])
    assert "git clone --mirror" in capsys.readouterr().out


def test_binary_blobs_are_not_silently_excluded(repository, monkeypatch):
    commit(repository, "ordinary.bin", b"\x00\xffpassword='fixture-binary-secret'\x00")
    code, output = scan(repository, monkeypatch)
    assert code == 1
    assert "password value: 1" in output
    assert "fixture-binary-secret" not in output


def test_an_oversized_blob_makes_the_scan_incomplete(repository, monkeypatch):
    commit(repository, "large.bin", b"x" * 5_000_001)
    code, output = scan(repository, monkeypatch)
    assert code == 2
    assert "blobs scanned: 0" in output
    assert "blobs skipped: 1" in output
    assert "scan incomplete" in output
    assert "scan complete" not in output


def test_explicit_budget_can_check_a_previously_skipped_blob(repository, monkeypatch):
    commit(repository, "large.bin", b"x" * 5_000_001 + b"\npassword='fixture-tail-secret'")
    code, output = scan(repository, monkeypatch, "--max-blob-bytes", "6000000")
    assert code == 1
    assert "blobs scanned: 1" in output
    assert "password value: 1" in output
    assert "fixture-tail-secret" not in output


def test_identical_blob_names_include_all_historical_aliases(repository, monkeypatch):
    commit(repository, "ordinary.txt", b"same contents")
    commit(repository, "data/secrets.enc", b"same contents")
    git(repository, "rm", "data/secrets.enc")
    git(repository, "commit", "-m", "remove synthetic path")
    code, output = scan(repository, monkeypatch)
    assert code == 1
    assert "blobs scanned: 1" in output
    assert "data/secrets.enc" in output
    assert "runtime/secret-looking paths" in output


def test_the_synthetic_stores_of_the_journal_fixtures_are_not_runtime_paths(repository, monkeypatch):
    commit(repository, "tests/journal_fixtures/case/home/.tow-site-transaction/secrets.bin")
    commit(repository, "tests/journal_fixtures/case/home/state.json", b"{}")
    code, output = scan(repository, monkeypatch)
    assert code == 0
    assert "runtime/secret-looking paths: none" in output
    # Only that folder: the same names anywhere else, a look-alike folder too, still need review.
    commit(repository, "fixtures/tests/journal_fixtures/state.json", b"[]")
    commit(repository, "tests/journal_fixtures_extra/secrets.enc", b"x")
    code, output = scan(repository, monkeypatch)
    assert code == 1
    assert "fixtures/tests/journal_fixtures/state.json" in output
    assert "tests/journal_fixtures_extra/secrets.enc" in output
    assert "home/state.json" not in output


def test_a_source_module_name_is_not_a_runtime_store(repository, monkeypatch):
    commit(repository, "src/tow/download_history.py")
    code, output = scan(repository, monkeypatch)
    assert code == 0
    assert "runtime/secret-looking paths: none" in output


def test_replacement_refs_cannot_hide_original_payload(repository, monkeypatch):
    commit(repository, "ordinary.txt", b"password='fixture-original-secret'")
    original = git(repository, "rev-parse", "HEAD:ordinary.txt").strip().decode()
    replacement = git(repository, "hash-object", "-w", "--stdin", input=b"ordinary contents").strip().decode()
    git(repository, "replace", original, replacement)
    before_refs = git(repository, "show-ref")
    code, output = scan(repository, monkeypatch)
    assert code == 1
    assert "password value: 1" in output
    assert "fixture-original-secret" not in output
    assert git(repository, "show-ref") == before_refs


def test_a_shallow_repository_is_not_a_complete_history(repository, tmp_path, monkeypatch):
    commit(repository, "ordinary.txt", b"first contents")
    commit(repository, "ordinary.txt", b"second contents")
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "--depth", "1", repository.as_uri(), str(shallow))
    (shallow / "scripts").mkdir()
    (shallow / "scripts/history-scan.py").write_bytes(SCRIPT.read_bytes())
    code, output = scan(shallow, monkeypatch)
    assert code == 2
    assert "scan incomplete" in output
    assert "shallow" in output


def test_empty_repository_is_a_complete_zero_blob_scan(repository, monkeypatch):
    code, output = scan(repository, monkeypatch)
    assert code == 0
    assert "blobs scanned: 0" in output
    assert "scan complete" in output


def test_detached_head_is_scanned_without_a_branch_reference(repository, monkeypatch):
    commit(repository, "ordinary.txt", b"password='fixture-detached-secret'")
    git(repository, "checkout", "--detach")
    git(repository, "branch", "-d", "main")
    code, output = scan(repository, monkeypatch)
    assert code == 1
    assert "blobs scanned: 1" in output
    assert "fixture-detached-secret" not in output


def test_bare_clone_can_be_audited_without_a_working_tree(repository, tmp_path, monkeypatch):
    commit(repository, "ordinary.txt", b"password='fixture-bare-secret'")
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "--bare", str(repository), str(bare))
    (bare / "scripts").mkdir()
    (bare / "scripts/history-scan.py").write_bytes(SCRIPT.read_bytes())
    code, output = scan(bare, monkeypatch)
    assert code == 1
    assert "blobs scanned: 1" in output
    assert "fixture-bare-secret" not in output


def test_real_sha256_repository_is_supported(repository, tmp_path, monkeypatch):
    sha256 = tmp_path / "sha256"
    sha256.mkdir()
    git(sha256, "init", "-b", "main", "--object-format=sha256")
    for key, value in {
        "core.hooksPath": str(tmp_path / "empty-hooks"),
        "user.name": "Owner",
        "user.email": "1234567+owner@users.noreply.github.com",
        "commit.gpgsign": "false",
    }.items():
        git(sha256, "config", key, value)
    (sha256 / "scripts").mkdir()
    (sha256 / "scripts/history-scan.py").write_bytes(SCRIPT.read_bytes())
    commit(sha256, "ordinary.txt", b"password='fixture-sha256-secret'")
    assert len(git(sha256, "rev-parse", "HEAD").strip()) == 64
    code, output = scan(sha256, monkeypatch)
    assert code == 1
    assert "blobs scanned: 1" in output
    assert "fixture-sha256-secret" not in output


@pytest.fixture
def scanner():
    spec = importlib.util.spec_from_file_location("history_scan_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_reader(scanner, data):
    reader = scanner.GitBatch.__new__(scanner.GitBatch)
    reader.process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(data))
    return reader


@pytest.mark.parametrize(
    "response",
    [
        b"",
        b"private-diagnostic\n",
        b"a" * 40 + b" missing\n",
        b"b" * 40 + b" blob 0\n",
        b"a" * 40 + b" blob -1\n",
        b"a" * 40 + b" blob 00\n",
        b"a" * 40 + b" unknown 1\n",
        b"a" * 40 + b" blob 1",
        b"a" * 40 + b" blob " + b"1" * 300 + b"\n",
    ],
)
def test_invalid_batch_metadata_is_refused_without_echoing_it(scanner, response):
    reader = fake_reader(scanner, response)
    with pytest.raises(scanner.ScanError) as error:
        reader.request(b"info", b"a" * 40)
    assert "private-diagnostic" not in str(error.value)


@pytest.mark.parametrize(("size", "data"), [(3, b"ab"), (3, b"abcX"), (0, b"X")])
def test_truncated_or_badly_terminated_content_is_refused(scanner, size, data):
    reader = fake_reader(scanner, b"a" * 40 + f" blob {size}\n".encode() + data)
    with pytest.raises(scanner.ScanError):
        reader.read(b"a" * 40, size)


@pytest.mark.parametrize("oid", [b"a" * 40, b"b" * 64])
def test_binary_content_supports_both_git_hash_formats(scanner, oid):
    reader = fake_reader(scanner, oid + b" blob 3\n\x00\xffx\n")
    assert reader.read(oid, 3) == b"\x00\xffx"


def test_content_metadata_must_equal_the_preflight(scanner):
    reader = fake_reader(scanner, b"a" * 40 + b" blob 4\ndata\n")
    with pytest.raises(scanner.ScanError, match="changed"):
        reader.read(b"a" * 40, 3)


def test_invalid_request_identity_never_reaches_git(scanner):
    reader = fake_reader(scanner, b"")
    with pytest.raises(scanner.ScanError):
        reader.request(b"info", b"a\ncontents b")
    assert reader.process.stdin.getvalue() == b""


@pytest.mark.parametrize(
    "raw",
    [
        b"malformed\0",
        b":" + b"0" * 6 + b" malformed\0private-path\0",
        b":100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" M\0",
    ],
)
def test_incomplete_path_protocol_is_not_an_empty_clean_history(scanner, monkeypatch, raw):
    monkeypatch.setattr(scanner, "git", lambda *_args: raw)
    with pytest.raises(scanner.ScanError):
        scanner.historical_paths(Path("."))


def test_nul_paths_keep_newlines_and_multiple_aliases(scanner, monkeypatch):
    raw = b":100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" M\0folder/new\nline.txt\0"
    monkeypatch.setattr(scanner, "git", lambda *_args: raw)
    assert scanner.historical_paths(Path(".")) == {
        b"a" * 40: {b"folder/new\nline.txt"},
        b"b" * 40: {b"folder/new\nline.txt"},
    }


def test_child_environment_cannot_redirect_the_selected_repository(scanner, monkeypatch):
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE", "GIT_TRACE"):
        monkeypatch.setenv(name, "fixture-route")
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    env = scanner.git_environment()
    assert "GIT_DIR" not in env
    assert "GIT_NAMESPACE" not in env
    assert "GIT_TRACE" not in env
    assert env["GIT_NO_REPLACE_OBJECTS"] == env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


@pytest.mark.parametrize(
    "failure",
    [
        OSError("fixture-private-value"),
        subprocess.TimeoutExpired("fixture-private-value", 1),
        MemoryError("fixture-private-value"),
    ],
)
def test_native_failure_is_incomplete_and_does_not_expose_details(scanner, monkeypatch, capsys, failure):
    def fail(*_args):
        raise failure

    monkeypatch.setattr(scanner, "git", fail)
    assert scanner.main([]) == 2
    output = capsys.readouterr()
    assert "scan incomplete" in output.err
    assert "fixture-private-value" not in output.err + output.out


def test_git_nonzero_status_never_supplies_partial_metadata(scanner, monkeypatch):
    monkeypatch.setattr(
        scanner.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=b"partial", stderr=b"fixture-private-value"),
    )
    with pytest.raises(scanner.ScanError, match="failed"):
        scanner.git(Path("."), "rev-list", "--all")


@pytest.mark.parametrize("options", [("--all", "--raw", "-z", "--format="), ("--all", "--format=%an%x00")])
def test_metadata_logs_disable_verifiers_external_author_maps_and_display_settings(scanner, monkeypatch, options):
    commands = []

    def execute(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(scanner.subprocess, "run", execute)
    assert scanner.git(Path("."), "log", *options) == b""
    assert len(commands) == 1
    for control in ("--no-show-signature", "--no-use-mailmap", "--no-notes", "--no-decorate", "--no-color"):
        assert control in commands[0]


def test_configured_signature_display_does_not_verify_synthetic_signed_history(repository, tmp_path, monkeypatch):
    commit(repository, "ordinary.txt")
    tree = git(repository, "rev-parse", "HEAD^{tree}").strip()
    parent = git(repository, "rev-parse", "HEAD").strip()
    signed = (
        b"tree "
        + tree
        + b"\nparent "
        + parent
        + b"\nauthor Owner <1234567+owner@users.noreply.github.com> 1 +0000"
        + b"\ncommitter Owner <1234567+owner@users.noreply.github.com> 1 +0000"
        + b"\ngpgsig -----BEGIN PGP SIGNATURE-----\n fixture\n -----END PGP SIGNATURE-----\n"
        + b"\nsynthetic signature\n"
    )
    oid = git(repository, "hash-object", "-t", "commit", "-w", "--stdin", input=signed).strip().decode()
    git(repository, "update-ref", "refs/heads/main", oid)
    # A broken scanner may only try this nonexistent fixture executable, never the user's verifier.
    git(repository, "config", "gpg.program", str(tmp_path / "unavailable-fixture-verifier"))
    git(repository, "config", "log.showSignature", "true")
    git(repository, "config", "log.mailmap", "true")
    git(repository, "config", "color.ui", "always")
    git(repository, "config", "log.decorate", "full")
    code, output = scan(repository, monkeypatch)
    assert code == 0
    assert "blobs scanned: 1" in output
    assert '"Owner" (2)' in output
    assert "1234567+owner@users.noreply.github.com" not in output


def test_pattern_timeout_never_becomes_no_matches(scanner):
    def expire(*_args, **_kwargs):
        raise TimeoutError("fixture-private-value")

    with pytest.raises(scanner.ScanError, match="budget"):
        scanner.match_count("password value", SimpleNamespace(finditer=expire), b"payload")


def test_safe_hints_escape_controls_and_redact_addresses(scanner):
    hint = scanner.safe_hint(b"folder/owner@invalid.test\n\xff.txt")
    assert "owner@invalid.test" not in hint
    assert "[redacted]" in hint
    assert "\\n" in hint
    assert "\\udcff" in hint


def test_timeout_matcher_preserves_existing_byte_pattern_spans(scanner):
    rng = random.Random(42715)
    samples = [
        b"password='fixture-secret' uid='abcdefghijklmnop'",
        b"\xffpass=abcd\x00",
        b"x@invalid.test",
        b"a@b.comXX@invalid.test",
        b"fake@example.test x@users.noreply.github.com",
        b"123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        b"chat_ids: -12345678",
        b"192.168.200.201",
        b"A" * 43 + b"=",
    ]
    alphabet = b"abcXYZ09@.:=-_/'\" \r\n\xff"
    samples += [bytes(rng.choice(alphabet) for _ in range(rng.randrange(200))) for _ in range(512)]
    for label, reference in scanner.PATTERNS.items():
        for data in samples:
            expected = [match.span() for match in reference.finditer(data)]
            actual = [match.span() for match in scanner.SEARCH_PATTERNS[label].finditer(data, timeout=2)]
            assert actual == expected, label


@pytest.mark.parametrize("budget", ["0", "-1", "not-an-integer"])
def test_invalid_blob_budget_is_refused_before_git(scanner, monkeypatch, budget):
    monkeypatch.setattr(scanner, "git", lambda *_args: pytest.fail("invalid budget called Git"))
    with pytest.raises(SystemExit) as exit_info:
        scanner.main(["--max-blob-bytes", budget])
    assert exit_info.value.code == 2


class OwnedProcess:
    def __init__(self, code=0, time_out=False):
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
        self.stderr = io.BytesIO(b"fixture-private-diagnostic" * 10000)
        self.code, self.time_out, self.waited, self.killed = code, time_out, 0, 0

    def wait(self, timeout=None):
        self.waited += 1
        if self.time_out and self.waited == 1:
            raise subprocess.TimeoutExpired("fixture", timeout)
        return self.code

    def kill(self):
        self.killed += 1


@pytest.mark.parametrize("failure", ["exit", "wait", "body", "start", "pipe"])
def test_reader_failure_reaps_only_its_owned_child_and_closes_pipes(scanner, monkeypatch, capsys, failure):
    child = OwnedProcess(code=int(failure == "exit"), time_out=failure == "wait")
    monkeypatch.setattr(scanner.subprocess, "Popen", lambda *_args, **_kwargs: child)
    if failure == "start":

        def refuse_start(_thread):
            raise RuntimeError("fixture-private-diagnostic")

        monkeypatch.setattr(scanner.threading.Thread, "start", refuse_start)
    if failure == "pipe":

        class BrokenClose(io.BytesIO):
            def close(self):
                super().close()
                raise BrokenPipeError("fixture-private-diagnostic")

        child.stdin = BrokenClose()

    def use_reader():
        with scanner.GitBatch(Path(".")):
            if failure == "body":
                raise scanner.ScanError("fixed message")

    with pytest.raises(scanner.ScanError):
        use_reader()
    assert child.waited >= 1
    assert child.stdin.closed
    assert child.stdout.closed
    assert child.stderr.closed
    if failure != "exit":
        assert child.killed == 1
    output = capsys.readouterr()
    assert "fixture-private-diagnostic" not in output.out + output.err


def test_successful_reader_closes_pipes_without_killing_child(scanner, monkeypatch):
    child = OwnedProcess()
    monkeypatch.setattr(scanner.subprocess, "Popen", lambda *_args, **_kwargs: child)
    with scanner.GitBatch(Path(".")):
        pass
    assert child.waited == 1
    assert child.killed == 0
    assert child.stdin.closed
    assert child.stdout.closed
    assert child.stderr.closed


def test_changed_reachable_history_cannot_publish_a_complete_result(scanner, monkeypatch, capsys):
    revisions = iter([b"a" * 40, b"b" * 40])

    def metadata(_repo, *args):
        if args[0] == "rev-parse":
            return b"false"
        if args[0] == "rev-list":
            return next(revisions)
        return b""

    class Batch:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def request(self, *_args):
            return b"blob", 0

        def read(self, *_args):
            return b""

    monkeypatch.setattr(scanner, "git", metadata)
    monkeypatch.setattr(scanner, "GitBatch", lambda _repo: Batch())
    assert scanner.main([]) == 2
    output = capsys.readouterr()
    assert "changed" in output.err
    assert "scan complete" not in output.out
