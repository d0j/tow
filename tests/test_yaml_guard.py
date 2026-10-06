"""Small synthetic graphs exercise resource limits without constructing a bomb."""

from __future__ import annotations

import ast
import datetime as dt
import random
from pathlib import Path

import pytest
import yaml

from tow import yaml_guard as guard

LOADERS = [yaml.SafeLoader]
if hasattr(yaml, "CSafeLoader"):
    LOADERS.append(yaml.CSafeLoader)


@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize(
    "source",
    [
        "",
        "# empty\n",
        "null",
        "a: 12\nb: true\nc: текст\n",
        "a: &a [1, 2]\nb: *a\nc: *a\n",
        "a: &a {x: 1, y: 2}\nb: {<<: *a, y: 3}\n",
        "a: &a {x: 1}\nb: &b {x: 2, y: 3}\nc: {<<: [*a, *b]}\n",
        "a: {'<<': literal}\n",
        "a: !!set {one: null, two: null}\n",
        "a: !!omap [{one: 1}, {two: 2}]\n",
        "a: !!binary aGVsbG8=\n",
        "a: 2026-10-04\nb: 2026-10-04T10:20:30Z\n",
    ],
)
def test_safe_yaml_semantics_are_preserved(loader, source):
    assert guard.load(source, loader=loader) == yaml.load(source, Loader=loader)


@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize(
    ("source", "code"),
    [
        ("a: &loop [*loop]", "references"),
        ("a: &loop {child: *loop}", "references"),
        ("a: &loop {<<: *loop}", "references"),
        ("a: *later\nb: &later []", "references"),
        ("a: &same 1\nb: &same 2", "references"),
        ("---\na: 1\n---\na: 2", "documents"),
    ],
)
def test_unsafe_references_and_multiple_documents_are_refused(loader, source, code):
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.load(source, loader=loader)
    assert caught.value.code == "yaml_limits." + code
    assert isinstance(caught.value, yaml.YAMLError)
    assert caught.value.params == {}


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("t: 3:30", "3:30"),  # base 60 in YAML 1.1 only: text, as in YAML 1.2
        ("t: -1:20:30.5", "-1:20:30.5"),
        pytest.param("t: 1" + ":59" * 64_000, "1" + ":59" * 64_000, id="long-base-60"),
        ("t: 1_000", 1000),
        ("t: 0x1F", 31),
        ("t: 0b101", 5),
        ("t: 017", 15),
        ("t: -1.5e+3", -1500.0),
        ("t: .inf", float("inf")),
        ("t: '3:30'", "3:30"),
    ],
)
def test_numbers_are_read_without_base_60(source, value):
    assert guard.load(source)["t"] == value


@pytest.mark.parametrize("source", [pytest.param("t: " + "9" * 5000, id="huge-int"), "t: 2026-13-45"])
def test_an_unreadable_number_or_date_is_a_yaml_refusal(source):
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.load(source)
    assert caught.value.code == "yaml_limits.number"


def _doubling(*, merge: bool) -> str:
    lines = ["a0: &a0 {value: x}" if merge else "a0: &a0 [x]"]
    for number in range(1, 12):
        previous = f"*a{number - 1}, *a{number - 1}"
        body = "{<<: [" + previous + "]}" if merge else "[" + previous + "]"
        lines.append(f"a{number}: &a{number} {body}")
    return "\n".join(lines)


@pytest.mark.parametrize("loader", LOADERS)
@pytest.mark.parametrize("merge", [False, True])
def test_expansion_is_refused_before_constructor_or_merge_runs(monkeypatch, loader, merge):
    monkeypatch.setattr(guard, "MAX_EXPANDED_NODES", 256)
    monkeypatch.setattr(yaml, "load", lambda *args, **kwargs: pytest.fail("construction preceded limit check"))
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.load(_doubling(merge=merge), loader=loader)
    assert caught.value.code == "yaml_limits.nodes"


@pytest.mark.parametrize("loader", LOADERS)
def test_scalar_alias_text_is_counted_per_expanded_occurrence(monkeypatch, loader):
    monkeypatch.setattr(guard, "MAX_EXPANDED_TEXT", 10)
    assert guard.load("[&a hello, *a]", loader=loader) == ["hello", "hello"]
    monkeypatch.setattr(yaml, "load", lambda *args, **kwargs: pytest.fail("over-budget text was constructed"))
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.load("[&a hello, *a, *a]", loader=loader)
    assert caught.value.code == "yaml_limits.text"


@pytest.mark.parametrize("loader", LOADERS)
def test_keys_and_values_count_toward_the_inclusive_node_budget(monkeypatch, loader):
    monkeypatch.setattr(guard, "MAX_EXPANDED_NODES", 3)
    assert guard.load("{a: b}", loader=loader) == {"a": "b"}
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.load("{a: b, c: d}", loader=loader)
    assert caught.value.code == "yaml_limits.nodes"


@pytest.mark.parametrize("loader", LOADERS)
def test_inclusive_depth_limit_matches_graph_depth(monkeypatch, loader):
    monkeypatch.setattr(guard, "MAX_DEPTH", 4)
    assert guard.load("[[[[x]]]]", loader=loader) == [[[["x"]]]]
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.load("[[[[[x]]]]]", loader=loader)
    assert caught.value.code == "yaml_limits.depth"


@pytest.mark.parametrize("loader", LOADERS)
def test_memoized_anchor_height_is_checked_when_reused_deeper(monkeypatch, loader):
    monkeypatch.setattr(guard, "MAX_DEPTH", 4)
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.load("[&a [[x]], [[*a]]]", loader=loader)
    assert caught.value.code == "yaml_limits.depth"


@pytest.mark.parametrize(
    ("source", "allowed"),
    [(b"12345678", True), ("12345678", True), ("яяяя", True), ("яяяяя", False), (b"123456789", False)],
)
def test_input_size_is_utf8_bytes_not_character_count(monkeypatch, source, allowed):
    monkeypatch.setattr(guard, "MAX_INPUT_BYTES", 8)
    if allowed:
        guard.load(source)
    else:
        with pytest.raises(guard.YamlLimitError) as caught:
            guard.load(source)
        assert caught.value.code == "yaml_limits.size"


def test_file_read_is_bounded_and_closed_when_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "MAX_INPUT_BYTES", 8)
    source = tmp_path / "config.yaml"
    source.write_bytes(b"x" * 20)
    real_open = Path.open
    requests = []
    handles = []

    class Reader:
        def __enter__(self):
            handles.append(real_open(source, "rb"))
            return self

        def read(self, size):
            requests.append(size)
            return handles[-1].read(size)

        def __exit__(self, *args):
            handles[-1].close()

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: Reader())
    with pytest.raises(guard.YamlLimitError):
        guard.read_text(source)
    assert requests == [9]
    assert handles[0].closed


def test_invalid_utf8_is_not_silently_replaced(tmp_path):
    source = tmp_path / "config.yaml"
    source.write_bytes(b"key: \xff")
    with pytest.raises(UnicodeError):
        guard.read_text(source)


@pytest.mark.parametrize("kind", ["list", "dict", "tuple"])
def test_programmatic_cycles_are_refused_without_recursion(kind):
    items = []
    cycle = items if kind == "list" else {"child": items} if kind == "dict" else (items,)
    items.append(cycle)
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.validate_graph(cycle)
    assert caught.value.code == "yaml_limits.references"


def test_programmatic_shared_graph_cost_counts_expansion(monkeypatch):
    monkeypatch.setattr(guard, "MAX_EXPANDED_NODES", 100)
    value = ["x"]
    for _ in range(8):
        value = [value, value]
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.validate_graph(value)
    assert caught.value.code == "yaml_limits.nodes"


def test_programmatic_shared_height_is_not_hidden_by_memoization(monkeypatch):
    monkeypatch.setattr(guard, "MAX_DEPTH", 4)
    anchor = [["x"]]
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.validate_graph([anchor, [[anchor]]])
    assert caught.value.code == "yaml_limits.depth"


def test_programmatic_scalar_text_and_dictionary_keys_count(monkeypatch):
    monkeypatch.setattr(guard, "MAX_EXPANDED_TEXT", 5)
    guard.validate_graph({"ab": b"xyz"})
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.validate_graph({"abc": b"xyz"})
    assert caught.value.code == "yaml_limits.text"
    guard.validate_graph(dt.date(2026, 10, 4))


def test_constructor_output_is_checked_again(monkeypatch):
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!test", lambda loader, node: ["expanded"] * 10)
    monkeypatch.setattr(guard, "MAX_EXPANDED_NODES", 5)
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.load("!test short", loader=Loader)
    assert caught.value.code == "yaml_limits.nodes"


@pytest.mark.parametrize("loader", LOADERS)
def test_unsafe_python_tags_remain_forbidden(loader):
    with pytest.raises(yaml.YAMLError):
        guard.load("!!python/object:builtins.object {}", loader=loader)


def test_parser_is_disposed_when_preflight_stops(monkeypatch):
    disposed = []

    class Loader(yaml.SafeLoader):
        def dispose(self):
            disposed.append(True)
            super().dispose()

    monkeypatch.setattr(guard, "MAX_EXPANDED_NODES", 1)
    with pytest.raises(guard.YamlLimitError):
        guard.load("[x]", loader=Loader)
    assert disposed == [True]


def _oracle(value):
    if isinstance(value, dict):
        children = [child for pair in value.items() for child in pair]
    elif isinstance(value, (list, tuple, set)):
        children = list(value)
    else:
        return 1, len(value) if isinstance(value, (str, bytes)) else 0, 0
    costs = [_oracle(child) for child in children]
    return 1 + sum(x[0] for x in costs), sum(x[1] for x in costs), max((x[2] + 1 for x in costs), default=0)


def test_random_acyclic_graphs_match_an_independent_expansion_oracle(monkeypatch):
    rng = random.Random(435982)
    for _ in range(100):
        pool = [None, "text", 12, b"bytes", dt.date(2026, 10, 4)]
        for _ in range(7):
            children = rng.choices(pool, k=rng.randrange(4))
            pool.append(children if rng.randrange(2) else {str(index): child for index, child in enumerate(children)})
        value = pool[-1]
        nodes, text, height = _oracle(value)
        monkeypatch.setattr(guard, "MAX_EXPANDED_NODES", nodes)
        monkeypatch.setattr(guard, "MAX_EXPANDED_TEXT", text)
        monkeypatch.setattr(guard, "MAX_DEPTH", height)
        guard.validate_graph(value)
        for name, cost, code in [
            ("MAX_EXPANDED_NODES", nodes, "nodes"),
            ("MAX_EXPANDED_TEXT", text, "text"),
            ("MAX_DEPTH", height, "depth"),
        ]:
            if cost:
                monkeypatch.setattr(guard, name, cost - 1)
                with pytest.raises(guard.YamlLimitError) as caught:
                    guard.validate_graph(value)
                assert caught.value.code == "yaml_limits." + code
                monkeypatch.setattr(guard, name, cost)


def test_all_production_yaml_construction_goes_through_the_guard():
    root = Path(__file__).resolve().parents[1] / "src" / "tow"
    calls = []
    for source in root.rglob("*.py"):
        calls.extend(
            (source.name, node.func.attr)
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "yaml"
            and node.func.attr in {"load", "safe_load", "full_load", "unsafe_load", "dump", "safe_dump"}
        )
    # CLI renders to stdout; it does not persist a live configuration.
    assert sorted(calls) == [("cli.py", "safe_dump"), ("yaml_guard.py", "load"), ("yaml_guard.py", "safe_dump")]


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"a": "текст"},
        {"a": "line\nnext"},
        {"a": b"bytes"},
        {"a": dt.date(2026, 10, 4)},
        {"a": [1, True, None]},
        {"a": "\x00"},
    ],
)
def test_guarded_dump_preserves_safe_dump_contract(value):
    expected = yaml.safe_dump(value, allow_unicode=True, sort_keys=False, default_flow_style=False)
    assert guard.dump(value) == expected
    assert guard.load(guard.dump(value)) == value


def test_output_limit_counts_utf8_bytes_and_prefix_inclusively(monkeypatch):
    monkeypatch.setattr(guard, "MAX_INPUT_BYTES", 5)
    assert guard.dump({}, prefix="#\n") == "#\n{}\n"
    monkeypatch.setattr(guard, "MAX_INPUT_BYTES", 4)
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.dump({}, prefix="#\n")
    assert caught.value.code == "yaml_limits.size"


def test_sink_does_not_retain_over_budget_output(monkeypatch):
    monkeypatch.setattr(guard, "MAX_INPUT_BYTES", 4)
    with guard._Utf8Sink() as stream:
        assert stream.write("яя") == 2
        with pytest.raises(guard.YamlLimitError):
            stream.write("x")
        assert stream.getvalue() == "яя"
        assert stream._size == 4
    assert stream.closed


def test_escaped_scalar_output_is_bounded_as_well_as_plain_text(monkeypatch):
    monkeypatch.setattr(guard, "MAX_INPUT_BYTES", 64)
    with pytest.raises(guard.YamlLimitError):
        guard.dump({"a": "\x00" * 40})


def test_dump_refuses_a_cycle_before_serialization(monkeypatch):
    monkeypatch.setattr(yaml, "safe_dump", lambda *args, **kwargs: pytest.fail("cycle reached serialization"))
    cycle = []
    cycle.append(cycle)
    with pytest.raises(guard.YamlLimitError) as caught:
        guard.dump(cycle)
    assert caught.value.code == "yaml_limits.references"
