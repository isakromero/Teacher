"""The reader profile: what the reader has already shown they know, told to the page writer.

`ClaudeGenerator` runs it as the local `reader_profile` stage, right before `write`. It reads
the reader's standing on each concept (`teacher.managers.concept_standing`, read only, through
`ConceptStandingManager.standings()`) and picks, in this order and without repeats:

1. this document's concepts: the concepts an earlier generation linked to the source being
   taught (`SourceConceptManager.for_source`) that the reader has answered questions on
   (status weak, developing or strong; an unstudied concept says nothing), at most
   `DOCUMENT_LIMIT`, good standing first;
2. the reader's strongest concepts overall: strong, then developing, at most `STRONG_LIMIT`;
3. the reader's weakest concepts overall: weak, at most `WEAK_LIMIT`.

Within each group a concept ranks by its first-attempt pass rate (highest first for good
standing, lowest first for weak), then by the number of questions answered, then by name, so
the profile is deterministic. With every limit reached the profile lists `MAX_ENTRIES` concepts,
one short line each, which keeps the write prompt small. Each line names the concept, its status,
the first-attempt record behind it and up to `PAGES_PER_CONCEPT` study pages it was learned on
(the pages where its questions are tagged `main`, as the standing reports them; a page on this
same document, such as an earlier version of the page being written, is named as such, so the
writer does not refer the reader to the page they are about to replace).

`ReaderProfile.prompt()` is the text the write prompt gets after its outline (so after the
document's cache breakpoint): the profile in `<reader_profile>` tags, with instructions to
compress what the reader is strong on into a one-line reminder that names the earlier page, to
take extra care and add an exercise where the page touches a weak concept, and never to skip
what is specific to this document. When nothing is known (no standing at all, as for a new
reader or before any page was tagged), it says so and tells the writer to write as before. The
review and revise stages (`teacher.page_revision`) get the same text inside the write
instructions, so they do not "fix" a deliberate compression.

`ReaderProfile.record()` is what the stage saves to `<artifact>/generation/reader_profile.json`:
the chosen concepts with their evidence, the counts considered and the exact prompt text sent.

Reads only; it never writes to the database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.managers import (
    ArtifactManager,
    ConceptStandingManager,
    KnowledgeSourceManager,
    SourceConceptManager,
)
from teacher.managers.concept_standing import (
    DEVELOPING,
    GOOD_STANDING,
    STRONG,
    STRONG_FROM,
    STRONG_MIN_QUESTIONS,
    UNSTUDIED,
    WEAK,
    WEAK_BELOW,
    ConceptStanding,
)

READER_PROFILE_STAGE = "reader_profile"
READER_PROFILE_FILE = "reader_profile.json"

DOCUMENT_LIMIT = 10  # this document's concepts the reader has answered on
STRONG_LIMIT = 5  # the reader's strongest concepts elsewhere
WEAK_LIMIT = 5  # the reader's weakest concepts elsewhere
MAX_ENTRIES = DOCUMENT_LIMIT + STRONG_LIMIT + WEAK_LIMIT
PAGES_PER_CONCEPT = 2

DOCUMENT_GROUP = "this_document"
STRONG_GROUP = "strongest"
WEAK_GROUP = "weakest"

_TAG_RE = re.compile(r"<(/?)(reader_profile)(?=[\s>/]|$)", re.IGNORECASE)

READER_PROFILE_INSTRUCTIONS = """\
The reader. Teacher keeps track of how this reader has done on the concepts its earlier study \
pages tested, and <reader_profile> below says what it knows. Use it like this:

- Where the page relies on a concept the reader is in good standing on (strong or developing) \
and the document uses it rather than develops it, give a one-line reminder instead of a full \
explanation, and name the earlier page it was studied on (for example "as on the page \
*<that page's title>*"), so the reader can go back to it.
- Where the page touches a concept the reader is weak on, take extra care: build it up from \
the ground, with an example, and add an exercise that makes the reader use it.
- Never skip or shorten what is specific to this document. Its own problem, method, evidence \
and claims are taught in full however well the reader knows the concepts around them.
- The profile covers only concepts that earlier pages tested. Treat everything else as the \
outline says: the reader knows its background and nothing yet about this document.\
"""

NOTHING_KNOWN = """\
The reader. Nothing is known yet about this reader: they have not answered any question on the \
concepts Teacher keeps track of. Write the page as described above, for a reader who knows the \
background listed in the outline and nothing yet about this document.

<reader_profile>
Nothing is known about this reader yet.
</reader_profile>\
"""

_STATUS_WORDS = {
    STRONG: "strong",
    DEVELOPING: "developing",
    WEAK: "weak",
}


def _neutral(text: str) -> str:
    """Text from the database (concept names, page titles) with anything that looks like the
    profile's own tag escaped, so it cannot close the profile early."""
    return _TAG_RE.sub(lambda m: f"&lt;{m.group(1)}{m.group(2)}", " ".join(text.split()))


@dataclass(frozen=True)
class ProfileEntry:
    """One concept in the profile: which group it is in and the evidence behind its status."""

    group: str
    concept_id: int
    slug: str
    name: str
    status: str
    questions_answered: int
    first_attempt_passed: int
    pages: tuple[str, ...]  # how the line names the pages it was learned on

    def line(self) -> str:
        record = (
            f"{self.first_attempt_passed} of {self.questions_answered} question"
            f"{'' if self.questions_answered == 1 else 's'} right the first time"
        )
        studied = f"; studied on {', '.join(self.pages)}" if self.pages else ""
        return f"- {_neutral(self.name)}: {_STATUS_WORDS[self.status]} ({record}){studied}"

    def record(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "concept": {"id": self.concept_id, "slug": self.slug, "name": self.name},
            "status": self.status,
            "questions_answered": self.questions_answered,
            "first_attempt_passed": self.first_attempt_passed,
            "pages": list(self.pages),
        }


@dataclass(frozen=True)
class ReaderProfile:
    """What the writer is told about the reader (see the module docstring)."""

    source_slug: str
    entries: tuple[ProfileEntry, ...]
    concepts_with_standing: int  # concepts the reader has answered on, anywhere
    linked_concepts: int  # concepts linked to this source by an earlier generation

    @property
    def empty(self) -> bool:
        return not self.entries

    def group(self, name: str) -> list[ProfileEntry]:
        return [entry for entry in self.entries if entry.group == name]

    def prompt(self) -> str:
        """The section the write prompt gets after its outline."""
        if self.empty:
            return NOTHING_KNOWN
        parts = [
            READER_PROFILE_INSTRUCTIONS,
            "",
            "<reader_profile>",
            "Statuses come from the reader's first attempts at questions on earlier pages: "
            f'"strong" is at least {STRONG_MIN_QUESTIONS} questions with at least '
            f'{round(STRONG_FROM * 100)}% right the first time, "weak" is fewer than '
            f'{round(WEAK_BELOW * 100)}% right, "developing" is in between.',
        ]
        headings = {
            DOCUMENT_GROUP: "Concepts of this document that the reader has studied before:",
            STRONG_GROUP: "The reader's strongest concepts elsewhere:",
            WEAK_GROUP: "The reader's weakest concepts elsewhere:",
        }
        for group, heading in headings.items():
            entries = self.group(group)
            if entries:
                parts += ["", heading, *(entry.line() for entry in entries)]
        if not self.group(DOCUMENT_GROUP):
            unlinked = " (no earlier generation has linked concepts to this document yet)"
            parts += [
                "",
                "None of this document's concepts has been tested on an earlier page"
                + ("" if self.linked_concepts else unlinked)
                + ".",
            ]
        parts.append("</reader_profile>")
        return "\n".join(parts)

    def record(self) -> dict[str, Any]:
        """The `reader_profile.json` content, with the exact prompt text sent."""
        return {
            "stage": READER_PROFILE_STAGE,
            "source": self.source_slug,
            "limits": {
                DOCUMENT_GROUP: DOCUMENT_LIMIT,
                STRONG_GROUP: STRONG_LIMIT,
                WEAK_GROUP: WEAK_LIMIT,
                "pages_per_concept": PAGES_PER_CONCEPT,
            },
            "concepts_with_standing": self.concepts_with_standing,
            "linked_concepts": self.linked_concepts,
            "known": not self.empty,
            "entries": [entry.record() for entry in self.entries],
            "prompt": self.prompt(),
        }


def _rate(standing: ConceptStanding) -> float:
    rate = standing.first_attempt_pass_rate
    return 0.0 if rate is None else rate


def _good_key(standing: ConceptStanding) -> tuple[Any, ...]:
    return (
        standing.status != STRONG,
        -_rate(standing),
        -standing.questions_answered,
        standing.concept.name.casefold(),
        standing.concept.id,
    )


def _weak_key(standing: ConceptStanding) -> tuple[Any, ...]:
    return (
        _rate(standing),
        -standing.questions_answered,
        standing.concept.name.casefold(),
        standing.concept.id,
    )


def _entry(group: str, standing: ConceptStanding, same_document: set[int]) -> ProfileEntry:
    pages: list[str] = []
    for coverage in standing.pages:
        if coverage.role != "main" or not coverage.main_questions:
            continue
        artifact = coverage.artifact
        if artifact.id in same_document:
            label = f'"{_neutral(artifact.title)}" (an earlier page on this same document)'
        else:
            label = f'"{_neutral(artifact.title)}"'
        if label not in pages:
            pages.append(label)
        if len(pages) == PAGES_PER_CONCEPT:
            break
    return ProfileEntry(
        group=group,
        concept_id=standing.concept.id,
        slug=standing.concept.slug,
        name=standing.concept.name,
        status=standing.status,
        questions_answered=standing.questions_answered,
        first_attempt_passed=standing.first_attempt_passed,
        pages=tuple(pages),
    )


def build_reader_profile(session: Session, settings: Settings, source_slug: str) -> ReaderProfile:
    """The profile for a page on the source `source_slug` (see the module docstring)."""
    source = KnowledgeSourceManager(session, settings).get_by_slug(source_slug)
    standings = [
        s for s in ConceptStandingManager(session, settings).standings().values()
        if s.status != UNSTUDIED
    ]
    linked = {link.concept_id for link in SourceConceptManager(session, settings).for_source(source)}
    same_document = {a.id for a in ArtifactManager(session, settings).list(source=source)}

    chosen: list[ProfileEntry] = []
    taken: set[int] = set()

    def take(group: str, candidates: list[ConceptStanding], limit: int) -> None:
        count = 0
        for standing in candidates:
            if count == limit:
                break
            if standing.concept.id in taken:
                continue
            chosen.append(_entry(group, standing, same_document))
            taken.add(standing.concept.id)
            count += 1

    of_document = [s for s in standings if s.concept.id in linked]
    take(
        DOCUMENT_GROUP,
        sorted((s for s in of_document if s.status in GOOD_STANDING), key=_good_key)
        + sorted((s for s in of_document if s.status == WEAK), key=_weak_key),
        DOCUMENT_LIMIT,
    )
    take(
        STRONG_GROUP,
        sorted((s for s in standings if s.status in GOOD_STANDING), key=_good_key),
        STRONG_LIMIT,
    )
    take(
        WEAK_GROUP,
        sorted((s for s in standings if s.status == WEAK), key=_weak_key),
        WEAK_LIMIT,
    )
    return ReaderProfile(
        source_slug=source_slug,
        entries=tuple(chosen),
        concepts_with_standing=len(standings),
        linked_concepts=len(linked),
    )


__all__ = [
    "DOCUMENT_GROUP",
    "DOCUMENT_LIMIT",
    "MAX_ENTRIES",
    "NOTHING_KNOWN",
    "PAGES_PER_CONCEPT",
    "READER_PROFILE_FILE",
    "READER_PROFILE_INSTRUCTIONS",
    "READER_PROFILE_STAGE",
    "STRONG_GROUP",
    "STRONG_LIMIT",
    "WEAK_GROUP",
    "WEAK_LIMIT",
    "ProfileEntry",
    "ReaderProfile",
    "build_reader_profile",
]
