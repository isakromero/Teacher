"""The graph match stage: each drafted concept of an outline resolved against the concept graph
with concept extraction's resolver, read-only.

`GraphMatcher.match(settings, drafts)` reuses `teacher.concept_extraction` step 2 exactly, as
`_resolve_one`, `render_candidates` and `apply_judgements` and the judge's prompts and schema:

a. Exact: the drafted name (and aliases) looked up with `ConceptManager.find_by_name`; found by
   its name, with every hit one and the same concept: `exact`.
b. Otherwise a shortlist (exact hits, the concept whose slug the name would take, the search
   tiers and the embedding neighbours with cosine >= the extractor's `SIMILARITY_FLOOR`); an
   empty graph or an empty shortlist means `new` without a judge.
c. One judge call (the concept model) for every concept with a shortlist: `same_as` an id of
   its own shortlist (`judged`) or `new`, with a sentence of reason; checked as in extraction.

What it does NOT do, unlike extraction: write anything. No concept, alias, relation, source
link or embedding is created or changed, and nothing is committed: the resolver's reads run in
a session that is always rolled back (`read_only_session`). Extraction first brings the
embedding index up to date (`embed_stale`, a write); here a stale index is an error instead
(`check_index`, run before the run spends anything and again at the match): a similarity search
over a partial index would silently miss concepts, and embedding them is a write this stage must
not make. The fix is `teacher concepts reindex`. The drafted concepts are embedded (one request,
recorded in the cost ledger) only to search with; the vectors are not stored. The judgement
memo (`concept_pair_judgements`) is not involved: it belongs to concept relating, not to the
resolver, and nothing here reads or writes it. The one thing a similarity search may write is
the approximate-nearest-neighbour index file under `derived/_vector_index/`, a cache derived
from `concept_embeddings` that every similarity search (the graph page's search included)
reconciles and may save; no database row.

The judge's text inputs are the outline's names and summaries (written by the planner from the
reader's topic) and the graph's own concepts: no fetched or searched text. The call has no
tools; its system prompt says the names are data.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from teacher.claude_api import default_client
from teacher.claude_generator import DEFAULT_TIMEOUT, parse_reply, stream_message
from teacher.concept_extraction import (
    JUDGE_MAX_TOKENS,
    JUDGE_STEP,
    Candidate,
    ConceptExtractionError,
    ConceptExtractor,
    Resolution,
    WireJudgements,
    _resolve_one,
    apply_judgements,
)
from teacher.config import DEFAULT_CONCEPT_MODEL, Settings
from teacher.db.engine import session_factory
from teacher.db.fts import ConceptSearchUnavailableError
from teacher.db.models import Concept
from teacher.embeddings import Embedder, EmbeddingError, check_vectors
from teacher.exploration.common import (
    ERROR_PREFIX,
    ExplorationUnavailableError,
    GraphMatchError,
    output_error,
    unavailable,
)
from teacher.exploration.planner import DraftConcept
from teacher.managers import ConceptEmbeddingManager, ManagerError, StaleEmbeddingsError

REINDEX_COMMAND = "teacher concepts reindex"


@contextmanager
def read_only_session(settings: Settings) -> Iterator[Session]:
    """A session that is always rolled back and closed: whatever it is used for, nothing it
    does is committed."""
    session = session_factory(settings)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@dataclass(frozen=True)
class ConceptMatch:
    """How one drafted concept matched: `verdict` exact, judged or new; `concept_id` the
    matched concept (None for new); `reason` why (the judge's sentence, or the exact name
    found); `shortlist` what the judge saw (`[{"id", "slug", "name", "found_by"}]`)."""

    key: str
    verdict: str
    concept_id: int | None
    reason: str | None
    shortlist: tuple[dict[str, Any], ...] = ()

    def detail(self) -> dict[str, Any]:
        return {"shortlist": list(self.shortlist)}


def _candidate(draft: DraftConcept) -> Candidate:
    """The drafted concept as the resolver's candidate. Only its number, name, aliases and
    summary are read; the role is a placeholder the resolver never looks at."""
    return Candidate(
        number=draft.position,
        name=draft.name,
        aliases=draft.aliases,
        summary=draft.summary,
        role="uses",
        evidence=None,
    )


def _match_of(draft: DraftConcept, resolution: Resolution) -> ConceptMatch:
    shortlist = tuple(
        {**entry.concept.to_dict(), "found_by": list(entry.found_by)}
        for entry in resolution.shortlist
    )
    if resolution.verdict == "exact":
        assert resolution.concept is not None
        return ConceptMatch(
            draft.key, "exact", resolution.concept.id,
            f"exact name match: {resolution.matched_text!r}", shortlist,
        )  # fmt: skip
    if resolution.verdict == "judged":
        assert resolution.concept is not None
        return ConceptMatch(draft.key, "judged", resolution.concept.id, resolution.reason, shortlist)
    if resolution.verdict == "new":
        reason = resolution.reason or (
            "no concept of the graph comes close" if not shortlist else None
        )
        return ConceptMatch(draft.key, "new", None, reason, shortlist)
    raise AssertionError(f"unresolved verdict {resolution.verdict!r}")


class GraphMatcher:
    """Matches drafted concepts against the graph (see the module docstring). `client` may be
    any object with an Anthropic-compatible `messages.stream` (default: created on first
    use); `embedder` is the concept graph's embedder (the model its index was made with);
    `model` the judge's model."""

    def __init__(
        self,
        client: Any = None,
        *,
        embedder: Embedder,
        model: str = DEFAULT_CONCEPT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not callable(getattr(embedder, "embed", None)) or not callable(
            getattr(embedder, "check_available", None)
        ):
            raise TypeError("embedder must have embed(texts) and check_available()")
        self._client = client
        self.embedder = embedder
        self.model = model
        self.timeout = timeout
        # The judge's request is the extractor's, built by an extractor that is never run.
        self._judge = ConceptExtractor(client, embedder=embedder, model=model, timeout=timeout)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = default_client(unavailable)
        return self._client

    def check_available(self) -> None:
        """ExplorationUnavailableError naming every problem when no Claude client can be made
        or the embedder has no key; no request is made."""
        problems: list[str] = []
        try:
            self._get_client()
        except ExplorationUnavailableError as exc:
            problems.append(str(exc).removeprefix(ERROR_PREFIX))
        try:
            self.embedder.check_available()
        except EmbeddingError as exc:
            problems.append(str(exc))
        if problems:
            raise unavailable("; ".join(problems))

    def check_index(self, settings: Settings) -> int:
        """The number of concepts in the graph; ExplorationUnavailableError when any of them has
        no up-to-date embedding for the embedder's model (nothing is embedded here)."""
        with read_only_session(settings) as session:
            count = session.scalar(select(func.count(Concept.id))) or 0
            if not count:
                return 0
            stale = ConceptEmbeddingManager(session, settings).stale(self.embedder.model)
            if stale:
                examples = ", ".join(repr(c.slug) for c in stale[:5])
                more = f" and {len(stale) - 5} more" if len(stale) > 5 else ""
                raise unavailable(
                    f"{len(stale)} of {count} concepts of the graph have no up-to-date embedding "
                    f"for {self.embedder.model!r} ({examples}{more}), so the outline cannot be "
                    f"matched against the whole graph. Run `{REINDEX_COMMAND}`, then try again."
                )
        return count

    def _embed(self, texts: list[str]) -> list[list[float]]:
        model = self.embedder.model
        try:
            return check_vectors(self.embedder.embed(list(texts)), count=len(texts), model=model)
        except EmbeddingError as exc:
            raise unavailable(f"the outline could not be embedded: {exc}") from exc

    def match(self, settings: Settings, drafts: Sequence[DraftConcept]) -> list[ConceptMatch]:
        """Every draft's match, in the order given (see the module docstring)."""
        if not drafts:
            return []
        count = self.check_index(settings)
        candidates = [_candidate(draft) for draft in drafts]
        if not count:
            resolutions = [Resolution(candidate, "new") for candidate in candidates]
        else:
            vectors = self._embed([candidate.text for candidate in candidates])
            try:
                with read_only_session(settings) as session:
                    resolutions = [
                        _resolve_one(session, settings, candidate, vector, self.embedder.model)
                        for candidate, vector in zip(candidates, vectors, strict=True)
                    ]
            except StaleEmbeddingsError as exc:
                raise unavailable(
                    f"the graph's embeddings went stale during the match ({exc}); run "
                    f"`{REINDEX_COMMAND}`, then try again"
                ) from exc
            except EmbeddingError as exc:
                raise unavailable(f"the graph's embedding index cannot be used: {exc}") from exc
            except ConceptExtractionError as exc:  # e.g. too many exact hits for a shortlist
                raise output_error(
                    "a drafted concept cannot be matched: "
                    + str(exc).removeprefix("Concepts could not be extracted: ")
                ) from exc
            except (ManagerError, ConceptSearchUnavailableError) as exc:
                raise GraphMatchError(f"{ERROR_PREFIX}the concept graph could not be read: {exc}") from exc

        pending = [r for r in resolutions if r.verdict is None]
        if pending:
            request = self._judge.judge_request(pending)
            message = stream_message(
                self._get_client(), request, stage_name=JUDGE_STEP, error=unavailable,
                purpose="exploration",
            )  # fmt: skip
            judgements = parse_reply(
                JUDGE_STEP, message, WireJudgements, JUDGE_MAX_TOKENS,
                stopped=output_error, invalid=output_error, outcome="the outline was not matched",
            )  # fmt: skip
            try:
                apply_judgements(pending, judgements)
            except ConceptExtractionError as exc:
                raise output_error(
                    "the graph match judge's reply is invalid: "
                    + str(exc).removeprefix("Concepts could not be extracted: ")
                ) from exc
        return [_match_of(draft, r) for draft, r in zip(drafts, resolutions, strict=True)]


__all__ = ["REINDEX_COMMAND", "ConceptMatch", "GraphMatcher", "read_only_session"]
