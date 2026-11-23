"""Pure helpers for the concept graph, shared by the write paths and the read-side graph.

- `ORIGINS`, `RELATION_TYPES`, `SYMMETRIC_RELATION_TYPES`, `SOURCE_ROLES`, `LINK_RESOLUTIONS`:
  the closed vocabularies (the database CHECK constraints spell out the same values).
- `stronger_role(a, b)`: how two roles of one concept in one source combine (introduces > uses
  > assumes).
- `normalize_name(text)`: the one normalisation used for concept names and aliases (Unicode
  NFKC, strip, internal whitespace collapsed to one space, casefold). Lookups compare these.
- `find_cycle(edges)`: the one cycle detector, used to keep `builds_on` acyclic when writing and
  to refuse a cyclic graph when reading.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Hashable, Iterable, Mapping
from typing import TypeVar

ORIGINS: tuple[str, ...] = ("paper", "model", "user")
RELATION_TYPES: tuple[str, ...] = ("builds_on", "different_version_of", "relates_to")
SYMMETRIC_RELATION_TYPES: tuple[str, ...] = ("different_version_of", "relates_to")
BUILDS_ON = "builds_on"
# How a source relates to a concept (`source_concepts.role`): the source's own new idea, an idea
# it explains or relies on, or background it takes for granted. Strongest first.
SOURCE_ROLES: tuple[str, ...] = ("introduces", "uses", "assumes")
# How an extraction run found the concept it linked (`source_concepts.resolution`).
LINK_RESOLUTIONS: tuple[str, ...] = ("created", "exact", "judged")

Node = TypeVar("Node", bound=Hashable)


def normalize_name(text: str, *, what: str = "name") -> str:
    """`text` in the form names and aliases are compared in: NFKC, stripped, whitespace runs
    collapsed to one space, casefolded. TypeError for a non-string, ValueError if nothing is left.
    """
    if not isinstance(text, str):
        raise TypeError(f"{what} must be a string, got {text.__class__.__name__}")
    normalized = " ".join(unicodedata.normalize("NFKC", text).split()).casefold()
    if not normalized:
        raise ValueError(f"{what} {text!r} is empty after normalisation")
    return normalized


def validate_origin(origin: str) -> str:
    if origin not in ORIGINS:
        raise ValueError(f"invalid origin {origin!r}: expected one of {ORIGINS}")
    return origin


def validate_relation_type(type: str) -> str:
    if type not in RELATION_TYPES:
        raise ValueError(f"invalid relation type {type!r}: expected one of {RELATION_TYPES}")
    return type


def validate_source_role(role: str) -> str:
    if role not in SOURCE_ROLES:
        raise ValueError(f"invalid source role {role!r}: expected one of {SOURCE_ROLES}")
    return role


def validate_link_resolution(resolution: str) -> str:
    if resolution not in LINK_RESOLUTIONS:
        raise ValueError(
            f"invalid link resolution {resolution!r}: expected one of {LINK_RESOLUTIONS}"
        )
    return resolution


def stronger_role(a: str, b: str) -> str:
    """The role that wins when one source relates to one concept in two ways
    (introduces > uses > assumes)."""
    validate_source_role(a)
    validate_source_role(b)
    return a if SOURCE_ROLES.index(a) <= SOURCE_ROLES.index(b) else b


def find_cycle(edges: Mapping[Node, Iterable[Node]]) -> list[Node] | None:
    """A directed cycle in `edges` (node -> successors), or None if the graph is acyclic.

    The cycle is returned as `[n0, n1, ..., n0]` (first node repeated at the end), following
    edge direction. Nodes and successors are visited in sorted order, so the answer is
    deterministic. Iterative depth-first search, so deep graphs do not hit the recursion limit.
    """
    successors: dict[Node, list[Node]] = {}
    for node, targets in edges.items():
        successors.setdefault(node, [])
        for target in targets:
            successors[node].append(target)
            successors.setdefault(target, [])
    for node in successors:
        successors[node].sort()  # type: ignore[type-var]

    done: set[Node] = set()
    for start in sorted(successors):  # type: ignore[type-var]
        if start in done:
            continue
        path: list[Node] = [start]
        on_path: dict[Node, int] = {start: 0}
        iterators = [iter(successors[start])]
        while iterators:
            advanced = False
            for target in iterators[-1]:
                if target in on_path:
                    return [*path[on_path[target] :], target]
                if target not in done:
                    on_path[target] = len(path)
                    path.append(target)
                    iterators.append(iter(successors[target]))
                    advanced = True
                    break
            if not advanced:
                iterators.pop()
                finished = path.pop()
                del on_path[finished]
                done.add(finished)
    return None


def format_cycle(slugs: Iterable[str]) -> str:
    """`a -> b -> a`: a builds_on cycle in edge direction (each concept builds on the next)."""
    return " -> ".join(slugs)


__all__ = [
    "BUILDS_ON",
    "LINK_RESOLUTIONS",
    "ORIGINS",
    "RELATION_TYPES",
    "SOURCE_ROLES",
    "SYMMETRIC_RELATION_TYPES",
    "find_cycle",
    "format_cycle",
    "normalize_name",
    "stronger_role",
    "validate_link_resolution",
    "validate_origin",
    "validate_relation_type",
    "validate_source_role",
]
