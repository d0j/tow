"""Behaviour tests for the portable TOW bundle (tow.bundle).

Bundles here are sealed independently of the code under test (PBKDF2-HMAC-SHA256 +
Fernet over a zip with a sha256 manifest), so every test pins the on-disk format
contract and the error a user sees for a damaged, tampered or unsafe bundle.
"""

from __future__ import annotations

import base64
import codecs
import functools
import hashlib
import io
import json
import shutil
import warnings
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from tow import __version__
from tow import bundle as tow_bundle
from tow.bundle import (
    ExportImportError,
    export_bundle,
    import_bundle,
    parse_path_maps,
    prune_import_checkpoints,
    recover_import_transactions,
    rollback_import,
)
from tow.paths import config_path, data_dir, download_history_path, state_path
from tow.store import (
    SecretStoreError,
    encrypted_secrets_path,
    load_secret_undo,
    load_secrets,
    save_download_history,
    save_secret_undo,
    save_secrets,
    save_state,
    secret_undo_path,
    secrets_path,
)

PASS = "bundle-passphrase"
ITERATIONS = 100_000  # the lowest work factor an import accepts; keeps the suite fast
FIXED_SALT = b"fixed-test-salt!"
BASE_CONFIG = {"bind": "127.0.0.1", "port": 8787}
BASE_STATE: dict[str, Any] = {"topics": [], "mirrors": {}}
BASE_HISTORY: dict[str, Any] = {"schema_version": 1, "topics": {}}
BASE_SECRETS = {"qbittorrent": {"username": "fixture-user", "password": "bundle-secret"}}
NO_MANIFEST = object()


def _key(seed: bytes = b"test-only-tow-master-key-32bytes") -> str:
    return base64.urlsafe_b64encode(seed).decode()


@pytest.fixture(autouse=True)
def _destination(monkeypatch):
    # conftest already isolates TOW_HOME/TOW_CONFIG into tmp_path; the destination also needs a key.
    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)
    monkeypatch.setattr(tow_bundle, "KDF_ITERATIONS", ITERATIONS)


# --------------------------------------------------------------------------- bundle builders


@functools.lru_cache(maxsize=16)
def _fernet(passphrase: str, salt: bytes, iterations: int) -> Fernet:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8"))))


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _encode(name: str, value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if name == "config.yaml":
        return yaml.safe_dump(value, sort_keys=False).encode("utf-8")
    return _json(value)


def _members(**sections: Any) -> dict[str, bytes]:
    """A valid member set; keyword names use ``_`` for ``.`` (config_yaml, state_json, ...)."""
    values: dict[str, Any] = {
        "config.yaml": BASE_CONFIG,
        "state.json": BASE_STATE,
        "download_history.json": BASE_HISTORY,
        "secrets.json": BASE_SECRETS,
    }
    for key, value in sections.items():
        name = key.replace("_json", ".json").replace("_yaml", ".yaml")
        if value is None:
            values.pop(name, None)
        else:
            values[name] = value
    return {name: _encode(name, value) for name, value in values.items()}


def _manifest(contents: dict[str, bytes], /, **changes: Any) -> dict[str, Any]:
    manifest = {
        "format": "tow-export-v1",
        "schema_version": 1,
        "source_version": __version__,
        "members": sorted(contents),
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
    }
    manifest.update(changes)
    return manifest


def _zip(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate names are written on purpose
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries:
                archive.writestr(name, data)
    return buffer.getvalue()


def _seal(payload: bytes, passphrase: str = PASS, *, salt: bytes = FIXED_SALT) -> dict[str, Any]:
    return {
        "format": "tow-export-v1",
        "cipher": "fernet",
        "kdf": {
            "name": "pbkdf2-hmac-sha256",
            "iterations": ITERATIONS,
            "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        },
        "payload": _fernet(passphrase, salt, ITERATIONS).encrypt(payload).decode("ascii"),
    }


def _write_outer(path: Path, outer: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(outer), encoding="utf-8")
    return path


def _bundle(
    path: Path,
    members: dict[str, bytes] | None = None,
    *,
    manifest: Any = None,
    extra: list[tuple[str | zipfile.ZipInfo, bytes]] = (),
) -> Path:
    members = _members() if members is None else members
    entries: list[tuple[str | zipfile.ZipInfo, bytes]] = list(members.items())
    if manifest is None:
        entries.insert(0, ("manifest.json", _json(_manifest(members))))
    elif manifest is not NO_MANIFEST:
        entries.insert(0, ("manifest.json", _encode("manifest.json", manifest)))
    return _write_outer(path, _seal(_zip(entries + list(extra))))


def _open_bundle(path: Path, passphrase: str = PASS) -> dict[str, bytes]:
    outer = json.loads(path.read_text(encoding="utf-8"))
    salt = base64.urlsafe_b64decode(outer["kdf"]["salt"])
    payload = _fernet(passphrase, salt, outer["kdf"]["iterations"]).decrypt(outer["payload"].encode("ascii"))
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# --------------------------------------------------------------------------- destination helpers


def _targets() -> dict[str, bytes | None]:
    paths = {
        "config": config_path(),
        "state": state_path(),
        "history": download_history_path(),
        "secrets": encrypted_secrets_path(),
        "undo": secret_undo_path(),
    }
    return {name: (path.read_bytes() if path.is_file() else None) for name, path in paths.items()}


def _checkpoints_root() -> Path:
    return data_dir() / "import-checkpoints"


def _assert_untouched(before: dict[str, bytes | None]) -> None:
    assert _targets() == before
    assert not _checkpoints_root().exists()


def _seed_destination() -> dict[str, bytes | None]:
    config_path().write_text("bind: 127.0.0.1\nport: 9999\n", encoding="utf-8")
    save_state({"topics": [{"id": "old", "save_path": "C:/dest/media"}], "mirrors": {}})
    save_download_history({"schema_version": 1, "topics": {"old": {}}})
    save_secrets({"qbittorrent": {"password": "old-secret"}})
    return _targets()


def _transaction(checkpoint: Path) -> dict[str, Any]:
    return json.loads((checkpoint / "TRANSACTION.json").read_text(encoding="utf-8"))


def _rewrite_json(path: Path, mutate) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def applied(tmp_path):
    """A destination with prior content that then received a committed import."""
    before = _seed_destination()
    result = import_bundle(_bundle(tmp_path / "in" / "tow.towx"), PASS, apply=True)
    assert result["committed"] is True
    return {"checkpoint": Path(result["checkpoint"]), "before": before, "after": _targets()}


def _rejects(tmp_path, bundle: Path, match: str, **kwargs) -> None:
    before = _targets()
    with pytest.raises(ExportImportError, match=match):
        import_bundle(bundle, kwargs.pop("passphrase", PASS), apply=True, **kwargs)
    _assert_untouched(before)


CONFIG_ENCODINGS = [
    ("utf-8", b""),
    ("utf-8", codecs.BOM_UTF8),
    ("utf-16-le", b""),
    ("utf-16-le", codecs.BOM_UTF16_LE),
    ("utf-16-be", b""),
    ("utf-16-be", codecs.BOM_UTF16_BE),
    ("utf-32-le", b""),
    ("utf-32-le", codecs.BOM_UTF32_LE),
    ("utf-32-be", b""),
    ("utf-32-be", codecs.BOM_UTF32_BE),
]


@pytest.mark.parametrize(("codec", "bom"), CONFIG_ENCODINGS)
def test_import_commits_a_config_the_runtime_can_read(tmp_path, codec, bom):
    from tow.config import load_config

    before = _seed_destination()
    text = "# Keep комментарий\r\nbind: 127.0.0.1\r\nport: 8787\r\nextra: &a {x: '例😀\ufeff'}\r\ncopy: {<<: *a}\r\n"
    source = bom + text.encode(codec)
    archive = _bundle(tmp_path / "in" / "unicode.towx", _members(config_yaml=source))
    archive_bytes = archive.read_bytes()
    tow_bundle.verify_bundle(archive, PASS)
    preview = import_bundle(archive, PASS)
    assert preview["committed"] is False
    _assert_untouched(before)
    result = import_bundle(archive, PASS, apply=True)
    assert result["committed"] is True
    assert config_path().read_bytes() == (source if codec == "utf-8" else text.encode("utf-8"))
    assert load_config()["port"] == 8787
    assert load_config()["copy"] == {"x": "例😀\ufeff"}
    assert load_secrets() == BASE_SECRETS
    assert archive.read_bytes() == archive_bytes
    assert _open_bundle(archive)["config.yaml"] == source
    rollback_import(Path(result["checkpoint"]), apply=True)
    assert _targets() == before


@pytest.mark.parametrize(("codec", "bom"), CONFIG_ENCODINGS)
def test_export_normalizes_only_archive_config_not_source_files(tmp_path, codec, bom):
    _seed_destination()
    text = "# Keep this comment\r\nbind: 127.0.0.1\r\nport: 8787\r\n"
    source = bom + text.encode(codec)
    config_path().write_bytes(source)
    before = _targets()
    output = tmp_path / "out" / "unicode.towx"
    result = export_bundle(output, PASS)
    assert result["source_mutation"] is False
    assert _open_bundle(output)["config.yaml"] == (source if codec == "utf-8" else text.encode("utf-8"))
    tow_bundle.verify_bundle(output, PASS)
    _assert_untouched(before)


@pytest.mark.parametrize("action", ["verify", "preview", "apply"])
@pytest.mark.parametrize(
    "source",
    [
        b"\xff",
        b"\xef\xbb",
        b"\xff\xfea",
        b"\xfe\xff\x00",
        b"\xff\xfe\x00\x00a",
        b"\x00\x00\xfe\xffa",
        b"key: \xed\xa0\x80",
        b"\xff\xfe\x00\x00\x00\x00\x11\x00",
    ],
)
def test_malformed_config_encoding_is_refused_before_import_writes(tmp_path, source, action):
    before = _seed_destination()
    archive = _bundle(tmp_path / "in" / "malformed.towx", _members(config_yaml=source))
    operation = (
        functools.partial(tow_bundle.verify_bundle, archive, PASS)
        if action == "verify"
        else functools.partial(import_bundle, archive, PASS, apply=action == "apply")
    )
    with pytest.raises(ExportImportError, match=r"^invalid config\.yaml$"):
        operation()
    _assert_untouched(before)


@pytest.mark.parametrize("action", ["verify", "preview", "apply", "export"])
@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
def test_normalized_config_size_is_checked_before_any_write(tmp_path, monkeypatch, action, codec):
    from tow import yaml_guard

    _seed_destination()
    text = "extra: '" + "例" * 45 + "'\n"
    source = text.encode(codec)
    assert len(source) < 128 < len(text.encode("utf-8"))
    archive = _bundle(tmp_path / "in" / "oversized.towx", _members(config_yaml=source))
    if action == "export":
        config_path().write_bytes(source)
    before = _targets()
    monkeypatch.setattr(yaml_guard, "MAX_INPUT_BYTES", 128)
    output = tmp_path / "output" / "not-written.towx"
    operations = {
        "verify": functools.partial(tow_bundle.verify_bundle, archive, PASS),
        "export": functools.partial(export_bundle, output, PASS),
        "preview": functools.partial(import_bundle, archive, PASS),
        "apply": functools.partial(import_bundle, archive, PASS, apply=True),
    }
    with pytest.raises(ExportImportError, match=r"invalid config\.yaml:"):
        operations[action]()
    assert not output.parent.exists()
    _assert_untouched(before)


def test_checksum_is_checked_before_normalizing_archive_config(tmp_path, monkeypatch):
    before = _seed_destination()
    members = _members(config_yaml="bind: 127.0.0.1\nport: 8787\n".encode("utf-16"))
    manifest = _manifest(members)
    manifest["sha256"]["config.yaml"] = "0" * 64
    archive = _bundle(tmp_path / "in" / "tampered.towx", members, manifest=manifest)
    monkeypatch.setattr(tow_bundle, "normalize_utf8", lambda *a: pytest.fail("unauthenticated content decoded"))
    with pytest.raises(ExportImportError, match=r"bundle checksum mismatch: config\.yaml"):
        import_bundle(archive, PASS, apply=True)
    _assert_untouched(before)


@pytest.mark.parametrize(("codec", "bom"), CONFIG_ENCODINGS[2:])
def test_import_readback_failure_restores_original_bytes_after_normalization(tmp_path, monkeypatch, codec, bom):
    _seed_destination()
    # A recovery snapshot preserves even a pre-existing invalid config exactly.
    config_path().write_bytes(b"pre-existing-invalid-config: \xff")
    before = _targets()
    source = bom + "bind: 127.0.0.1\nport: 8787\n".encode(codec)
    archive = _bundle(tmp_path / "in" / "unicode.towx", _members(config_yaml=source))

    def fail_readback(*args):
        assert config_path().read_bytes() == b"bind: 127.0.0.1\nport: 8787\n"
        raise ExportImportError("synthetic read-back failure")

    monkeypatch.setattr(tow_bundle, "_destination_readback", fail_readback)
    with pytest.raises(ExportImportError, match="synthetic read-back failure"):
        import_bundle(archive, PASS, apply=True)
    assert _targets() == before


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize(
    "source",
    [b"extra: &loop [*loop]\n", b"extra: &loop {<<: *loop}\n", b"---\na: 1\n---\na: 2\n"],
)
def test_correctly_encrypted_and_hashed_unsafe_yaml_is_refused_without_writes(tmp_path, source, apply):
    before = _seed_destination()
    archive = _bundle(tmp_path / "in" / "unsafe.towx", _members(config_yaml=source))
    assert _open_bundle(archive)["config.yaml"] == source
    with pytest.raises(ExportImportError, match=r"invalid config\.yaml:"):
        import_bundle(archive, PASS, apply=apply)
    _assert_untouched(before)


def test_aliased_text_in_a_valid_archive_is_refused_before_apply(tmp_path, monkeypatch):
    from tow import yaml_guard

    monkeypatch.setattr(yaml_guard, "MAX_EXPANDED_TEXT", 64)
    before = _seed_destination()
    source = b"extra: [&a '" + b"x" * 32 + b"', *a, *a]\n"
    archive = _bundle(tmp_path / "in" / "unsafe.towx", _members(config_yaml=source))
    with pytest.raises(ExportImportError, match=r"invalid config\.yaml:"):
        import_bundle(archive, PASS, apply=True)
    _assert_untouched(before)


def test_valid_scalar_aliases_cannot_expand_past_read_limit_during_import_override(tmp_path, monkeypatch):
    from tow import yaml_guard

    before = _seed_destination()
    monkeypatch.setattr(yaml_guard, "MAX_INPUT_BYTES", 128)
    source = ("a: &a " + "я" * 20 + "\nb: [*a, *a, *a, *a]\n").encode()
    assert len(source) < 128
    archive = _bundle(tmp_path / "in" / "aliased.towx", _members(config_yaml=source))
    with pytest.raises(ExportImportError, match=r"invalid config\.yaml:"):
        import_bundle(archive, PASS, apply=True, config_overrides={"port": 8790})
    _assert_untouched(before)


# --------------------------------------------------------------------------- envelope


def test_round_trip_of_an_independently_sealed_bundle_previews_without_writing(tmp_path):
    before = _targets()

    result = import_bundle(_bundle(tmp_path / "in" / "tow.towx"), PASS)

    assert result["preview"] is True
    assert result["apply_required"] is True
    assert result["committed"] is False
    assert result["members"] == ["config.yaml", "download_history.json", "secrets.json", "state.json"]
    _assert_untouched(before)


def test_missing_bundle_file_is_reported_as_unreadable(tmp_path):
    _rejects(tmp_path, tmp_path / "nope.towx", "cannot read export bundle")


@pytest.mark.parametrize("raw", [b"not json at all", b"\xff\xfe\x00", b""])
def test_bundle_that_is_not_a_json_envelope_is_rejected(tmp_path, raw):
    bundle = tmp_path / "bad.towx"
    bundle.write_bytes(raw)
    _rejects(tmp_path, bundle, "invalid export bundle envelope")


def test_oversized_bundle_file_is_rejected_before_parsing(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path / "in" / "tow.towx")
    monkeypatch.setattr(tow_bundle, "MAX_BUNDLE_BYTES", bundle.stat().st_size - 1)
    _rejects(tmp_path, bundle, "export bundle is too large")


def _mutate(path: list[str], value: Any):
    def apply(outer: dict[str, Any]) -> Any:
        target = outer
        for key in path[:-1]:
            target = target[key]
        if value is _DELETE:
            del target[path[-1]]
        else:
            target[path[-1]] = value
        return outer

    return apply


_DELETE = object()


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda outer: [outer], "unsupported export bundle format"),
        (_mutate(["format"], "tow-export-v2"), "unsupported export bundle format"),
        (_mutate(["cipher"], "aes-ecb"), "unsupported export bundle cipher"),
        (_mutate(["kdf"], "pbkdf2"), "unsupported export bundle KDF"),
        (_mutate(["kdf", "name"], "scrypt"), "unsupported export bundle KDF"),
        (_mutate(["kdf", "iterations"], "many"), "malformed export bundle KDF"),
        (_mutate(["kdf", "salt"], "a"), "malformed export bundle KDF"),
        (_mutate(["kdf", "iterations"], 1_000), "invalid export bundle KDF parameters"),
        (_mutate(["kdf", "iterations"], 50_000_000), "invalid export bundle KDF parameters"),
        (_mutate(["kdf", "salt"], base64.urlsafe_b64encode(b"short").decode()), "invalid export bundle KDF parameters"),
        (_mutate(["payload"], _DELETE), "malformed export bundle payload"),
        (_mutate(["payload"], 123), "malformed export bundle payload"),
        (_mutate(["payload"], "not-a-fernet-token"), "wrong passphrase or corrupted bundle"),
        (_mutate(["kdf", "salt"], base64.urlsafe_b64encode(b"another-salt-val").decode()), "wrong passphrase"),
    ],
)
def test_tampered_envelope_is_rejected_without_touching_destination(tmp_path, mutate, match):
    bundle = _bundle(tmp_path / "in" / "tow.towx")
    outer = mutate(json.loads(bundle.read_text(encoding="utf-8")))
    _write_outer(bundle, outer)

    _rejects(tmp_path, bundle, match)


@pytest.mark.parametrize("passphrase", ["", None])
def test_import_requires_a_non_empty_passphrase(tmp_path, passphrase):
    _rejects(tmp_path, _bundle(tmp_path / "in" / "tow.towx"), "passphrase must not be empty", passphrase=passphrase)


# --------------------------------------------------------------------------- archive and manifest


def _symlink_member(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = (0o120777 & 0xFFFF) << 16
    return info


@pytest.mark.parametrize(
    ("build", "match"),
    [
        (lambda p: _write_outer(p, _seal(b"PK not really a zip")), "invalid export bundle archive"),
        (lambda p: _bundle(p, extra=[("state.json", _json(BASE_STATE))]), "duplicate bundle member"),
        (lambda p: _bundle(p, extra=[("notes.txt", b"x")]), "unsupported or unsafe bundle member: notes.txt"),
        # zipfile itself rewrites "\\" to "/" on Windows; either way the member is refused.
        (lambda p: _bundle(p, extra=[("..\\state.json", b"x")]), "strict relative path|unsupported or unsafe"),
        (lambda p: _bundle(p, extra=[("/events.json", b"x")]), "unsupported or unsafe bundle member"),
        (lambda p: _bundle(p, extra=[("sub/../state.json", b"x")]), "unsupported or unsafe bundle member"),
        (lambda p: _bundle(p, extra=[(_symlink_member("events.json"), b"/etc")]), "directories and links"),
        (lambda p: _bundle(p, manifest=NO_MANIFEST), "bundle manifest is missing"),
        (lambda p: _bundle(p, manifest=b"{not json"), "invalid bundle manifest"),
        (lambda p: _bundle(p, manifest=b"\xff"), "invalid bundle manifest"),
        (lambda p: _bundle(p, manifest=[1]), "unsupported bundle manifest"),
        (lambda p: _bundle(p, manifest=_manifest(_members(), format="other")), "unsupported bundle manifest"),
        (lambda p: _bundle(p, manifest=_manifest(_members(), schema_version=2)), "unsupported bundle manifest"),
        (lambda p: _bundle(p, manifest=_manifest(_members(), source_version="")), "source version is missing"),
        (lambda p: _bundle(p, manifest=_manifest(_members(), source_version=7)), "source version is missing"),
        (lambda p: _bundle(p, manifest=_manifest(_members(), source_version="999.0.0")), "version is incompatible"),
        (lambda p: _bundle(p, manifest=_manifest(_members(), members="all")), "malformed bundle manifest"),
        (lambda p: _bundle(p, manifest=_manifest(_members(), sha256=[])), "malformed bundle manifest"),
        (
            lambda p: _bundle(p, manifest=_manifest(_members(), members=[*sorted(_members()), "events.json"])),
            "member list mismatch",
        ),
        (
            lambda p: _bundle(p, manifest=_manifest(_members(), members=[*sorted(_members()), "state.json"])),
            "member list mismatch",
        ),
        (lambda p: _bundle(p, _members(secrets_json=None)), "bundle required member is missing"),
        (
            lambda p: _bundle(p, manifest=_manifest(_members(), sha256={"state.json": "0" * 64})),
            "bundle checksum list mismatch",
        ),
        (
            lambda p: _bundle(
                p,
                manifest=_manifest(
                    _members(),
                    sha256={**_manifest(_members())["sha256"], "state.json": hashlib.sha256(b"").hexdigest()},
                ),
            ),
            "bundle checksum mismatch: state.json",
        ),
    ],
)
def test_unsafe_or_inconsistent_archive_is_rejected(tmp_path, build, match):
    bundle = build(tmp_path / "in" / "tow.towx")
    _rejects(tmp_path, bundle, match)


def test_member_with_corrupted_bytes_fails_the_archive_crc(tmp_path):
    marker = b"bind: 127.0.0.1\nport: 8787\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("config.yaml", marker)
    payload = buffer.getvalue().replace(marker, marker.replace(b"8787", b"8788"))
    bundle = _write_outer(tmp_path / "in" / "tow.towx", _seal(payload))

    _rejects(tmp_path, bundle, "cannot read export bundle archive")


def test_member_over_the_per_member_limit_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(tow_bundle, "MAX_MEMBER_BYTES", 64)
    bundle = _bundle(tmp_path / "in" / "tow.towx", _members(config_yaml=b"# " + b"x" * 200 + b"\nport: 1\n"))
    _rejects(tmp_path, bundle, "bundle member is too large")


def test_members_whose_total_size_exceeds_the_bundle_limit_are_rejected(tmp_path, monkeypatch):
    # Highly compressible members: the sealed file stays small, the unpacked total does not.
    monkeypatch.setattr(tow_bundle, "MAX_BUNDLE_BYTES", 6000)
    monkeypatch.setattr(tow_bundle, "MAX_MEMBER_BYTES", 5000)
    padding = b"#" + b" " * 3500 + b"\n"
    bundle = _bundle(
        tmp_path / "in" / "tow.towx",
        _members(config_yaml=padding + b"port: 1\n", state_json=_json(BASE_STATE) + b" " * 3500),
    )
    assert bundle.stat().st_size < 6000
    _rejects(tmp_path, bundle, "bundle contents are too large")


# --------------------------------------------------------------------------- member schemas


def _deep(levels: int) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for _ in range(levels):
        value = {"n": value}
    return value


@pytest.mark.parametrize(
    ("member", "patch", "match"),
    [
        ("config.yaml", {"port": "8787x"}, r"invalid config\.yaml: port must be a whole number"),
        ("config.yaml", {"port": True}, r"invalid config\.yaml: port must be a whole number"),
        ("config.yaml", {"port": 0}, r"invalid config\.yaml: port must be a whole number"),
        ("config.yaml", {"interval_sec": -1.5}, r"interval_sec must be a whole number"),
        ("config.yaml", {"bind": 5}, r"bind must be a text address"),
        ("config.yaml", {"allowed_save_roots": "C:/media"}, r"allowed_save_roots must be a list of folders"),
        ("config.yaml", {"allowed_save_roots": [1]}, r"allowed_save_roots must be a list of folders"),
        ("config.yaml", {"allow_unc_save_paths": "maybe"}, r"allow_unc_save_paths must be true or false"),
        ("config.yaml", {"restore_points_dir": ["x"]}, r"restore_points_dir must be a folder path"),
        ("config.yaml", {"trackers": ["rutracker"]}, r"trackers must map each site name to its settings"),
        ("config.yaml", {"trackers": {"t": "x"}}, r"trackers must map each site name to its settings"),
        ("config.yaml", {"trackers": {"t": {"title": 5}}}, r"trackers\.t\.title must be text"),
        ("config.yaml", {"trackers": {"t": {"login_hosts": "h"}}}, r"trackers\.t\.login_hosts must be a list of http"),
        ("config.yaml", {"trackers": {"t": {"login_form": "x"}}}, r"trackers\.t\.login_form may have only"),
        (
            "config.yaml",
            {"trackers": {"t": {"login_form": {"extra": {"a": 1}}}}},
            r"trackers\.t\.login_form may have only",
        ),
        (
            "config.yaml",
            {"trackers": {"t": {"fail_threshold": 1.5}}},
            r"trackers\.t\.fail_threshold must be a whole number",
        ),
        ("config.yaml", {"nest": _deep(40)}, r"invalid config\.yaml: .*32"),
        ("config.yaml", {"telegram": {"token": "plain"}}, r"outside encrypted payload: config\.yaml:telegram\.token"),
        ("state.json", {"topics": {}}, r"state\.json has an unsupported schema"),
        ("state.json", {"mirrors": []}, r"state\.json has an unsupported schema"),
        ("state.json", {"topics": [1]}, r"state\.json topic 0 has an invalid type"),
        ("state.json", {"topics": [{"id": 1}]}, r"state\.json\.topic\[0\]\.id has an invalid type"),
        ("state.json", {"topics": [{"paused": "no"}]}, r"topic\[0\]\.paused has an invalid type"),
        ("state.json", {"topics": [{"selected_file_count": True}]}, r"selected_file_count has an invalid"),
        ("state.json", {"topics": [{"previous_hashes": "abc"}]}, r"previous_hashes has an invalid type"),
        ("state.json", {"topics": [{"selection": "all"}]}, r"topic\[0\]\.selection has an invalid type"),
        ("state.json", {"topics": [{"selection": {"mode": 1}}]}, r"selection\.mode has an invalid type"),
        ("state.json", {"mirrors": {"m": "x"}}, r"mirror entry has an invalid type"),
        ("state.json", {"health": []}, r"state\.json\.health has an invalid type"),
        ("state.json", {"undo": []}, r"state\.json\.undo has an invalid type"),
        ("state.json", {"undo": {"secrets": {"telegram": "x"}}}, r"plaintext secret undo is not allowed"),
        ("state.json", {"topics": [{"id": "a", "cookie": "c"}]}, r"state\.json:topics\[0\]\.cookie"),
        ("download_history.json", {"schema_version": "1"}, r"schema_version has an invalid type"),
        ("download_history.json", {"schema_version": True}, r"schema_version has an invalid type"),
        ("download_history.json", {"topics": []}, r"download_history\.json\.topics has an invalid type"),
        ("download_history.json", {"topics": {"a": []}}, r"topic entry has an invalid type"),
        ("download_history.json", {"topics": {"a": {"items": []}}}, r"download_history\.json\.items has an"),
        ("download_history.json", {"topics": {"a": {"api_key": "k"}}}, r"download_history\.json:topics\.a\.api_key"),
    ],
)
def test_schema_violations_are_rejected_with_the_offending_field(tmp_path, member, patch, match):
    base = {"config.yaml": BASE_CONFIG, "state.json": BASE_STATE, "download_history.json": BASE_HISTORY}[member]
    key = member.replace(".", "_")
    bundle = _bundle(tmp_path / "in" / "tow.towx", _members(**{key: {**base, **patch}}))
    _rejects(tmp_path, bundle, match)


@pytest.mark.parametrize(
    ("member", "raw", "match"),
    [
        ("config.yaml", b"bind: [unclosed\n", r"invalid config\.yaml"),
        ("config.yaml", b"- a\n- b\n", r"config\.yaml must be an object"),
        ("config.yaml", b"1: a\n", r"config\.yaml contains a non-string key"),
        ("config.yaml", b"when: 2020-01-01\n", r"config\.yaml contains an unsupported value type"),
        ("state.json", b"\xff\xfe", r"invalid state\.json"),
        ("download_history.json", b"{", r"invalid download_history\.json"),
        ("secrets.json", b"[]", r"secrets\.json must be an object"),
    ],
)
def test_unparseable_members_are_rejected(tmp_path, member, raw, match):
    bundle = _bundle(tmp_path / "in" / "tow.towx", _members(**{member.replace(".", "_"): raw}))
    _rejects(tmp_path, bundle, match)


_SETTINGS_UNDO = {"kind": "settings", "secrets_undo_ref": "settings-v1"}


@pytest.mark.parametrize(
    ("sections", "match"),
    [
        ({"state_json": {**BASE_STATE, "undo": _SETTINGS_UNDO}}, "settings undo reference has no encrypted snapshot"),
        (
            {
                "state_json": {**BASE_STATE, "undo": {**_SETTINGS_UNDO, "secrets_undo_ref": "v0"}},
                "secrets_undo_json": {"secrets": {}},
            },
            "settings undo reference has no encrypted snapshot",
        ),
        (
            {"state_json": {**BASE_STATE, "undo": _SETTINGS_UNDO}, "secrets_undo_json": {"secrets": "x"}},
            "malformed encrypted settings undo payload",
        ),
        ({"secrets_undo_json": {"secrets": {}}}, "orphan encrypted settings undo member"),
        ({"events_json": {"events": "x"}}, "events member is not redacted"),
        ({"events_json": {"events": [{"kind": "x", "detail": "leak"}]}}, "events member is not redacted"),
        ({"events_json": {"events": [{"kind": {"nested": 1}}]}}, "events member is not redacted"),
        ({"events_json": {"events": ["x"]}}, "events member is not redacted"),
        ({"events_json": {"events": [], "raw_log": "leak"}}, "events member is not redacted"),
    ],
)
def test_undo_snapshot_and_event_log_members_must_be_consistent(tmp_path, sections, match):
    _rejects(tmp_path, _bundle(tmp_path / "in" / "tow.towx", _members(**sections)), match)


def test_redacted_event_log_member_is_accepted(tmp_path):
    members = _members(events_json={"events": [{"kind": "check", "ts": "2026-01-01T00:00:00"}]})

    result = import_bundle(_bundle(tmp_path / "in" / "tow.towx", members), PASS)

    assert "events.json" in result["members"]


# --------------------------------------------------------------------------- export


def _seed_source(config: str = "bind: 127.0.0.1\nport: 8787\n") -> None:
    config_path().write_text(config, encoding="utf-8")
    save_state({"topics": [{"id": "t1", "save_path": "C:/media/t1"}], "mirrors": {}})
    save_download_history({"schema_version": 1, "topics": {"t1": {}}})
    save_secrets(BASE_SECRETS)


def test_export_writes_a_bundle_that_opens_with_the_documented_format(tmp_path):
    _seed_source()
    output = tmp_path / "out" / "tow.towx"
    output.parent.mkdir()

    result = export_bundle(output, PASS)

    members = _open_bundle(output)
    manifest = json.loads(members.pop("manifest.json"))
    assert result["bytes"] == output.stat().st_size
    assert set(members) == set(result["members"]) == set(manifest["members"])
    assert manifest["sha256"] == {name: hashlib.sha256(data).hexdigest() for name, data in members.items()}
    assert "source_master_key" in manifest["excluded"]
    assert json.loads(members["secrets.json"]) == BASE_SECRETS
    assert b"bundle-secret" not in output.read_bytes()


def test_export_without_history_file_exports_an_empty_history(tmp_path):
    _seed_source()
    download_history_path().unlink()
    output = tmp_path / "tow.towx"

    export_bundle(output, PASS)

    assert json.loads(_open_bundle(output)["download_history.json"]) == {"schema_version": 1, "topics": {}}


def test_export_refuses_to_replace_an_existing_file_unless_forced(tmp_path):
    _seed_source()
    output = tmp_path / "tow.towx"
    output.write_bytes(b"precious")

    with pytest.raises(ExportImportError, match="already exists"):
        export_bundle(output, PASS)
    assert output.read_bytes() == b"precious"

    export_bundle(output, PASS, overwrite=True)
    assert "secrets.json" in _open_bundle(output)


@pytest.mark.parametrize("previous", [None, b"previous bundle bytes"])
def test_export_read_back_failure_leaves_the_output_as_it_was(tmp_path, monkeypatch, previous):
    _seed_source()
    output = tmp_path / "tow.towx"
    if previous is not None:
        output.write_bytes(previous)
    monkeypatch.setattr(tow_bundle, "_read_bundle", lambda _path, _passphrase: {"members": {}})

    with pytest.raises(ExportImportError, match="read-back member mismatch"):
        export_bundle(output, PASS, overwrite=True)

    if previous is None:
        assert not output.exists()
    else:
        assert output.read_bytes() == previous


def test_export_onto_a_directory_fails_and_leaves_it_intact(tmp_path, monkeypatch):
    import tow.store

    # On Windows replacing a directory is a PermissionError, which the atomic write
    # retries with backoff (~2.5 s); the retry itself is not under test here.
    monkeypatch.setattr(tow.store.time, "sleep", lambda _seconds: None)
    _seed_source()
    output = tmp_path / "tow.towx"
    output.mkdir()
    (output / "keep.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ExportImportError, match=r"cannot write .*tow\.towx"):
        export_bundle(output, PASS, overwrite=True)

    assert output.is_dir()
    assert [path.name for path in output.iterdir()] == ["keep.txt"]


def test_export_with_empty_passphrase_writes_nothing(tmp_path):
    _seed_source()
    output = tmp_path / "tow.towx"
    with pytest.raises(ExportImportError, match="passphrase must not be empty"):
        export_bundle(output, "")
    assert not output.exists()


def test_export_refuses_plaintext_secret_in_config(tmp_path):
    _seed_source("bind: 127.0.0.1\ntelegram:\n  token: plain-token\n")
    output = tmp_path / "tow.towx"
    with pytest.raises(ExportImportError, match=r"config\.yaml:telegram\.token"):
        export_bundle(output, PASS)
    assert not output.exists()


@pytest.mark.parametrize(
    ("raw", "match"),
    [(b"{broken", r"invalid download_history\.json"), (b"[1, 2]", r"download_history\.json must be an object")],
)
def test_export_refuses_a_corrupt_history_file(tmp_path, raw, match):
    _seed_source()
    download_history_path().write_bytes(raw)
    output = tmp_path / "tow.towx"
    with pytest.raises(ExportImportError, match=match):
        export_bundle(output, PASS)
    assert not output.exists()


def test_export_refuses_missing_or_oversized_config(tmp_path, monkeypatch):
    _seed_source()
    output = tmp_path / "tow.towx"
    with monkeypatch.context() as patch:
        patch.setattr(tow_bundle, "MAX_BUNDLE_BYTES", 4)
        with pytest.raises(ExportImportError, match=r"config\.yaml is too large"):
            export_bundle(output, PASS)
    config_path().unlink()
    with pytest.raises(ExportImportError, match=r"cannot read config\.yaml"):
        export_bundle(output, PASS)
    assert not output.exists()


def test_read_limited_rechecks_size_during_read(tmp_path, monkeypatch):
    path = tmp_path / "growing.towx"
    path.write_bytes(b"12345")
    original_stat = Path.stat

    def stale_stat(self, *args, **kwargs):
        result = original_stat(self, *args, **kwargs)
        if self == path:
            return type("StaleStat", (), {"st_size": 4})()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(tow_bundle, "MAX_BUNDLE_BYTES", 4)
        patch.setattr(Path, "stat", stale_stat)
        with pytest.raises(ExportImportError, match=r"growing\.towx is too large"):
            tow_bundle._read_limited(path, label=path.name)


def test_export_settings_undo_must_point_at_a_readable_encrypted_snapshot(tmp_path):
    _seed_source()
    output = tmp_path / "tow.towx"
    save_state({**BASE_STATE, "undo": {"kind": "settings", "secrets_undo_ref": "legacy"}})
    with pytest.raises(ExportImportError, match="settings undo reference is invalid"):
        export_bundle(output, PASS)

    save_state({**BASE_STATE, "undo": _SETTINGS_UNDO})  # no secrets-undo.enc exists
    with pytest.raises(ExportImportError, match="cannot read encrypted settings undo snapshot"):
        export_bundle(output, PASS)
    assert not output.exists()


def test_export_wraps_unexpected_failures(tmp_path, monkeypatch):
    _seed_source()

    def broken_state():
        raise ValueError("unexpected")

    monkeypatch.setattr(tow_bundle, "load_state", broken_state)
    with pytest.raises(ExportImportError, match="cannot create export bundle safely"):
        export_bundle(tmp_path / "tow.towx", PASS)


# --------------------------------------------------------------------------- path maps


@pytest.mark.parametrize(
    ("values", "match"),
    [
        (["C:/old"], "must use OLD=NEW"),
        ([42], "must use OLD=NEW"),
        (["=D:/new"], "non-empty OLD and NEW"),
        (["C:/old=  "], "non-empty OLD and NEW"),
        (["C:/old=C:/old"], "OLD and NEW must differ"),
        (["C:/old=D:/a", " C:/old =E:/b"], "duplicate path map source"),
    ],
)
def test_invalid_path_maps_are_rejected(values, match):
    with pytest.raises(ExportImportError, match=match):
        parse_path_maps(values)


def test_path_maps_are_trimmed_and_keep_equals_in_new_path():
    assert parse_path_maps([" C:/old = D:/new=1 "]) == [("C:/old", "D:/new=1")]
    assert parse_path_maps(None) == []


def test_invalid_path_map_aborts_import_before_any_write(tmp_path):
    _rejects(tmp_path, _bundle(tmp_path / "in" / "tow.towx"), "must use OLD=NEW", path_maps=["nope"])


def test_path_maps_rewrite_state_paths_and_warn_about_unmapped_absolute_paths(tmp_path):
    state = {
        "topics": [
            {"id": "a", "save_path": "C:/old\\Show"},
            {"id": "b", "save_path": "C:/old/Film"},
            {"id": "c", "save_path": "C:/older/x"},
            {"id": "d", "save_path": "\\\\nas\\share\\x"},
            {"id": "e", "save_path": "relative/dir"},
            {"id": "f"},
        ],
        "mirrors": {},
        "save_roots": ["C:/old", "/mnt/media", "relative"],
    }
    bundle = _bundle(tmp_path / "in" / "tow.towx", _members(state_json=state))

    preview = import_bundle(bundle, PASS, path_maps=["C:/old/=E:/new/"])

    assert preview["path_maps"] == [{"old": "C:/old/", "new": "E:/new/"}]
    assert preview["path_warnings"] == [
        "unmapped save root: /mnt/media",
        "unmapped topic path: C:/older/x",
        "unmapped topic path: \\\\nas\\share\\x",
    ]
    assert not state_path().exists()

    import_bundle(bundle, PASS, apply=True, path_maps=["C:/old/=E:/new/"])

    written = json.loads(state_path().read_text(encoding="utf-8"))
    assert [topic.get("save_path") for topic in written["topics"]] == [
        "E:/new\\Show",
        "E:/new/Film",
        "C:/older/x",
        "\\\\nas\\share\\x",
        "relative/dir",
        None,
    ]
    assert written["save_roots"][1:] == ["/mnt/media", "relative"]
    assert written["save_roots"][0].rstrip("/") == "E:/new"


# --------------------------------------------------------------------------- overrides and preserved secrets


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"user_agent": "x"}, "unsupported config override"),
        ({"port": "80"}, r"config\.yaml\.port has an invalid type"),
        ({"port": -1}, r"invalid config\.yaml: port must be a whole number"),
        ({"allow_lan": "yes"}, r"allow_lan has an invalid type"),
    ],
)
def test_invalid_config_overrides_abort_before_any_write(tmp_path, overrides, match):
    _rejects(tmp_path, _bundle(tmp_path / "in" / "tow.towx"), match, config_overrides=overrides)


def test_config_overrides_replace_access_settings_of_the_imported_config(tmp_path):
    bundle = _bundle(tmp_path / "in" / "tow.towx", _members(config_yaml={**BASE_CONFIG, "interval_sec": 600}))

    result = import_bundle(bundle, PASS, apply=True, config_overrides={"port": 9100, "allow_lan": False})

    assert result["config_overrides"] == ["allow_lan", "port"]
    written = yaml.safe_load(config_path().read_text(encoding="utf-8"))
    assert written == {"bind": "127.0.0.1", "port": 9100, "interval_sec": 600, "allow_lan": False}


def test_only_lan_auth_can_be_preserved_from_destination(tmp_path):
    _rejects(
        tmp_path,
        _bundle(tmp_path / "in" / "tow.towx"),
        "unsupported preserved secret key",
        preserve_secret_keys=["qbittorrent"],
    )


@pytest.mark.parametrize(
    ("destination", "expected_lan_auth"),
    [({"lan_auth": "destination-hash"}, "destination-hash"), ({}, None)],
)
def test_preserved_lan_auth_comes_from_destination_not_bundle(tmp_path, destination, expected_lan_auth):
    save_secrets(destination)
    bundle = _bundle(tmp_path / "in" / "tow.towx", _members(secrets_json={**BASE_SECRETS, "lan_auth": "bundle-hash"}))

    result = import_bundle(bundle, PASS, apply=True, preserve_secret_keys=["lan_auth"])

    assert result["preserved_secret_keys"] == ["lan_auth"]
    secrets = load_secrets()
    assert secrets.get("lan_auth") == expected_lan_auth
    assert secrets["qbittorrent"] == BASE_SECRETS["qbittorrent"]


def test_destination_with_legacy_plaintext_secrets_blocks_import(tmp_path):
    secrets_path().parent.mkdir(parents=True, exist_ok=True)
    secrets_path().write_text('{"qbittorrent": {}}', encoding="utf-8")
    bundle = _bundle(tmp_path / "in" / "tow.towx")

    _rejects(tmp_path, bundle, "cannot read destination secrets to preserve access", preserve_secret_keys=["lan_auth"])
    _rejects(tmp_path, bundle, "legacy plaintext secrets require explicit migration")


# --------------------------------------------------------------------------- apply, read-back and automatic rollback


def test_import_without_undo_snapshot_drops_stale_destination_snapshot_and_rollback_restores_it(tmp_path):
    _seed_destination()
    save_secret_undo({"telegram": {"token": "destination-undo"}})
    before = _targets()
    assert before["undo"] is not None

    result = import_bundle(_bundle(tmp_path / "in" / "tow.towx"), PASS, apply=True)

    assert not secret_undo_path().exists()
    rollback_import(Path(result["checkpoint"]), apply=True)
    assert _targets() == before
    assert load_secret_undo("settings-v1") == {"telegram": {"token": "destination-undo"}}


def test_import_with_undo_snapshot_is_reencrypted_and_rollback_removes_it_again(tmp_path):
    before = _seed_destination()
    members = _members(
        state_json={**BASE_STATE, "undo": _SETTINGS_UNDO},
        secrets_undo_json={"secrets": {"telegram": {"token": "bundle-undo"}}},
    )

    result = import_bundle(_bundle(tmp_path / "in" / "tow.towx", members), PASS, apply=True)

    assert load_secret_undo("settings-v1") == {"telegram": {"token": "bundle-undo"}}
    rollback = rollback_import(Path(result["checkpoint"]), apply=True)
    assert rollback["restored"] == [
        "config.yaml",
        "state.json",
        "download_history.json",
        "secrets.enc",
        "secrets-undo.enc",
    ]
    assert _targets() == before
    assert not secret_undo_path().exists()


@pytest.mark.parametrize(
    ("name", "replacement", "match"),
    [
        ("load_secrets", lambda: {"tampered": True}, "secrets semantic read-back mismatch"),
        ("load_secret_undo", lambda _ref: {"tampered": True}, "settings undo semantic read-back mismatch"),
    ],
)
def test_destination_read_back_mismatch_restores_previous_bytes(tmp_path, monkeypatch, name, replacement, match):
    before = _seed_destination()
    members = _members(
        state_json={**BASE_STATE, "undo": _SETTINGS_UNDO},
        secrets_undo_json={"secrets": {"telegram": {"token": "bundle-undo"}}},
    )
    bundle = _bundle(tmp_path / "in" / "tow.towx", members)
    monkeypatch.setattr(tow_bundle, name, replacement)

    with pytest.raises(ExportImportError, match=match):
        import_bundle(bundle, PASS, apply=True)

    assert _targets() == before
    (checkpoint,) = _checkpoints_root().iterdir()
    assert _transaction(checkpoint)["status"] == "rolled_back"


def test_unreadable_destination_secrets_after_write_restore_previous_bytes(tmp_path, monkeypatch):
    before = _seed_destination()

    def unreadable():
        raise SecretStoreError("fixture: cannot decrypt")

    monkeypatch.setattr(tow_bundle, "load_secrets", unreadable)
    with pytest.raises(ExportImportError, match="encrypted destination secret read-back failed"):
        import_bundle(_bundle(tmp_path / "in" / "tow.towx"), PASS, apply=True)
    assert _targets() == before


def test_failed_import_with_failed_rollback_stays_pending_and_is_recovered_later(tmp_path, monkeypatch):
    before = _seed_destination()
    bundle = _bundle(tmp_path / "in" / "tow.towx")

    def fail_save(_data):
        raise SecretStoreError("fixture write failure")

    read_checkpoint = tow_bundle._read_checkpoint
    reads = 0

    def fail_checkpoint(checkpoint):
        nonlocal reads
        reads += 1
        if reads == 1:
            return read_checkpoint(checkpoint)  # preparing the checkpoint is verified too
        raise ExportImportError("fixture: checkpoint unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(tow_bundle, "save_secrets", fail_save)
        patch.setattr(tow_bundle, "_read_checkpoint", fail_checkpoint)
        with pytest.raises(ExportImportError, match="import failed and destination rollback also failed"):
            import_bundle(bundle, PASS, apply=True)

    (checkpoint,) = _checkpoints_root().iterdir()
    assert _transaction(checkpoint)["status"] == "prepared"
    assert _targets() != before  # half-written destination is still there ...

    recover_import_transactions()  # ... until the next writer takes the lock

    assert _targets() == before
    assert _transaction(checkpoint)["status"] == "rolled_back"


def test_unrecorded_transaction_log_does_not_undo_a_committed_import(tmp_path, monkeypatch):
    _seed_destination()
    original = tow_bundle._write_import_transaction

    def flaky_marker(checkpoint, *, status, **fields):
        if "log_recorded" in fields:
            raise ExportImportError("fixture: marker write failed")
        return original(checkpoint, status=status, **fields)

    monkeypatch.setattr(tow_bundle, "_write_import_transaction", flaky_marker)
    result = import_bundle(_bundle(tmp_path / "in" / "tow.towx"), PASS, apply=True)

    assert result["committed"] is True
    assert result["transaction_log_status"] == "unrecorded"
    assert load_secrets() == BASE_SECRETS
    assert _transaction(Path(result["checkpoint"]))["status"] == "committed"


def test_import_wraps_unexpected_failures(tmp_path, monkeypatch):
    def broken(*_args):
        raise KeyError("unexpected")

    monkeypatch.setattr(tow_bundle, "_apply_path_maps", broken)
    _rejects(tmp_path, _bundle(tmp_path / "in" / "tow.towx"), "cannot import TOW bundle safely")


# --------------------------------------------------------------------------- rollback


def test_rollback_preview_lists_targets_and_changes_nothing(applied):
    preview = rollback_import(applied["checkpoint"])

    assert preview["preview"] is True
    assert preview["targets"] == [
        "config.yaml",
        "state.json",
        "download_history.json",
        "secrets.enc",
        "secrets-undo.enc",
    ]
    assert _targets() == applied["after"]


def test_rollback_of_missing_checkpoint_is_rejected(tmp_path):
    with pytest.raises(ExportImportError, match="import checkpoint does not exist"):
        rollback_import(tmp_path / "no-such-checkpoint", apply=True)


def _entry(manifest: dict[str, Any], member: str) -> dict[str, Any]:
    return next(entry for entry in manifest["targets"] if entry["member"] == member)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda m: m.update(format="other"), "unsupported import checkpoint"),
        (lambda m: m.update(targets="all"), "unsupported import checkpoint"),
        (lambda m: m["targets"].pop(), "target list is invalid"),
        (lambda m: m["targets"].append(dict(m["targets"][0])), "target list is invalid"),
        (lambda m: m["targets"].append("junk"), "malformed import checkpoint target"),
        (lambda m: _entry(m, "state.json").update(exists="yes"), "exists flag is invalid"),
        (lambda m: _entry(m, "state.json").update(sha256="0" * 64), "checksum mismatch: state.json"),
        (lambda m: _entry(m, "state.json").update(sha256="NOT-HEX"), "checksum mismatch: state.json"),
        (lambda m: _entry(m, "state.json").update(exists=False, sha256=None), "unexpected backup: state.json"),
        (lambda m: _entry(m, "secrets-undo.enc").update(sha256="0" * 64), "unexpected backup: secrets-undo.enc"),
    ],
)
def test_tampered_checkpoint_manifest_blocks_rollback(applied, mutate, match):
    _rewrite_json(applied["checkpoint"] / "MANIFEST.json", mutate)

    for apply in (False, True):
        with pytest.raises(ExportImportError, match=match):
            rollback_import(applied["checkpoint"], apply=apply)
    assert _targets() == applied["after"]


@pytest.mark.parametrize(
    ("damage", "match"),
    [
        (lambda cp: (cp / "MANIFEST.json").write_text("{broken", encoding="utf-8"), "manifest is unreadable"),
        (lambda cp: shutil.rmtree(cp / "files"), "files directory is invalid"),
        (lambda cp: (cp / "files" / "state.json").write_bytes(b"{}"), "checksum mismatch: state.json"),
        (lambda cp: (cp / "files" / "config.yaml").unlink(), "backup is missing: config.yaml"),
    ],
)
def test_damaged_checkpoint_files_block_rollback(applied, damage, match):
    damage(applied["checkpoint"])

    with pytest.raises(ExportImportError, match=match):
        rollback_import(applied["checkpoint"], apply=True)
    assert _targets() == applied["after"]


@pytest.mark.parametrize(
    ("damage", "match"),
    [
        (lambda cp: (cp / "TRANSACTION.json").write_text("{", encoding="utf-8"), "import transaction is unreadable"),
        (lambda cp: _rewrite_json(cp / "TRANSACTION.json", lambda t: t.update(format="x")), "unsupported import"),
        (lambda cp: _rewrite_json(cp / "TRANSACTION.json", lambda t: t.update(status="done")), "invalid import"),
    ],
)
def test_damaged_transaction_journal_blocks_rollback_apply(applied, damage, match):
    damage(applied["checkpoint"])

    with pytest.raises(ExportImportError, match=match):
        rollback_import(applied["checkpoint"], apply=True)
    assert _targets() == applied["after"]


def test_copied_checkpoint_cannot_be_applied_from_its_new_location(applied):
    copy = applied["checkpoint"].with_name(applied["checkpoint"].name + "-copy")
    shutil.copytree(applied["checkpoint"], copy)

    assert rollback_import(copy)["preview"] is True
    with pytest.raises(ExportImportError, match="import transaction checkpoint mismatch"):
        rollback_import(copy, apply=True)
    assert _targets() == applied["after"]


def test_recorded_target_path_never_redirects_a_rollback(applied, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "state.json"
    _rewrite_json(applied["checkpoint"] / "MANIFEST.json", lambda m: _entry(m, "state.json").update(target=""))
    _rewrite_json(
        applied["checkpoint"] / "MANIFEST.json", lambda m: _entry(m, "config.yaml").update(target=str(elsewhere))
    )

    rollback_import(applied["checkpoint"], apply=True)

    assert _targets() == applied["before"]
    assert not elsewhere.exists()


def test_interrupted_import_recovers_after_the_install_moved(tmp_path, monkeypatch):
    before = _seed_destination()
    bundle = _bundle(tmp_path / "in" / "tow.towx")

    def power_loss(*_args, **_kwargs):
        raise KeyboardInterrupt  # stands in for a crash between the checkpoint and the commit

    with monkeypatch.context() as patch:
        patch.setattr(tow_bundle, "save_secrets", power_loss)
        with pytest.raises(KeyboardInterrupt):
            import_bundle(bundle, PASS, apply=True)
    checkpoint = next(_checkpoints_root().iterdir())
    assert _transaction(checkpoint)["status"] == "prepared"
    # The install moves (another drive letter, a renamed folder): every recorded path is stale.
    moved = tmp_path / "moved install"
    shutil.copytree(data_dir(), moved / "data", ignore=shutil.ignore_patterns("*.lock"))
    shutil.copy2(config_path(), moved / "config.yaml")
    monkeypatch.setenv("TOW_HOME", str(moved / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(moved / "config.yaml"))

    save_state({"topics": [{"id": "after-move"}], "mirrors": {}})

    moved_checkpoint = _checkpoints_root() / checkpoint.name
    assert _transaction(moved_checkpoint)["status"] == "rolled_back"
    assert json.loads(state_path().read_text(encoding="utf-8"))["topics"] == [{"id": "after-move"}]
    assert config_path().read_bytes() == before["config"]
    assert load_secrets() == {"qbittorrent": {"password": "old-secret"}}
    assert prune_import_checkpoints(keep=0) == 1


def test_rollback_refuses_a_target_replaced_by_a_directory(applied):
    download_history_path().unlink()
    download_history_path().mkdir()

    with pytest.raises(ExportImportError, match=r"import rollback target is not a file: download_history\.json"):
        rollback_import(applied["checkpoint"], apply=True)
    assert config_path().read_bytes() == applied["after"]["config"]
    assert state_path().read_bytes() == applied["after"]["state"]


def test_rollback_that_cannot_compensate_stays_pending_and_completes_on_next_lock(applied, monkeypatch):
    original = tow_bundle._atomic_write

    def disk_full_after_marker(path, content):
        if path.name == "TRANSACTION.json":
            return original(path, content)
        raise ExportImportError(f"cannot write imported {path.name}")

    with monkeypatch.context() as patch:
        patch.setattr(tow_bundle, "_atomic_write", disk_full_after_marker)
        with pytest.raises(ExportImportError, match="rollback failed; recovery remains pending"):
            rollback_import(applied["checkpoint"], apply=True)

    assert _transaction(applied["checkpoint"])["status"] == "prepared"
    recover_import_transactions()
    assert _targets() == applied["before"]
    assert _transaction(applied["checkpoint"])["status"] == "rolled_back"


def test_rollback_failure_whose_marker_cannot_be_updated_still_preserves_destination(applied, monkeypatch):
    original = tow_bundle._atomic_write
    failed_target = False

    def fail_first_target_then_markers(path, content):
        nonlocal failed_target
        if path.name == "TRANSACTION.json":
            if failed_target:
                raise OSError("fixture: journal disk failure")
            return original(path, content)
        if not failed_target:
            failed_target = True
            raise OSError("fixture: target disk failure")
        return original(path, content)

    monkeypatch.setattr(tow_bundle, "_atomic_write", fail_first_target_then_markers)
    with pytest.raises(ExportImportError, match="destination preserved but marker update failed"):
        rollback_import(applied["checkpoint"], apply=True)

    assert _targets() == applied["after"]


def test_rollback_reports_a_marker_failure_after_restoring_files(applied, monkeypatch):
    original = tow_bundle._atomic_write

    def fail_final_marker(path, content):
        if path.name == "TRANSACTION.json" and json.loads(content)["status"] == "rolled_back":
            raise OSError("fixture: journal disk failure")
        return original(path, content)

    monkeypatch.setattr(tow_bundle, "_atomic_write", fail_final_marker)
    with pytest.raises(ExportImportError, match="rollback applied but transaction marker update failed"):
        rollback_import(applied["checkpoint"], apply=True)

    assert _targets() == applied["before"]


def test_rollback_wraps_unexpected_failures(applied, monkeypatch):
    def broken(_checkpoint):
        raise KeyError("unexpected")

    monkeypatch.setattr(tow_bundle, "_read_checkpoint", broken)
    with pytest.raises(ExportImportError, match="cannot roll back import checkpoint safely"):
        rollback_import(applied["checkpoint"])


def test_interrupted_import_whose_checkpoint_is_corrupt_fails_closed(applied):
    _rewrite_json(applied["checkpoint"] / "TRANSACTION.json", lambda t: t.update(status="prepared"))
    (applied["checkpoint"] / "files" / "state.json").write_bytes(b"{}")

    with pytest.raises(ExportImportError, match="incomplete import recovery failed"):
        recover_import_transactions()
    with pytest.raises(ExportImportError, match="incomplete import recovery failed"):
        save_state({"topics": [], "mirrors": {}})
    assert _targets() == applied["after"]


# --------------------------------------------------------------------------- prune


def _fake_checkpoint(name: str, status: str | None) -> Path:
    checkpoint = _checkpoints_root() / name
    checkpoint.mkdir(parents=True)
    (checkpoint / "files").mkdir()
    manifest = {
        "format": tow_bundle.CHECKPOINT_FORMAT,
        "targets": [
            {"member": member, "target": str(target), "exists": False, "sha256": None}
            for member, target in tow_bundle._checkpoint_targets()
        ],
    }
    (checkpoint / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    if status is None:
        (checkpoint / "TRANSACTION.json").write_text("{", encoding="utf-8")
    else:
        transaction = {"format": "tow-import-transaction-v1", "checkpoint": str(checkpoint), "status": status}
        (checkpoint / "TRANSACTION.json").write_text(json.dumps(transaction), encoding="utf-8")
    return checkpoint


def test_prune_without_checkpoints_removes_nothing():
    assert prune_import_checkpoints() == 0


def test_prune_keeps_newest_and_never_removes_unfinished_or_unreadable_checkpoints():
    statuses = {
        "2000-01-01": "committed",
        "2000-01-02": "rolled_back",
        "2000-01-03": "prepared",
        "2000-01-04": None,
        "2000-01-05": "committed",
        "2000-01-06": "committed",
        "2000-01-07": "rolled_back",
    }
    for name, status in statuses.items():
        _fake_checkpoint(name, status)
    (_checkpoints_root() / "2000-01-09-stray-file").write_text("x", encoding="utf-8")

    assert prune_import_checkpoints(keep=2) == 3

    remaining = sorted(path.name for path in _checkpoints_root().iterdir())
    assert remaining == ["2000-01-03", "2000-01-04", "2000-01-06", "2000-01-07", "2000-01-09-stray-file"]


def test_negative_keep_prunes_every_finished_checkpoint():
    _fake_checkpoint("2000-01-01", "committed")
    _fake_checkpoint("2000-01-02", "prepared")

    assert prune_import_checkpoints(keep=-3) == 1
    assert [path.name for path in _checkpoints_root().iterdir()] == ["2000-01-02"]


def test_applied_import_prunes_old_finished_checkpoints(tmp_path):
    for day in range(1, 7):
        _fake_checkpoint(f"2000-01-0{day}", "committed")

    result = import_bundle(_bundle(tmp_path / "in" / "tow.towx"), PASS, apply=True)

    assert result["pruned_checkpoints"] == 2
    remaining = sorted(path.name for path in _checkpoints_root().iterdir())
    assert len(remaining) == 5
    assert Path(result["checkpoint"]).name in remaining
    assert "2000-01-01" not in remaining
    assert "2000-01-02" not in remaining
