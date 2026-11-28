"""What every part of an exploration shares: the caps, the errors and small text checks.

The caps (`LIMITS`, shown to the reader before a run starts): at most `MAX_CONCEPTS` concepts in
an outline, at most `MAX_SEARCHES_PER_CONCEPT` searches per concept (its first search and at
most one follow-up) and at most `MAX_CANDIDATES_PER_CONCEPT` candidate sources kept per concept.
There is no price estimate: what a run costs depends on how many concepts need sources and how
long the web searches take, and Teacher has no reliable figure before it runs (the cost ledger
shows what was spent once it has, `teacher.costs.exploration_cost`).

Errors. Every failure of a run is an `ExplorationError` whose message is shown to the reader
and stored on the exploration: `ExplorationUnavailableError` (something the run needs is
missing: credentials, an embedder, a source finder, an up-to-date embedding index; HTTP 503),
`ExplorationOutputError` (a model reply that cannot be used: refused, cut off, not in the
expected format or breaking a rule of the outline; HTTP 502) and `GraphMatchError` (the graph
could not be read; HTTP 500). There is no fallback: no template brief, no generic query.
"""

from __future__ import annotations

from typing import Any

from teacher.discovery.common import DEFAULT_LEVEL, LEVEL_LABELS, LEVELS, MAX_TOPIC_CHARS

MAX_CONCEPTS = 12
MAX_SEARCHES_PER_CONCEPT = 2  # the first search and at most one follow-up
MAX_CANDIDATES_PER_CONCEPT = 5
MAX_PROMPT_CHARS = MAX_TOPIC_CHARS
ERROR_PREFIX = "The exploration failed: "

LIMITS: dict[str, Any] = {
    "max_concepts": MAX_CONCEPTS,
    "searches_per_concept": MAX_SEARCHES_PER_CONCEPT,
    "candidates_per_concept": MAX_CANDIDATES_PER_CONCEPT,
    "price_estimate": None,
    "note": (
        f"At most {MAX_CONCEPTS} concepts; for each concept that is new or thinly covered, "
        f"at most {MAX_SEARCHES_PER_CONCEPT} source searches (one, and one follow-up when "
        f"nothing readable fits) and at most {MAX_CANDIDATES_PER_CONCEPT} candidate sources. "
        "There is no reliable price estimate before a run: what it spent is shown once it "
        "has run. Nothing is added to the library or the concept graph."
    ),
}


class ExplorationError(Exception):
    """An exploration failed; the message says why (no secret in it)."""


class ExplorationUnavailableError(ExplorationError):
    """Something the run needs is missing (HTTP 503)."""


class ExplorationOutputError(ExplorationError):
    """A model reply cannot be used (HTTP 502)."""


class GraphMatchError(ExplorationError):
    """The concept graph could not be read for the match (HTTP 500)."""


def unavailable(reason: str) -> ExplorationUnavailableError:
    return ExplorationUnavailableError(f"{ERROR_PREFIX}{reason}")


def output_error(reason: str) -> ExplorationOutputError:
    return ExplorationOutputError(f"{ERROR_PREFIX}{reason}")


def one_line(value: str, where: str, what: str, limit: int) -> str:
    """`value` stripped: not blank, one line, at most `limit` characters; else
    ExplorationOutputError naming `where` and `what`."""
    text = value.strip()
    if not text:
        raise output_error(f"{where}: the {what} is blank")
    if "\n" in text or "\r" in text:
        raise output_error(f"{where}: the {what} {text!r} spans several lines; it must be one line")
    if len(text) > limit:
        raise output_error(f"{where}: the {what} is {len(text)} characters long, more than {limit}")
    return text


def check_prompt(prompt: str) -> str:
    """The topic a reader typed, whitespace collapsed; ValueError when blank or too long."""
    if not isinstance(prompt, str):
        raise ValueError("the topic must be text")
    text = " ".join(prompt.split())
    if not text:
        raise ValueError("the topic is empty: say what to explore")
    if len(text) > MAX_PROMPT_CHARS:
        raise ValueError(f"the topic is longer than {MAX_PROMPT_CHARS} characters")
    return text


def check_level(level: str) -> str:
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}; choose one of: {', '.join(LEVELS)}")
    return level


def escape_tags(text: str) -> str:
    """`text` with `<` escaped, so text from the reader or the database cannot close a tag of
    the prompt it is put in."""
    return text.replace("<", "&lt;")


__all__ = [
    "DEFAULT_LEVEL",
    "ERROR_PREFIX",
    "LEVELS",
    "LEVEL_LABELS",
    "LIMITS",
    "MAX_CANDIDATES_PER_CONCEPT",
    "MAX_CONCEPTS",
    "MAX_PROMPT_CHARS",
    "MAX_SEARCHES_PER_CONCEPT",
    "ExplorationError",
    "ExplorationOutputError",
    "ExplorationUnavailableError",
    "GraphMatchError",
    "check_level",
    "check_prompt",
    "escape_tags",
    "one_line",
    "output_error",
    "unavailable",
]
