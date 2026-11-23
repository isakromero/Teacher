"""GlossaryManager: replace-all, order, the built flag, validation, and cascades."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.models import Artifact, GlossaryTerm
from teacher.glossary import GlossaryEntry
from teacher.managers import ArtifactManager, DuplicateError, GlossaryManager

GAN = GlossaryEntry(
    "GAN", "acronym", "Two networks trained against each other.",
    expansion="generative adversarial network", aliases=("GANs",),
)
LOSS = GlossaryEntry("loss function", "term", "A number that measures how wrong a model is.")


@pytest.fixture
def artifacts(session: Session, settings: Settings) -> ArtifactManager:
    return ArtifactManager(session, settings)


@pytest.fixture
def artifact(artifacts: ArtifactManager) -> Artifact:
    return artifacts.create(slug="guide", kind="study_page", title="Guide", generator="g/1")


@pytest.fixture
def glossary(session: Session, settings: Settings) -> GlossaryManager:
    return GlossaryManager(session, settings)


def test_never_built_is_not_built_with_no_terms(
    glossary: GlossaryManager, artifact: Artifact
) -> None:
    assert glossary.list(artifact) == [] and not glossary.is_built(artifact)
    assert artifact.glossary_built_at is None
    assert glossary.replace(artifact, []) == []
    assert glossary.is_built(artifact) and artifact.glossary_built_at is not None
    assert glossary.list(artifact) == []


def test_replace_stores_in_order_and_lists(
    glossary: GlossaryManager, artifact: Artifact, session: Session
) -> None:
    rows = glossary.replace(artifact, [GAN, LOSS])
    session.commit()
    assert [(r.position, r.term, r.kind) for r in rows] == [
        (1, "GAN", "acronym"), (2, "loss function", "term"),
    ]
    listed = glossary.list(artifact)
    assert [r.id for r in listed] == [r.id for r in rows]
    assert listed[0].expansion == "generative adversarial network"
    assert listed[0].aliases == ["GANs"] and listed[1].aliases == [] and listed[1].expansion is None
    assert glossary.entries(artifact) == [GAN, LOSS]
    assert artifact.glossary_terms == listed


def test_replace_replaces_everything_and_updates_the_timestamp(
    glossary: GlossaryManager, artifact: Artifact, artifacts: ArtifactManager, session: Session
) -> None:
    other = artifacts.create(slug="other", kind="study_page", title="Other", generator="g/1")
    glossary.replace(other, [GAN])
    glossary.replace(artifact, [GAN, LOSS])
    first = artifact.glossary_built_at
    session.commit()
    new = GlossaryEntry("gan", "term", "Lower-case, so a different entry.")
    glossary.replace(artifact, [LOSS, new])
    session.commit()
    assert [(r.position, r.term) for r in glossary.list(artifact)] == [(1, "loss function"), (2, "gan")]
    assert artifact.glossary_built_at >= first
    assert [r.term for r in glossary.list(other)] == ["GAN"]  # other artifacts untouched
    assert session.scalar(select(func.count()).select_from(GlossaryTerm)) == 3


@pytest.mark.parametrize(
    ("entry", "error", "fragment"),
    [
        (GlossaryEntry(" ", "term", "d"), ValueError, "term must be a non-empty string"),
        (GlossaryEntry("x", "term", ""), ValueError, "definition must be"),
        (GlossaryEntry("x", "word", "d"), ValueError, "invalid kind 'word'"),
        (GlossaryEntry("x", "term", "d", expansion=" "), ValueError, "expansion must be"),
        (GlossaryEntry("x", "term", "d", aliases=("",)), ValueError, "every alias"),
        ({"term": "x"}, TypeError, "must be a GlossaryEntry"),
    ],
)
def test_invalid_entries_change_nothing(
    glossary: GlossaryManager, artifact: Artifact, session: Session,
    entry: object, error: type, fragment: str,
) -> None:
    glossary.replace(artifact, [GAN])
    session.commit()
    with pytest.raises(error, match=fragment):
        glossary.replace(artifact, [LOSS, entry])  # type: ignore[list-item]
    assert [r.term for r in glossary.list(artifact)] == ["GAN"]


def test_terms_differing_only_in_case_are_a_duplicate(
    glossary: GlossaryManager, artifact: Artifact
) -> None:
    with pytest.raises(DuplicateError, match=r"glossary term 2 \('gan'\) repeats term 1"):
        glossary.replace(artifact, [GAN, GlossaryEntry("gan", "term", "d")])
    assert glossary.list(artifact) == [] and not glossary.is_built(artifact)


def test_deleting_the_artifact_deletes_its_terms(
    glossary: GlossaryManager, artifact: Artifact, artifacts: ArtifactManager, session: Session
) -> None:
    keep = artifacts.create(slug="keep", kind="study_page", title="Keep", generator="g/1")
    glossary.replace(artifact, [GAN, LOSS])
    glossary.replace(keep, [LOSS])
    session.commit()
    artifacts.delete(artifact)
    session.commit()
    assert [r.term for r in session.scalars(select(GlossaryTerm))] == ["loss function"]


def test_database_cascade_without_the_orm(
    glossary: GlossaryManager, artifact: Artifact, session: Session
) -> None:
    glossary.replace(artifact, [GAN])
    session.commit()
    session.execute(text("DELETE FROM artifacts WHERE id = :a"), {"a": artifact.id})
    assert session.execute(text("SELECT count(*) FROM glossary_terms")).scalar() == 0
