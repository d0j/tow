"""Corrupt night-copy descriptions must not break Settings or allocate unbounded input."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tow import snapshots
from tow.config import load_config, save_config
from tow.i18n import t
from tow.paths import config_path, data_dir
from tow.web.text import format_bytes


@pytest.fixture
def night(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    root = tmp_path / "night"
    cfg = load_config()
    cfg["backup_dir"] = str(root)
    save_config(cfg)
    return root


def _plant(root, content):
    folder = root / "tow-fixture"
    folder.mkdir(parents=True)
    (folder / "MANIFEST.json").write_text(content, encoding="utf-8")
    return folder


def _manifest(size, *, format_value=snapshots.FORMAT, created="2026-01-01T00:00:00+00:00"):
    return {
        "format": format_value,
        "created_at": created,
        "files": {"state.json": {"size": size}},
        "version": "1.22.40",
    }


@pytest.mark.parametrize("size", [None, -1, True, False, "12", 1.5, float("inf"), float("nan"), 10**400, 2**63, {}, []])
@pytest.mark.parametrize("surface", ["list", "settings"])
def test_corrupt_sizes_are_not_coerced_or_allowed_to_break_settings(night, size, surface):
    _plant(night, json.dumps(_manifest(size)))
    before = config_path().read_bytes()
    if surface == "list":
        assert snapshots.list_snapshots() == []
    else:
        from tow.web import app

        response = TestClient(app, headers={"Origin": "http://127.0.0.1"}, raise_server_exceptions=False).get(
            "/settings"
        )
        assert response.status_code == 200
    assert config_path().read_bytes() == before


@pytest.mark.parametrize("size", [None, -1, True, False, "12", 1.5, float("inf"), float("nan"), 10**400, 2**63, {}, []])
def test_verification_refuses_invalid_sizes_before_inspecting_a_member(night, size, monkeypatch):
    folder = _plant(night, json.dumps(_manifest(size, format_value=snapshots.UNSIGNED_FORMAT)))
    real_stat = Path.stat

    def inspected(path, *args, **kwargs):
        if path == folder / "state.json":
            pytest.fail("invalid size reached the member filesystem probe")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", inspected)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.verify_snapshot(folder)


@pytest.mark.parametrize("size", [0, 1, 1024, snapshots.MAX_MEMBER_SIZE])
@pytest.mark.parametrize("format_value", [snapshots.FORMAT, snapshots.UNSIGNED_FORMAT])
def test_inventory_accepts_exact_integer_sizes_without_claiming_signature_verification(night, size, format_value):
    _plant(night, json.dumps(_manifest(size, format_value=format_value)))
    rows = snapshots.list_snapshots()
    assert len(rows) == 1
    assert rows[0]["bytes"] == size
    assert format_bytes(rows[0]["bytes"]) != "—"


@pytest.mark.parametrize("created", [None, "not-a-date", "2026-99-01", "2026-01-01T99:00:00", [], {}])
def test_invalid_creation_dates_are_skipped(night, created):
    _plant(night, json.dumps(_manifest(0, created=created)))
    assert snapshots.list_snapshots() == []


def test_timestamp_conversion_failure_does_not_escape_inventory(night, monkeypatch):
    _plant(night, json.dumps(_manifest(0)))

    def unavailable():
        raise OverflowError("unrepresentable platform timestamp")

    monkeypatch.setattr(
        snapshots, "datetime", SimpleNamespace(fromisoformat=lambda _v: SimpleNamespace(timestamp=unavailable))
    )
    assert snapshots.list_snapshots() == []


@pytest.mark.parametrize(
    "content",
    ["[" * 20000 + "0" + "]" * 20000, "{broken", "null", "[]", "{}"],
    ids=["deep", "syntax", "null", "list", "empty"],
)
def test_malformed_or_deep_descriptions_are_snapshot_errors_and_skip_inventory(night, content):
    folder = _plant(night, content)
    with pytest.raises(snapshots.SnapshotError):
        snapshots.verify_snapshot(folder)
    assert snapshots.list_snapshots() == []


def test_manifest_read_is_bounded_before_parsing_or_signature_check(night, monkeypatch):
    folder = _plant(night, "{}")
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_BYTES", 32)
    reads = []
    real_open = Path.open

    class Oversized(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    def opened(path, mode="r", *args, **kwargs):
        if path == folder / "MANIFEST.json" and mode == "rb":
            return Oversized(b" " * 128)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(
        snapshots, "_signature_matches", lambda *_a: pytest.fail("oversized manifest reached signature check")
    )
    with pytest.raises(snapshots.SnapshotError, match=re.escape(t("backup.snapshot.manifest_too_large"))):
        snapshots.verify_snapshot(folder)
    assert reads == [33]
    assert snapshots.list_snapshots() == []
    assert reads == [33, 33]


def test_manifest_read_accepts_the_exact_byte_limit(night, monkeypatch):
    manifest = _manifest(0)
    content = json.dumps(manifest).encode("utf-8")
    folder = _plant(night, content.decode("utf-8"))
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_BYTES", len(content))
    assert snapshots._read_manifest(folder) == manifest
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_BYTES", len(content) - 1)
    with pytest.raises(snapshots.SnapshotError, match=re.escape(t("backup.snapshot.manifest_too_large"))):
        snapshots._read_manifest(folder)


def test_extreme_declared_member_size_is_refused_before_reading(night, monkeypatch):
    size = snapshots.MAX_MEMBER_SIZE
    folder = _plant(night, json.dumps(_manifest(size, format_value=snapshots.UNSIGNED_FORMAT)))
    source = folder / "state.json"
    source.write_bytes(b"{}")
    real_open = Path.open
    reads = []

    class Unreadable:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, amount=-1):
            reads.append(amount)
            pytest.fail("an impossible declared size reached a read allocation")

    def opened(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        return Unreadable(handle) if path == source and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(snapshots.SnapshotError, match=re.escape(t("backup.snapshot.file_damaged", name="state.json"))):
        snapshots.verify_snapshot(folder)
    assert not reads


@pytest.mark.parametrize("size", [0, 10, snapshots.MAX_MEMBER_SIZE])
def test_manifest_output_matches_the_existing_json_format_and_exact_byte_boundary(size, monkeypatch):
    manifest = _manifest(size)
    manifest["version"] = "проба"
    expected = json.dumps(manifest, indent=2).encode("utf-8")
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_BYTES", len(expected))
    assert snapshots._manifest_bytes(manifest) == expected
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_BYTES", len(expected) - 1)
    with pytest.raises(snapshots.SnapshotError, match=re.escape(t("backup.snapshot.manifest_too_large"))):
        snapshots._manifest_bytes(manifest)


def test_oversized_new_description_is_never_published_or_used_to_prune_older_copies(night, monkeypatch):
    old = Path(snapshots.create_snapshot()["snapshot"])
    prior = {p.relative_to(old): p.read_bytes() for p in old.rglob("*") if p.is_file()}
    monkeypatch.setattr(snapshots, "MAX_MANIFEST_BYTES", 8)
    with pytest.raises(snapshots.SnapshotError, match=re.escape(t("backup.snapshot.manifest_too_large"))):
        snapshots.create_snapshot()
    assert list(night.iterdir()) == [old]
    assert {p.relative_to(old): p.read_bytes() for p in old.rglob("*") if p.is_file()} == prior
    assert data_dir().is_dir()


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 10**400])
def test_unrepresentable_byte_counts_display_unknown_instead_of_raising(value):
    assert format_bytes(value) == "—"


@pytest.mark.parametrize("value", [1023, 1024, 1024**2, 1024**3, 1024**4, 1536 * 1024**2])
def test_ordinary_byte_counts_keep_their_units(value):
    assert format_bytes(value) != "—"
