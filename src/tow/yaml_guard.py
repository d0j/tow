"""Bound YAML before construction/merge and bound graphs before later traversals."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any

import yaml

from tow.errors import TowError

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_NODES = 100_000
MAX_EXPANDED_TEXT = 16 * 1024 * 1024
MAX_DEPTH = 32
SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


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
    value = yaml.load(data, Loader=selected)
    validate_graph(value)
    return value
