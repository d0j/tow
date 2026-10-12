"""The portable TOW file (``.towx``): export, import, the import checkpoint and its rollback.

- ``errors``: ``ExportImportError``;
- ``io``: the format name, the size limit, JSON bytes, checksums, bounded reads, atomic writes;
- ``secret_scan``: no credential-named field outside the encrypted secrets;
- ``schema``: what each member may contain;
- ``crypto``: the passphrase key (PBKDF2) and the Fernet envelope;
- ``archive``: the zip, its manifest, reading a bundle back.

The rest of TOW imports only the names below. A test that replaces a name does it in the module
that reads it when it runs. What has no module of its own yet is below.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import re
import shutil
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from tow import __version__
from tow.bundle.archive import (
    ALLOWED_MEMBERS,
    OPTIONAL_MEMBERS,
    REQUIRED_MEMBERS,
    read_bundle,
    verify_bundle,
    zip_payload,
)
from tow.bundle.crypto import KDF_ITERATIONS, KDF_NAME, KDF_SALT_BYTES, seal_outer
from tow.bundle.errors import ExportImportError
from tow.bundle.io import FORMAT, MAX_BUNDLE_BYTES, atomic_write, json_bytes, read_limited, sha256
from tow.bundle.schema import parse_mapping, utf8_config, validate_config_schema, validate_field
from tow.bundle.secret_scan import refuse_plaintext_secrets
from tow.clock import iso_now
from tow.errors import Msg
from tow.log import export_event_projection, log_event, read_events
from tow.paths import config_path, data_dir, download_history_path, secrets_path, state_path
from tow.platform import plain_kind
from tow.store import (
    STATE_SCHEMA_VERSION,
    SecretStoreError,
    StoreCorruptionError,
    atomic_create_bytes,
    decode_json_bytes,
    encrypted_secrets_path,
    load_json,
    load_secret_undo,
    load_secrets,
    load_state,
    master_fernet,
    persistence_lock,
    restrict_file,
    save_secret_undo,
    save_secrets,
    secret_undo_path,
)
from tow.yaml_guard import YamlLimitError
from tow.yaml_guard import dump as dump_yaml

CHECKPOINT_FORMAT = "tow-import-checkpoint-v1"
TRANSACTION_FORMAT = "tow-import-transaction-v1"

# The backup folders of the install a bundle is restored into (a restore point keeps them).
_FOLDER_OVERRIDES = ("backup_dir", "restore_points_dir")

_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _build_export_members(*, include_log: bool) -> dict[str, bytes]:
    config_file = config_path()
    config_bytes = utf8_config(read_limited(config_file, label="config.yaml"))
    config_data = parse_mapping(config_bytes, label="config.yaml")
    # load_state intentionally hides the format header from callers. An archive must
    # restore it, or an old reader could accept newer fields after importing it.
    state_data = {"schema_version": STATE_SCHEMA_VERSION, **load_state()}
    history_data = load_json_for_export(download_history_path(), {"schema_version": 1, "topics": {}})
    secrets_data = load_secrets()
    refuse_plaintext_secrets(config_data, state_data, history_data)
    state_bytes = json_bytes(state_data)
    history_bytes = json_bytes(history_data)
    members = {
        "config.yaml": config_bytes,
        "state.json": state_bytes,
        "download_history.json": history_bytes,
        "secrets.json": json_bytes(secrets_data),
    }
    undo = state_data.get("undo")
    if isinstance(undo, dict) and undo.get("kind") == "settings":
        if undo.get("secrets_undo_ref") != "settings-v1":
            raise ExportImportError("settings undo reference is invalid")
        try:
            undo_secrets = load_secret_undo("settings-v1")
        except SecretStoreError as exc:
            raise ExportImportError("cannot read encrypted settings undo snapshot") from exc
        members["secrets_undo.json"] = json_bytes({"secrets": undo_secrets})
    if include_log:
        members["events.json"] = json_bytes(
            {"events": [export_event_projection(event) for event in read_events(limit=200)]}
        )
    manifest = {
        "format": FORMAT,
        "schema_version": 1,
        "source_version": __version__,
        "members": sorted(members),
        "sha256": {name: sha256(data) for name, data in sorted(members.items())},
        "excluded": [
            "media_bytes",
            "torrent_files",
            "torrent_client_database",
            "os_keyrings",
            "scheduler_registrations",
            "caches",
            "source_master_key",
        ],
    }
    return {"manifest.json": json_bytes(manifest), **members}


def load_json_for_export(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        if not _checkpoint_file(path, missing=True):
            # A quarantined history is not an empty history. Do not make an
            # apparently healthy backup that silently omits the original data.
            data = load_json(path, default, quarantine=False)
        else:
            raw = read_limited(path, label=path.name)
            data = decode_json_bytes(raw)
    except (UnicodeError, ValueError, RecursionError, StoreCorruptionError) as exc:
        raise ExportImportError(f"invalid {path.name}") from exc
    if not isinstance(data, dict):
        raise ExportImportError(f"{path.name} must be an object")
    return data


def export_bundle(
    output: Path, passphrase: str, *, include_log: bool = False, overwrite: bool = False
) -> dict[str, Any]:
    try:
        with persistence_lock():
            return _export_bundle(output, passphrase, include_log=include_log, overwrite=overwrite)
    except ExportImportError:
        raise
    except Exception as exc:
        raise ExportImportError("cannot create export bundle safely") from exc


def _export_bundle(
    output: Path, passphrase: str, *, include_log: bool = False, overwrite: bool = False
) -> dict[str, Any]:
    output = Path(output)
    if output.exists() and not overwrite:
        raise ExportImportError(
            "export output already exists; choose another path or use --force",
            owner_text=Msg("cli.bundle.output_exists", path=str(output)),
        )
    previous = output.read_bytes() if overwrite and output.is_file() else None
    members = _build_export_members(include_log=include_log)
    payload = zip_payload(members)
    outer = seal_outer(payload, passphrase)
    content = json_bytes(outer)
    if overwrite:
        atomic_write(output, content)
    else:
        atomic_create_bytes(output, content)
    restrict_file(output)
    try:
        verified = read_bundle(output, passphrase)
        if set(verified["members"]) != set(members):
            raise ExportImportError("export bundle read-back member mismatch")
    except ExportImportError:
        if previous is None:
            with contextlib.suppress(OSError):
                if not output.is_symlink() and output.read_bytes() == content:
                    output.unlink()  # never remove a replacement supplied by another writer
        else:
            atomic_write(output, previous)
        raise
    return {
        "ok": True,
        "format": FORMAT,
        "preview": False,
        "source_mutation": False,
        "read_back": True,
        "output": str(output),
        "members": sorted(name for name in members if name != "manifest.json"),
        "bytes": output.stat().st_size,
        "secrets": "encrypted-in-bundle",
        "source_master_key": "not-included",
    }


def parse_path_maps(values: Iterable[str] | None) -> list[tuple[str, str]]:
    def refuse(message: str) -> ExportImportError:
        return ExportImportError(message, owner_text=Msg("cli.bundle.path_map"))

    result = []
    for raw in values or ():
        if not isinstance(raw, str) or "=" not in raw:
            raise refuse("path map must use OLD=NEW")
        old, new = raw.split("=", 1)
        old, new = old.strip(), new.strip()
        if not old or not new:
            raise refuse("path map must contain non-empty OLD and NEW")
        if old == new:
            raise refuse("path map OLD and NEW must differ")
        result.append((old, new))
    if len({old for old, _ in result}) != len(result):
        raise refuse("duplicate path map source")
    return result


def _looks_absolute(value: str) -> bool:
    return bool(_DRIVE_PATH.match(value) or value.startswith(("/", "\\\\")))


def _map_one(value: str, mappings: list[tuple[str, str]]) -> tuple[str, bool]:
    for old, new in mappings:
        old_clean = old.rstrip("/\\")
        if value == old_clean:
            return new, True
        for separator in ("/", "\\"):
            prefix = old_clean + separator
            if value.startswith(prefix):
                return new.rstrip("/\\") + value[len(old_clean) :], True
    return value, False


def _apply_path_maps(state: dict[str, Any], mappings: list[tuple[str, str]]) -> tuple[dict[str, Any], list[str]]:
    result = copy.deepcopy(state)
    warnings: list[str] = []
    roots = result.get("save_roots")
    if isinstance(roots, list):
        for index, value in enumerate(roots):
            if isinstance(value, str):
                mapped, matched = _map_one(value, mappings)
                result["save_roots"][index] = mapped
                if _looks_absolute(value) and not matched:
                    warnings.append(f"unmapped save root: {value}")
    for topic in result.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        value = topic.get("save_path")
        if isinstance(value, str):
            mapped, matched = _map_one(value, mappings)
            topic["save_path"] = mapped
            if _looks_absolute(value) and not matched:
                warnings.append(f"unmapped topic path: {value}")
    return result, warnings


def _checkpoint_targets() -> list[tuple[str, Path]]:
    return [
        ("config.yaml", config_path()),
        ("state.json", state_path()),
        ("download_history.json", download_history_path()),
        ("secrets.enc", encrypted_secrets_path()),
        ("secrets-undo.enc", secret_undo_path()),
    ]


def _create_import_checkpoint() -> Path:
    checkpoint = data_dir() / "import-checkpoints" / f"{iso_now().replace(':', '-')}-{uuid.uuid4().hex[:10]}"
    files_dir = checkpoint / "files"
    files_dir.mkdir(parents=True, exist_ok=False)
    try:
        return _fill_import_checkpoint(checkpoint, files_dir)
    except BaseException:
        # Nothing was imported yet. A half copy of every store (a full disk, a file held open)
        # has no journal, so the pruning never removed it: one more per attempt, on that disk.
        shutil.rmtree(checkpoint, ignore_errors=True)
        raise


def _fill_import_checkpoint(checkpoint: Path, files_dir: Path) -> Path:
    entries = []
    for member, target in _checkpoint_targets():
        exists = _checkpoint_file(target, missing=True)
        entry = {"member": member, "target": str(target), "exists": exists}
        if exists:
            content = read_limited(target, label=member)
            backup = files_dir / member
            backup.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(backup, content)
            entry["sha256"] = sha256(content)
        else:
            entry["sha256"] = None
        entries.append(entry)
    manifest = {
        "format": CHECKPOINT_FORMAT,
        "created_at": iso_now(),
        "checkpoint_id": checkpoint.name,
        "targets": entries,
        "excluded": ["plaintext secrets", "TOW_MASTER_KEY", "TOW_MASTER_KEY_FILE", "media bytes", "qBittorrent data"],
    }
    atomic_write(checkpoint / "MANIFEST.json", json_bytes(manifest, readable=True))
    _read_checkpoint(checkpoint)  # every copy is intact before the operation can start
    _write_import_transaction(checkpoint, status="prepared")
    return checkpoint


def _write_import_transaction(checkpoint: Path, *, status: str, **fields: Any) -> None:
    if status not in {"prepared", "committed", "rolled_back"}:
        raise ExportImportError("invalid import transaction status")
    transaction = {
        "format": TRANSACTION_FORMAT,
        "checkpoint": str(Path(checkpoint)),
        "status": status,
        "updated_at": iso_now(),
        **fields,
    }
    atomic_write(Path(checkpoint) / "TRANSACTION.json", json_bytes(transaction, readable=True))


def _read_import_transaction(checkpoint: Path, *, bound: bool = True) -> dict[str, Any]:
    path = Path(checkpoint) / "TRANSACTION.json"
    try:
        _checkpoint_directory(Path(checkpoint))
        _checkpoint_file(path)
        value = decode_json_bytes(read_limited(path, label="import transaction"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ExportImportError("import transaction is unreadable") from exc
    if not isinstance(value, dict) or value.get("format") != TRANSACTION_FORMAT:
        raise ExportImportError("unsupported import transaction")
    if not isinstance(value.get("status"), str) or value["status"] not in {"prepared", "committed", "rolled_back"}:
        raise ExportImportError("invalid import transaction status")
    # Bound by folder name, not by absolute path: a moved install (another drive letter, a
    # renamed folder, another system) still recovers its own interrupted import.
    if not isinstance(value.get("checkpoint"), str) or (
        bound and re.split(r"[\\/]", value["checkpoint"])[-1] != Path(checkpoint).name
    ):
        raise ExportImportError("import transaction checkpoint mismatch")
    return value


def recover_import_transactions() -> list[str]:
    with persistence_lock():
        return _recover_import_transactions_locked()


def _recover_import_transactions_locked() -> list[str]:
    root = data_dir() / "import-checkpoints"
    if not _checkpoint_directory(root, missing=True):
        return []
    recovered: list[str] = []
    try:
        checkpoints = sorted(path for path in root.iterdir() if path.is_dir())
    except OSError as exc:
        raise ExportImportError("cannot inspect import transactions") from exc
    for checkpoint in checkpoints:
        if not _checkpoint_file(checkpoint / "TRANSACTION.json", missing=True):
            continue
        transaction = _read_import_transaction(checkpoint, bound=False)
        if transaction["status"] != "prepared":
            continue
        try:
            _rollback_import(checkpoint, apply=True)
            _write_import_transaction(checkpoint, status="rolled_back", recovered=True)
        except ExportImportError as exc:
            raise ExportImportError("incomplete import recovery failed") from exc
        recovered.append(str(checkpoint))
    return recovered


def recover_interrupted_import() -> None:
    """A recovery step (``tow.store.RECOVERY_STEPS``), the data lock held.

    An import runs entirely under the persistence lock, so a "prepared" journal seen when
    a process takes the lock was left by a crash: roll it back before anyone writes.
    An unreadable marker cannot prove that an import finished: it also fails closed.
    """
    _recover_import_transactions_locked()


def _checkpoint_file(path: Path, *, missing: bool = False) -> bool:
    try:
        kind = plain_kind(path)
    except OSError as exc:
        raise ExportImportError("import checkpoint file cannot be inspected") from exc
    if kind is None:
        if missing:
            return False
        raise ExportImportError("import checkpoint file is missing") from None
    if kind != "file":
        raise ExportImportError("import checkpoint file is unsafe")
    return True


def _checkpoint_directory(path: Path, *, missing: bool = False) -> bool:
    try:
        kind = plain_kind(path)
    except OSError as exc:
        raise ExportImportError("import checkpoint directory cannot be inspected") from exc
    if kind is None:
        if missing:
            return False
        raise ExportImportError("import checkpoint directory is missing") from None
    if kind != "dir":
        raise ExportImportError("import checkpoint directory is unsafe")
    return True


def _read_checkpoint(checkpoint: Path) -> dict[str, Any]:
    checkpoint = Path(checkpoint)
    if not _checkpoint_directory(checkpoint, missing=True):
        raise ExportImportError(
            "import checkpoint does not exist", owner_text=Msg("cli.bundle.no_checkpoint", path=str(checkpoint))
        )
    try:
        _checkpoint_directory(checkpoint)
        _checkpoint_file(checkpoint / "MANIFEST.json")
        data = decode_json_bytes(read_limited(checkpoint / "MANIFEST.json", label="import checkpoint manifest"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ExportImportError("import checkpoint manifest is unreadable") from exc
    if (
        not isinstance(data, dict)
        or data.get("format") != CHECKPOINT_FORMAT
        or not isinstance(data.get("targets"), list)
    ):
        raise ExportImportError("unsupported import checkpoint")
    expected_targets = dict(_checkpoint_targets())
    entries = data["targets"]
    valid_entries = all(isinstance(entry, dict) and isinstance(entry.get("member"), str) for entry in entries)
    if not valid_entries:
        raise ExportImportError("malformed import checkpoint target")
    target_members = [entry["member"] for entry in entries]
    if len(target_members) != len(set(target_members)) or set(target_members) != set(expected_targets):
        raise ExportImportError("import checkpoint target list is invalid")
    files_dir = checkpoint / "files"
    if not _checkpoint_directory(files_dir, missing=True):
        raise ExportImportError("import checkpoint files directory is invalid")
    for entry in data["targets"]:
        if not isinstance(entry, dict) or entry.get("member") not in expected_targets:
            raise ExportImportError("malformed import checkpoint target")
        # The member name alone says where a file goes back to; the recorded absolute
        # "target" is informative only (it changes when the install moves).
        member = str(entry["member"])
        exists = entry.get("exists")
        if not isinstance(exists, bool):
            raise ExportImportError("import checkpoint exists flag is invalid")
        checksum = entry.get("sha256")
        backup = files_dir / member
        if exists:
            if not _checkpoint_file(backup, missing=True):
                raise ExportImportError(f"import checkpoint backup is missing: {member}")
            content = read_limited(backup, label=f"checkpoint {member}")
            if (
                not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                or checksum != sha256(content)
            ):
                raise ExportImportError(f"import checkpoint checksum mismatch: {member}")
        elif checksum is not None or backup.exists():
            raise ExportImportError(f"import checkpoint has an unexpected backup: {member}")
    return data


def rollback_import(checkpoint: Path, *, apply: bool = False) -> dict[str, Any]:
    try:
        with persistence_lock():
            return _rollback_import(checkpoint, apply=apply)
    except ExportImportError:
        raise
    except Exception as exc:
        raise ExportImportError("cannot roll back import checkpoint safely") from exc


def _capture_runtime_targets(expected_targets: dict[str, Path]) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for member, target in expected_targets.items():
        try:
            exists = _checkpoint_file(target, missing=True)
        except ExportImportError as exc:
            raise ExportImportError(f"import rollback target is not a file: {member}") from exc
        if exists:
            try:
                snapshot[member] = target.read_bytes()
            except OSError as exc:
                raise ExportImportError(f"cannot snapshot rollback target: {member}") from exc
        else:
            snapshot[member] = None
    return snapshot


def _restore_runtime_targets(snapshot: dict[str, bytes | None], expected_targets: dict[str, Path]) -> None:
    for member, target in expected_targets.items():
        content = snapshot[member]
        if content is None:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ExportImportError(f"cannot compensate rollback target: {member}") from exc
            if target.exists() or target.is_symlink():
                raise ExportImportError(f"rollback compensation could not remove {member}")
            continue
        atomic_write(target, content)
        try:
            if target.read_bytes() != content:
                raise ExportImportError(f"rollback compensation read-back mismatch: {member}")
        except OSError as exc:
            raise ExportImportError(f"cannot read rollback compensation: {member}") from exc


def _rollback_import(checkpoint: Path, *, apply: bool = False) -> dict[str, Any]:
    checkpoint = Path(checkpoint)
    manifest = _read_checkpoint(checkpoint)
    expected_targets = dict(_checkpoint_targets())
    if not apply:
        return {
            "ok": True,
            "preview": True,
            "checkpoint": str(checkpoint),
            "targets": [entry.get("member") for entry in manifest["targets"] if isinstance(entry, dict)],
        }

    transaction = _read_import_transaction(checkpoint)
    operation_id = uuid.uuid4().hex
    snapshot = _capture_runtime_targets(expected_targets)
    restore_plan: list[tuple[str, Path, bytes | None]] = []
    for entry in manifest["targets"]:
        if not isinstance(entry, dict) or entry.get("member") not in expected_targets:
            raise ExportImportError("malformed import checkpoint target")
        member = str(entry["member"])
        target = expected_targets[member]
        backup = checkpoint / "files" / member
        if entry.get("exists"):
            content = read_limited(backup, label=f"checkpoint {member}")
            if entry.get("sha256") != sha256(content):
                raise ExportImportError(f"import checkpoint checksum mismatch: {member}")
        else:
            content = None
        restore_plan.append((member, target, content))

    _write_import_transaction(checkpoint, status="prepared", operation_id=operation_id, phase="rollback")
    restored: list[str] = []
    try:
        for member, target, content in restore_plan:
            if content is not None:
                atomic_write(target, content)
                if target.read_bytes() != content:
                    raise ExportImportError(f"import rollback read-back mismatch: {member}")
            else:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise ExportImportError(f"cannot remove imported {member}") from exc
                if target.exists():
                    raise ExportImportError(f"import rollback could not remove {member}")
            restored.append(member)
    except Exception as exc:
        try:
            _restore_runtime_targets(snapshot, expected_targets)
        except Exception as compensation_exc:
            raise ExportImportError("rollback failed; recovery remains pending") from compensation_exc
        # The destination was put back as it was, so the marker gets back its own status too.
        # Turning a "rolled_back" marker into "prepared" made the next lock holder roll back
        # once more - silently losing every edit made since the first rollback.
        previous = transaction.get("status")
        status = previous if previous in {"committed", "rolled_back"} else "prepared"
        try:
            _write_import_transaction(
                checkpoint,
                status=status,
                operation_id=operation_id,
                phase="rollback",
                rollback_status="failed_compensated",
                rollback_error=type(exc).__name__,
            )
        except Exception as marker_exc:
            raise ExportImportError("rollback failed; destination preserved but marker update failed") from marker_exc
        raise ExportImportError("rollback failed; destination preserved") from exc

    try:
        _write_import_transaction(checkpoint, status="rolled_back", operation_id=operation_id, phase="rollback")
    except Exception as exc:
        raise ExportImportError("rollback applied but transaction marker update failed") from exc
    return {"ok": True, "checkpoint": str(checkpoint), "restored": restored}


def _destination_readback(parsed: dict[str, Any], state_bytes: bytes, history_bytes: bytes) -> None:
    if config_path().read_bytes() != parsed["config_bytes"]:
        raise ExportImportError("config read-back mismatch")
    if state_path().read_bytes() != state_bytes:
        raise ExportImportError("state read-back mismatch")
    if download_history_path().read_bytes() != history_bytes:
        raise ExportImportError("download history read-back mismatch")
    try:
        if load_secrets() != parsed["secrets"]:
            raise ExportImportError("secrets semantic read-back mismatch")
        if parsed["undo"] is not None and load_secret_undo("settings-v1") != parsed["undo"]["secrets"]:
            raise ExportImportError("settings undo semantic read-back mismatch")
    except SecretStoreError as exc:
        raise ExportImportError("encrypted destination secret read-back failed") from exc


def _record_import_event(checkpoint: Path, mappings: list[tuple[str, str]], members: list[str]) -> bool:
    try:
        return log_event(
            "portable_import_applied",
            operation_id=uuid.uuid4().hex,
            format=FORMAT,
            checkpoint=str(checkpoint),
            path_maps=mappings,
            members=members,
        )
    except Exception as exc:  # noqa: BLE001 - the import is committed; a lost audit line is not fatal
        logging.getLogger("tow.bundle").warning("import audit event not written: %s", type(exc).__name__)
        return False


def import_bundle(
    input_path: Path,
    passphrase: str,
    *,
    apply: bool = False,
    path_maps: Iterable[str] | None = None,
    config_overrides: dict[str, Any] | None = None,
    preserve_secret_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    try:
        with persistence_lock():
            return _import_bundle(
                input_path,
                passphrase,
                apply=apply,
                path_maps=path_maps,
                config_overrides=config_overrides,
                preserve_secret_keys=preserve_secret_keys,
            )
    except ExportImportError:
        raise
    except Exception as exc:
        raise ExportImportError("cannot import TOW bundle safely") from exc


def _config_override_bytes(effective_config: dict[str, Any]) -> bytes:
    try:
        return dump_yaml(effective_config).encode("utf-8")
    except YamlLimitError as exc:
        raise ExportImportError(f"invalid config.yaml: {exc}") from exc
    except (TypeError, ValueError, UnicodeError, yaml.YAMLError) as exc:
        raise ExportImportError("cannot apply safe config overrides") from exc


def _apply_config_overrides(parsed: dict[str, Any], overrides: dict[str, Any]) -> None:
    """This install's own settings over the bundle's config.yaml (``parsed`` is changed in place)."""
    if not set(overrides).issubset({"bind", "port", "allow_lan", *_FOLDER_OVERRIDES}):
        raise ExportImportError("unsupported config override")
    # Typed values from the CLI or the web form: no "yes" or "80" written for them.
    validate_field(overrides, "bind", str, label="config.yaml")
    validate_field(overrides, "port", int, label="config.yaml")
    validate_field(overrides, "allow_lan", bool, label="config.yaml")
    for key in _FOLDER_OVERRIDES:
        validate_field(overrides, key, str, label="config.yaml")
    effective_config = copy.deepcopy(parsed["config"])
    for key, value in overrides.items():
        if key in _FOLDER_OVERRIDES and value is None:
            effective_config.pop(key, None)  # this install has none: the default folder, not the bundle's
        else:
            effective_config[key] = copy.deepcopy(value)
    validate_config_schema(effective_config)
    parsed["config_bytes"] = _config_override_bytes(effective_config)
    parsed["config"] = effective_config


def _import_bundle(
    input_path: Path,
    passphrase: str,
    *,
    apply: bool = False,
    path_maps: Iterable[str] | None = None,
    config_overrides: dict[str, Any] | None = None,
    preserve_secret_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    recover_import_transactions()
    parsed = read_bundle(Path(input_path), passphrase)
    overrides = dict(config_overrides or {})
    if overrides:
        _apply_config_overrides(parsed, overrides)
    secret_keys = {str(key) for key in (preserve_secret_keys or ())}
    if not secret_keys.issubset({"lan_auth"}):
        raise ExportImportError("unsupported preserved secret key")
    if secret_keys:
        try:
            destination_secrets = load_secrets()
        except SecretStoreError as exc:
            raise ExportImportError("cannot read destination secrets to preserve access") from exc
        effective_secrets = copy.deepcopy(parsed["secrets"])
        for key in secret_keys:
            if key in destination_secrets:
                effective_secrets[key] = copy.deepcopy(destination_secrets[key])
            else:
                effective_secrets.pop(key, None)
        parsed["secrets"] = effective_secrets
    mappings = parse_path_maps(path_maps)
    mapped_state, warnings = _apply_path_maps(parsed["state"], mappings)
    state_bytes = json_bytes(mapped_state, readable=True)  # as tow.store writes each file
    history_bytes = json_bytes(parsed["history"])
    members = sorted(name for name in parsed["members"] if name != "manifest.json")
    result = {
        "ok": True,
        "format": FORMAT,
        "preview": not apply,
        "apply_required": not apply,
        "members": members,
        "path_maps": [{"old": old, "new": new} for old, new in mappings],
        "config_overrides": sorted(overrides),
        "preserved_secret_keys": sorted(secret_keys),
        "path_warnings": warnings,
        "scheduler": "not restored; turn autostart on separately with tow autostart on",
        "client_media": "not transferred; qBittorrent/client check is separate",
        "committed": False,
        "log_recorded": False,
    }
    if not apply:
        return result
    if secrets_path().is_file():
        raise ExportImportError(
            "destination legacy plaintext secrets require explicit migration before import",
            owner_text=Msg("cli.bundle.legacy_secrets", command="tow secrets migrate"),
        )
    try:
        master_fernet()
    except SecretStoreError as exc:
        raise ExportImportError(
            "destination master key is unavailable or invalid", owner_text=Msg("cli.bundle.master_key")
        ) from exc

    checkpoint = _create_import_checkpoint()
    try:
        atomic_write(config_path(), parsed["config_bytes"])
        atomic_write(state_path(), state_bytes)
        atomic_write(download_history_path(), history_bytes)
        save_secrets(parsed["secrets"])
        if parsed["undo"] is not None:
            save_secret_undo(parsed["undo"]["secrets"])
        else:
            try:
                secret_undo_path().unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ExportImportError("cannot remove stale settings undo snapshot") from exc
        _destination_readback(parsed, state_bytes, history_bytes)
        _write_import_transaction(checkpoint, status="committed", operation_id=uuid.uuid4().hex)
    except (ExportImportError, SecretStoreError, OSError) as exc:
        try:
            rollback_import(checkpoint, apply=True)
        except ExportImportError as rollback_exc:
            raise ExportImportError(
                "import failed and destination rollback also failed", owner_text=Msg("cli.bundle.not_restored")
            ) from rollback_exc
        if isinstance(exc, ExportImportError):
            exc.owner_text = exc.owner_text or Msg("cli.bundle.restored")  # what failed is in the message
            raise
        raise ExportImportError(
            "import failed; destination was restored", owner_text=Msg("cli.bundle.restored")
        ) from exc

    result.update(
        {"preview": False, "apply_required": False, "checkpoint": str(checkpoint), "read_back": True, "committed": True}
    )
    result["log_recorded"] = _record_import_event(checkpoint, mappings, members)
    if not result["log_recorded"]:
        result["log_error"] = "audit event could not be persisted"
    try:
        _write_import_transaction(checkpoint, status="committed", log_recorded=result["log_recorded"])
    except ExportImportError:
        result["transaction_log_status"] = "unrecorded"
    result["pruned_checkpoints"] = prune_import_checkpoints(keep=IMPORT_CHECKPOINTS_KEPT)
    return result


IMPORT_CHECKPOINTS_KEPT = 5


def _owned_checkpoint(checkpoint: Path) -> bool:
    manifest = _read_checkpoint(checkpoint)
    allowed = {"MANIFEST.json", "TRANSACTION.json", "files"}
    if {entry.name for entry in checkpoint.iterdir()} != allowed:
        return False
    members = {entry["member"] for entry in manifest["targets"] if entry["exists"]}
    for entry in (checkpoint / "files").iterdir():
        if entry.name not in members:
            return False
        _checkpoint_file(entry)
    return True


def prune_import_checkpoints(*, keep: int = IMPORT_CHECKPOINTS_KEPT) -> int:
    """Remove finished import checkpoints beyond the newest ``keep``.

    Each one is a full copy of config, state, history and encrypted secrets, and they
    were never removed. Only ``committed``/``rolled_back`` ones are deleted; a
    ``prepared`` (in-flight or unrecovered) or unreadable checkpoint is always kept.
    """
    root = data_dir() / "import-checkpoints"
    try:
        if not _checkpoint_directory(root, missing=True):
            return 0
        checkpoints = sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()), reverse=True)
    except ExportImportError, OSError:
        return 0
    removed = 0
    for checkpoint in checkpoints[max(0, keep) :]:
        try:
            if _read_import_transaction(checkpoint)["status"] not in {"committed", "rolled_back"}:
                continue
            if not _owned_checkpoint(checkpoint):
                continue
            shutil.rmtree(checkpoint)
            removed += 1
        except ExportImportError, OSError:
            continue
    return removed


__all__ = [
    "ALLOWED_MEMBERS",
    "CHECKPOINT_FORMAT",
    "FORMAT",
    "IMPORT_CHECKPOINTS_KEPT",
    "KDF_ITERATIONS",
    "KDF_NAME",
    "KDF_SALT_BYTES",
    "MAX_BUNDLE_BYTES",
    "OPTIONAL_MEMBERS",
    "REQUIRED_MEMBERS",
    "TRANSACTION_FORMAT",
    "ExportImportError",
    "export_bundle",
    "import_bundle",
    "load_json_for_export",
    "parse_path_maps",
    "prune_import_checkpoints",
    "recover_import_transactions",
    "recover_interrupted_import",
    "rollback_import",
    "verify_bundle",
]
