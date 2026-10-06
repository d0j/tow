"""Bound YAML before construction/merge and bound graphs before later traversals."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from io import StringIO
from itertools import chain
from pathlib import Path
from typing import Any

import yaml

from tow.errors import TowError

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_NODES = 100_000
MAX_EXPANDED_TEXT = 16 * 1024 * 1024
MAX_DEPTH = 32
_INT, _FLOAT = "tag:yaml.org,2002:int", "tag:yaml.org,2002:float"
_BASE_LOADER: type[Any] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
# The safe loader without YAML 1.1 base-60 numbers (as in YAML 1.2): ``3:30`` stays text, and a
# long ``1:2:3:...`` scalar cannot cost quadratic time to construct. Other resolvers are unchanged.
SAFE_LOADER: type[Any] = type(
    "SafeLoader",
    (_BASE_LOADER,),
    {
        "yaml_implicit_resolvers": {
            first: [(tag, regexp) for tag, regexp in resolvers if tag not in (_INT, _FLOAT)]
            for first, resolvers in _BASE_LOADER.yaml_implicit_resolvers.items()
        }
    },
)
SAFE_LOADER.add_implicit_resolver(
    _INT,
    re.compile(
        r"""^(?:[-+]?0b[0-1_]+
        |[-+]?0[0-7_]+
        |[-+]?(?:0|[1-9][0-9_]*)
        |[-+]?0x[0-9a-fA-F_]+)$""",
        re.VERBOSE,
    ),
    list("-+0123456789"),
)
SAFE_LOADER.add_implicit_resolver(
    _FLOAT,
    re.compile(
        r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
        |\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
        |[-+]?\.(?:inf|Inf|INF)
        |\.(?:nan|NaN|NAN))$""",
        re.VERBOSE,
    ),
    list("-+0123456789."),
)


class YamlLimitError(TowError, yaml.YAMLError):
    """A safe, localized refusal, compatible with existing YAML error boundaries."""


@dataclass(frozen=True, slots=True)
class _Weight:
    nodes: int = 1
    text: int = 0
    height: int = 0

    def check(self, depth: int = 0) -> None:
        if self.nodes > MAX_EXPANDED_NODES:
            raise YamlLimitError("yaml_limits.nodes")
        if self.text > MAX_EXPANDED_TEXT:
            raise YamlLimitError("yaml_limits.text")
        if depth + self.height > MAX_DEPTH:
            raise YamlLimitError("yaml_limits.depth")

    def add(self, child: _Weight) -> _Weight:
        result = _Weight(self.nodes + child.nodes, self.text + child.text, max(self.height, child.height + 1))
        result.check()
        return result


@dataclass(slots=True)
class _Frame:
    anchor: str | None
    weight: _Weight


def _input_size(data: str | bytes) -> None:
    if len(data) > MAX_INPUT_BYTES or (isinstance(data, str) and len(data.encode("utf-8")) > MAX_INPUT_BYTES):
        raise YamlLimitError("yaml_limits.size")


def normalize_utf8(data: bytes) -> bytes:
    """Strict YAML 1.2 encoding detection; bound both source and UTF-8 output.

    UTF-32 must precede UTF-16 because their little-endian BOMs overlap.
    Decode text, not its YAML graph: comments, anchors, quoting and line breaks
    survive unchanged. Existing UTF-8 bytes (including a BOM) are retained.
    """
    _input_size(data)
    encoding, offset = "utf-8", 0
    if data.startswith(b"\x00\x00\xfe\xff"):
        encoding, offset = "utf-32-be", 4
    elif len(data) >= 4 and data[:3] == b"\x00\x00\x00":
        encoding = "utf-32-be"
    elif data.startswith(b"\xff\xfe\x00\x00"):
        encoding, offset = "utf-32-le", 4
    elif len(data) >= 4 and data[1:4] == b"\x00\x00\x00":
        encoding = "utf-32-le"
    elif data.startswith(b"\xfe\xff"):
        encoding, offset = "utf-16-be", 2
    elif len(data) >= 2 and data[0] == 0:
        encoding = "utf-16-be"
    elif data.startswith(b"\xff\xfe"):
        encoding, offset = "utf-16-le", 2
    elif len(data) >= 2 and data[1] == 0:
        encoding = "utf-16-le"
    text = data[offset:].decode(encoding)
    result = data if encoding == "utf-8" else text.encode("utf-8")
    _input_size(result)
    return result


def read_text(path: Path) -> str:
    """Read the existing UTF-8 config contract without allocating an unbounded file."""
    with path.open("rb") as handle:
        content = handle.read(MAX_INPUT_BYTES + 1)
    _input_size(content)
    return content.decode("utf-8")


def _preflight(data: str | bytes, loader: type[Any]) -> None:
    anchors: dict[str, _Weight | None] = {}
    stack: list[_Frame] = []
    documents = 0

    def append(weight: _Weight) -> None:
        weight.check(len(stack))
        if stack:
            stack[-1].weight = stack[-1].weight.add(weight)

    def register(anchor: str | None, weight: _Weight | None) -> None:
        if anchor is not None:
            if anchor in anchors:
                raise YamlLimitError("yaml_limits.references")
            anchors[anchor] = weight

    # Parsing emits aliases, not their targets; no Python object or merge list
    # is constructed here. Dispose even when a budget stops the event stream.
    with closing(yaml.parse(data, Loader=loader)) as events:
        for event in events:
            if isinstance(event, yaml.events.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise YamlLimitError("yaml_limits.documents")
            elif isinstance(event, yaml.events.AliasEvent):
                weight = anchors.get(event.anchor)
                if weight is None:
                    raise YamlLimitError("yaml_limits.references")
                append(weight)
            elif isinstance(event, yaml.events.ScalarEvent):
                weight = _Weight(text=len(event.value))
                register(event.anchor, weight)
                append(weight)
            elif isinstance(event, (yaml.events.SequenceStartEvent, yaml.events.MappingStartEvent)):
                _Weight().check(len(stack))
                register(event.anchor, None)
                stack.append(_Frame(event.anchor, _Weight()))
            elif isinstance(event, (yaml.events.SequenceEndEvent, yaml.events.MappingEndEvent)):
                frame = stack.pop()
                if frame.anchor is not None:
                    anchors[frame.anchor] = frame.weight
                append(frame.weight)


@dataclass(slots=True)
class _GraphFrame:
    identity: int
    children: Iterator[Any]
    weight: _Weight


def validate_graph(value: Any) -> None:
    """Count every expanded occurrence, but inspect each unique container once.

    Memoized height checks deeper reuse too. Active identities reject cycles;
    dictionary keys are counted as well as values. No recursive walk or strings
    containing a private YAML value are needed to compute the budgets.
    """
    memo: dict[int, _Weight] = {}
    active: set[int] = set()
    stack: list[_GraphFrame] = []
    weight: _Weight | None = None
    while True:
        if weight is None:
            if isinstance(value, (dict, list, tuple, set)):
                identity = id(value)
                if identity in active:
                    raise YamlLimitError("yaml_limits.references")
                weight = memo.get(identity)
                if weight is None:
                    _Weight().check(len(stack))
                    children = chain.from_iterable(value.items()) if isinstance(value, dict) else iter(value)
                    active.add(identity)
                    stack.append(_GraphFrame(identity, children, _Weight()))
                    try:
                        value = next(children)
                        continue
                    except StopIteration:
                        frame = stack.pop()
                        active.remove(identity)
                        weight = memo[identity] = frame.weight
            else:
                weight = _Weight(text=len(value) if isinstance(value, (str, bytes)) else 0)
        weight.check(len(stack))
        if not stack:
            return
        frame = stack[-1]
        frame.weight = frame.weight.add(weight)
        try:
            value = next(frame.children)
            weight = None
        except StopIteration:
            frame = stack.pop()
            active.remove(frame.identity)
            weight = memo[frame.identity] = frame.weight


def load(data: str | bytes, *, loader: type[Any] | None = None) -> Any:
    """Keep safe-loader semantics, with budgets checked before any construction."""
    _input_size(data)
    selected = loader or SAFE_LOADER
    _preflight(data, selected)
    try:
        value = yaml.load(data, Loader=selected)
    except (ValueError, OverflowError) as exc:  # an integer beyond Python's digit limit, an unrepresentable number
        raise YamlLimitError("yaml_limits.number") from exc
    validate_graph(value)
    return value


class _Utf8Sink(StringIO):
    """Retain at most the configured UTF-8 byte budget, including the header."""

    def __init__(self) -> None:
        super().__init__()
        self._size = 0

    def write(self, text: str) -> int:
        size = self._size + len(text.encode("utf-8"))
        if size > MAX_INPUT_BYTES:
            raise YamlLimitError("yaml_limits.size")
        result = super().write(text)
        self._size = size
        return result


def dump(value: Any, *, prefix: str = "") -> str:
    """Safe-loader-compatible text that never exceeds the reader's byte limit."""
    validate_graph(value)
    with _Utf8Sink() as stream:
        stream.write(prefix)
        yaml.safe_dump(value, stream=stream, allow_unicode=True, sort_keys=False, default_flow_style=False)
        return stream.getvalue()
