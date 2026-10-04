"""YAML byte detection is independent of parsing and preserves presentation."""

from __future__ import annotations

import codecs

import pytest
import yaml

from tow import yaml_guard as guard

ENCODINGS = [
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
LOADERS = [yaml.SafeLoader]
if hasattr(yaml, "CSafeLoader"):
    LOADERS.append(yaml.CSafeLoader)


@pytest.mark.parametrize(("codec", "bom"), ENCODINGS)
@pytest.mark.parametrize("loader", LOADERS)
def test_normalization_preserves_text_and_safe_merge_semantics(codec, bom, loader):
    text = "# 保留 комментарий\r\na: &a {x: '例😀\ufeff', y: 1}\r\nb: {<<: *a, y: 2}\r\n"
    source = bom + text.encode(codec)
    result = guard.normalize_utf8(source)
    assert result == (source if codec == "utf-8" else text.encode("utf-8"))
    assert guard.load(result, loader=loader) == {"a": {"x": "例😀\ufeff", "y": 1}, "b": {"x": "例😀\ufeff", "y": 2}}


@pytest.mark.parametrize(("codec", "bom"), ENCODINGS)
def test_normalization_never_replaces_invalid_unicode(codec, bom):
    # Unpaired surrogates are forbidden in all Unicode encodings, not repaired.
    source = bom + "a: '\ud800'\n".encode(codec, errors="surrogatepass")
    with pytest.raises(UnicodeError):
        guard.normalize_utf8(source)


@pytest.mark.parametrize(("codec", "bom"), ENCODINGS)
def test_original_bytes_are_bounded_before_decode(codec, bom, monkeypatch):
    source = bom + ("#" + "x" * 32).encode(codec)
    monkeypatch.setattr(guard, "MAX_INPUT_BYTES", len(source) - 1)
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.normalize_utf8(source)
    assert caught.value.code == "yaml_limits.size"


@pytest.mark.parametrize(("codec", "bom"), ENCODINGS)
def test_both_byte_limits_are_inclusive(codec, bom, monkeypatch):
    text = "a: 例例例例例例例例例例\n"
    source = bom + text.encode(codec)
    expected = source if codec == "utf-8" else text.encode("utf-8")
    limit = max(len(source), len(expected))
    monkeypatch.setattr(guard, "MAX_INPUT_BYTES", limit)
    assert guard.normalize_utf8(source) == expected
    monkeypatch.setattr(guard, "MAX_INPUT_BYTES", limit - 1)
    with pytest.raises(guard.YamlLimitError):
        guard.normalize_utf8(source)


@pytest.mark.parametrize(("codec", "bom"), ENCODINGS[2:])
def test_wide_encoding_cannot_bypass_alias_preflight(codec, bom, monkeypatch):
    source = bom + "a: &loop {<<: *loop}\n".encode(codec)
    monkeypatch.setattr(yaml, "load", lambda *a, **kw: pytest.fail("unsafe graph constructed"))
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.load(guard.normalize_utf8(source))
    assert caught.value.code == "yaml_limits.references"


@pytest.mark.parametrize(
    "source", [b"", b"a", b"{}", b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"]
)
def test_empty_bom_only_and_short_streams(source):
    result = guard.normalize_utf8(source)
    assert result == (source if source in (b"", b"a", b"{}", b"\xef\xbb\xbf") else b"")


@pytest.mark.parametrize(
    "source",
    [
        b"\xef\xbb",
        b"\xff",
        b"\xff\xfea",
        b"\xfe\xff\x00",
        b"\xff\xfe\x00\x00a",
        b"\x00\x00\xfe\xffa",
        b"\xff\xfe\x00\x00\x00\x00\x11\x00",
    ],
)
def test_truncated_and_out_of_range_streams_are_refused(source):
    with pytest.raises(UnicodeError):
        guard.normalize_utf8(source)
