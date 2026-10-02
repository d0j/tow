import hashlib

import pytest
from helpers import bencode, make_torrent

from tow.torrent import (
    display_name,
    infohash_v1,
    infohash_v2,
    is_download_limit,
    looks_like_torrent,
    parse_torrent_metadata,
    windows_path_key,
)


def v1_info(name=b"Show", *, files=None):
    info = {b"name": name, b"piece length": 16384, b"pieces": b"x" * 20}
    if files is None:
        info[b"length"] = 1
    else:
        info[b"files"] = files
    return info


def test_infohash_uses_exact_top_level_info_not_marker_in_comment():
    info = v1_info(b"real")
    blob = make_torrent(info, comment=b"fake 4:infod4:name4:evilee")
    assert looks_like_torrent(blob)
    assert infohash_v1(blob) == hashlib.sha1(bencode(info)).hexdigest().upper()


def test_display_name_prefers_utf8():
    info = v1_info(b"Hello")
    info[b"name.utf-8"] = "Побег".encode()
    assert display_name(make_torrent(info, announce=b"x")) == "Побег"


def test_multifile_metadata_and_padding():
    blob = make_torrent(
        v1_info(
            files=[
                {b"length": 10, b"path": [b"S01E01.mkv"]},
                {b"attr": b"p", b"length": 2, b"path": [b".pad", b"2"]},
            ]
        )
    )
    metadata = parse_torrent_metadata(blob)
    assert metadata.is_multi is True
    assert [(row.index, row.path, row.is_pad) for row in metadata.files] == [
        (0, "S01E01.mkv", False),
        (1, ".pad/2", True),
    ]


def test_pure_v2_hash_and_file_tree():
    info = {
        b"file tree": {b"S01E01.mkv": {b"": {b"length": 10, b"pieces root": b"r" * 32}}},
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
    }
    blob = make_torrent(info)
    metadata = parse_torrent_metadata(blob)
    assert len(metadata.infohash) == 64
    assert metadata.infohash == hashlib.sha256(bencode(info)).hexdigest().upper()
    assert metadata.client_hash == metadata.infohash[:40]
    assert metadata.hash_v1 is None
    assert metadata.hash_v2 == infohash_v2(blob)
    assert metadata.files[0].path == "S01E01.mkv"


def test_hybrid_uses_truncated_v2_as_qbit_client_id_but_retains_both_hashes():
    info = {
        b"file tree": {b"S01E01.mkv": {b"": {b"length": 10, b"pieces root": b"r" * 32}}},
        b"files": [{b"length": 10, b"path": [b"S01E01.mkv"]}],
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
        b"pieces": b"x" * 20,
    }
    blob = make_torrent(info)

    metadata = parse_torrent_metadata(blob)

    assert metadata.hash_v1 == hashlib.sha1(bencode(info)).hexdigest().upper()
    assert metadata.hash_v2 == hashlib.sha256(bencode(info)).hexdigest().upper()
    assert metadata.client_hash == metadata.hash_v2[:40]
    assert infohash_v1(blob) == metadata.hash_v1


def test_v1_piece_count_must_match_content_size():
    info = v1_info()
    info[b"length"] = 32768
    with pytest.raises(ValueError, match="piece count"):
        parse_torrent_metadata(make_torrent(info))


@pytest.mark.parametrize("piece_length", [0, 3, 8192])
def test_v2_piece_length_must_be_supported_power_of_two(piece_length):
    info = {
        b"file tree": {b"empty": {b"": {b"length": 0}}},
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": piece_length,
    }
    with pytest.raises(ValueError, match="piece length"):
        parse_torrent_metadata(make_torrent(info))


def test_v2_nonempty_file_requires_piece_root_and_large_file_requires_layer():
    missing_root = {
        b"file tree": {b"episode.mkv": {b"": {b"length": 1}}},
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
    }
    with pytest.raises(ValueError, match="pieces root"):
        parse_torrent_metadata(make_torrent(missing_root))

    missing_layer = {
        b"file tree": {b"episode.mkv": {b"": {b"length": 16385, b"pieces root": b"r" * 32}}},
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
    }
    with pytest.raises(ValueError, match="piece layer"):
        parse_torrent_metadata(make_torrent(missing_layer))


def test_v2_piece_layer_must_reconstruct_declared_root():
    first = b"a" * 32
    second = b"b" * 32
    root_hash = hashlib.sha256(first + second).digest()
    info = {
        b"file tree": {b"episode.mkv": {b"": {b"length": 32768, b"pieces root": root_hash}}},
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
    }
    parse_torrent_metadata(make_torrent(info, **{"piece layers": {root_hash: first + second}}))

    with pytest.raises(ValueError, match="does not match"):
        parse_torrent_metadata(make_torrent(info, **{"piece layers": {root_hash: first + b"c" * 32}}))


def test_padding_requires_bep47_attr_and_pathless_padding_is_supported():
    named_like_padding = parse_torrent_metadata(make_torrent(v1_info(files=[{b"length": 1, b"path": [b".pad", b"1"]}])))
    assert named_like_padding.files[0].is_pad is False

    pathless = parse_torrent_metadata(
        make_torrent(
            v1_info(
                files=[
                    {b"length": 1, b"path": [b"episode.mkv"]},
                    {b"attr": b"p", b"length": 2},
                ]
            )
        )
    )
    assert pathless.files[1].is_pad is True


def test_single_file_symlink_is_rejected():
    info = v1_info()
    info[b"attr"] = b"l"
    info[b"symlink path"] = [b"target"]
    with pytest.raises(ValueError, match="symlink"):
        parse_torrent_metadata(make_torrent(info))


def test_v2_with_incomplete_v1_fields_is_not_misclassified_as_pure_v2():
    info = {
        b"file tree": {b"empty": {b"": {b"length": 0}}},
        b"files": [{b"length": 0, b"path": [b"empty"]}],
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
    }
    with pytest.raises(ValueError, match="incomplete v1"):
        parse_torrent_metadata(make_torrent(info))


def test_v2_symlink_attr_is_rejected_even_without_target_key():
    info = {
        b"file tree": {
            b"link": {
                b"": {
                    b"attr": b"lx",
                    b"length": 1,
                    b"pieces root": b"r" * 32,
                }
            }
        },
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
    }
    with pytest.raises(ValueError, match="symlink"):
        parse_torrent_metadata(make_torrent(info))


def test_hybrid_multifile_requires_v1_padding_alignment():
    info = {
        b"file tree": {
            b"a.mkv": {b"": {b"length": 10, b"pieces root": b"a" * 32}},
            b"b.mkv": {b"": {b"length": 20, b"pieces root": b"b" * 32}},
        },
        b"files": [
            {b"length": 10, b"path": [b"a.mkv"]},
            {b"length": 20, b"path": [b"b.mkv"]},
        ],
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
        b"pieces": b"x" * 20,
    }
    with pytest.raises(ValueError, match="padding alignment"):
        parse_torrent_metadata(make_torrent(info))


@pytest.mark.parametrize(
    "path",
    [[b"..", b"escape.mkv"], [b"..."], [b" "], [b"bad\\name"], [b"ctl\x01.mkv"]],
)
def test_unsafe_paths_fail_closed(path):
    blob = make_torrent(v1_info(files=[{b"length": 1, b"path": path}]))
    with pytest.raises(ValueError, match="unsafe"):
        parse_torrent_metadata(blob)


@pytest.mark.parametrize(
    "name",
    [b"Space Show: Part A", b"Vol.", b"What? Why*", b'Say "hi" <ok> |x|', b"CON"],
)
def test_windows_illegal_names_are_accepted(name):
    blob = make_torrent(v1_info(name, files=[{b"length": 1, b"path": [b"E01: Pilot.mkv"]}]))
    assert looks_like_torrent(blob)
    metadata = parse_torrent_metadata(blob)
    assert metadata.name == name.decode()
    assert metadata.files[0].path == "E01: Pilot.mkv"


def test_windows_path_key_matches_client_sanitized_spelling():
    assert windows_path_key("Show: S1/E01: Pilot?.mkv") == windows_path_key("Show_ S1/E01_ Pilot_.mkv")
    assert windows_path_key("Vol./a.mkv") == windows_path_key("Vol/a.mkv")
    assert windows_path_key("CON") == "con_"


def test_windows_colliding_paths_fail_closed():
    blob = make_torrent(v1_info(files=[{b"length": 1, b"path": [b"a:b.mkv"]}, {b"length": 1, b"path": [b"a_b.mkv"]}]))
    with pytest.raises(ValueError, match="duplicate"):
        parse_torrent_metadata(blob)


def test_policy_violation_is_still_a_torrent_not_a_login_page():
    blob = make_torrent(v1_info(files=[{b"length": 1, b"path": [b"..", b"escape.mkv"]}]))
    assert looks_like_torrent(blob)
    assert not looks_like_torrent(b"<html>login</html>")
    assert not looks_like_torrent(bencode({b"announce": b"x"}))
    with pytest.raises(ValueError, match="unsafe"):
        parse_torrent_metadata(blob)


def test_long_root_name_is_not_truncated():
    root = b"A" * 208
    blob = make_torrent(v1_info(root, files=[{b"length": 1, b"path": [b"a.mkv"]}]))
    assert parse_torrent_metadata(blob).name == root.decode()


def test_duplicate_casefold_paths_fail_closed():
    blob = make_torrent(
        v1_info(
            files=[
                {b"length": 1, b"path": [b"Episode.mkv"]},
                {b"length": 1, b"path": [b"episode.mkv"]},
            ]
        )
    )
    with pytest.raises(ValueError, match="duplicate"):
        parse_torrent_metadata(blob)


def test_repeated_padding_paths_do_not_collide():
    blob = make_torrent(
        v1_info(
            files=[
                {b"length": 1, b"path": [b"a.mkv"]},
                {b"attr": b"p", b"length": 2, b"path": [b".pad", b"2"]},
                {b"length": 1, b"path": [b"b.mkv"]},
                {b"attr": b"p", b"length": 2, b"path": [b".pad", b"2"]},
            ]
        )
    )
    metadata = parse_torrent_metadata(blob)
    assert len(metadata.files) == 4
    assert sum(row.is_pad for row in metadata.files) == 2


@pytest.mark.parametrize(
    "blob",
    [
        b"<html>cf</html>",
        b"d4:infod4:name1:aee",
        b"d4:infoi1ee",
        b"d4:infod4:name1:aeejunk",
        b"d4:infod4:name1:a4:name1:bee",
    ],
)
def test_malformed_or_incomplete_torrent_is_rejected(blob):
    assert not looks_like_torrent(blob)


def test_download_limit():
    html = "Вам недоступен торрент-файл для скачивания. Вы скачали сегодня ( 20 ).".encode("cp1251")
    assert is_download_limit(html)
    assert not is_download_limit(make_torrent(v1_info(b"a")))


@pytest.mark.parametrize("piece_length", [8192, 24576])
def test_hybrid_piece_length_must_satisfy_bep52(piece_length):
    info = {
        b"file tree": {b"S01E01.mkv": {b"": {b"length": 10, b"pieces root": b"r" * 32}}},
        b"files": [{b"length": 10, b"path": [b"S01E01.mkv"]}],
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": piece_length,
        b"pieces": b"x" * 20,
    }
    with pytest.raises(ValueError, match="piece length"):
        parse_torrent_metadata(make_torrent(info))


def _fuzz_seed_torrent() -> bytes:
    """A valid multi-file v1 torrent with padding, a nested path and a utf-8 name."""
    files = [
        {b"length": 20_000, b"path": [b"Season 1", b"S01E01.mkv"]},
        {b"attr": b"p", b"length": 12_768, b"path": [b".pad", b"12768"]},
        {b"length": 5, b"path": [b"S01E02.srt"], b"path.utf-8": ["Серия 2.srt".encode()]},
    ]
    info = {b"name": b"Show", b"name.utf-8": "Шоу".encode(), b"piece length": 16384, b"pieces": b"x" * 60}
    info[b"files"] = files
    blob = make_torrent(info, announce=b"http://tracker.example/announce", comment=b"fuzz seed")
    assert len(parse_torrent_metadata(blob).files) == 3
    return blob


def _random_value(rng):
    return rng.choice([0, -1, 2**70, b"", b"x" * rng.randrange(1, 40), [], [b"a"], {}, {b"a": 1}, {b"": {}}])


def _mutate_structure(value, rng, depth=0):
    """Replace one randomly chosen node of the decoded torrent with a value of another type."""
    if depth and rng.random() < 0.25:
        return _random_value(rng)
    if isinstance(value, dict) and value:
        key = rng.choice(sorted(value))
        return {**value, key: _mutate_structure(value[key], rng, depth + 1)}
    if isinstance(value, list) and value:
        index = rng.randrange(len(value))
        return [*value[:index], _mutate_structure(value[index], rng, depth + 1), *value[index + 1 :]]
    return _random_value(rng)


def _mutate_bytes(blob: bytes, rng) -> bytes:
    """One to four random byte edits: overwrite, bencode-token swap, delete, insert, truncate, repeat."""
    data = bytearray(blob)
    for _ in range(rng.randint(1, 4)):
        if not data:
            break
        pos = rng.randrange(len(data))
        op = rng.randrange(6)
        if op == 0:
            data[pos] = rng.randrange(256)
        elif op == 1:
            data[pos] = rng.choice(b"dlie:0123456789-")
        elif op == 2:
            del data[pos : pos + rng.randint(1, 8)]
        elif op == 3:
            data[pos:pos] = bytes(rng.choice(b"dlie:0123456789") for _ in range(rng.randint(1, 4)))
        elif op == 4:
            del data[pos:]
        else:
            data[pos:pos] = data[pos : pos + rng.randint(1, 32)]
    return bytes(data)


def test_mutation_fuzz_only_ever_raises_value_error():
    """Malformed input is a ValueError ("not a valid torrent"), never TypeError & co.,
    so display_name and every caller that catches ValueError stay safe. Deterministic:
    a fixed seed, half byte-level and half structure-level mutations."""
    import random

    from tow.torrent import _Decoder

    seed = _fuzz_seed_torrent()
    decoded, _end = _Decoder(seed).parse()
    rng = random.Random(20261001)
    outcomes = {"parsed": 0, "rejected": 0}
    for round_ in range(4000):
        blob = _mutate_bytes(seed, rng) if round_ % 2 else bencode(_mutate_structure(decoded, rng))
        try:
            parse_torrent_metadata(blob)
        except ValueError:
            outcomes["rejected"] += 1
        except Exception as exc:  # noqa: BLE001 - any other exception is the bug this fuzz test looks for
            pytest.fail(f"round {round_}: {type(exc).__name__}: {exc} for {blob[:120]!r}")
        else:
            outcomes["parsed"] += 1
        assert isinstance(display_name(blob), str)
        assert isinstance(looks_like_torrent(blob), bool)
    assert outcomes["rejected"] > 1000  # the mutations really break the input
    assert outcomes["parsed"] > 50  # and some still decode (e.g. a changed comment)
