"""/api/sources endpoints."""

from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.managers import (
    ArtifactManager,
    KnowledgeSourceManager,
    SourceSegmentManager,
)

SOURCE_KEYS = {
    "slug",
    "kind",
    "title",
    "creators",
    "originated_on",
    "summary",
    "origin_url",
    "media_type",
    "content_sha256",
    "notes",
    "metadata",
    "added_at",
    "updated_at",
    "identifiers",
}


# -- create ------------------------------------------------------------------------------


def test_create_full(post_source, settings: Settings) -> None:
    content = b"Attention weighs tokens.\n"
    response = post_source(
        content=content,
        filename="paper.txt",
        kind="paper",
        title="Attention Is All You Need",
        slug="attention",
        creators=["Vaswani", "Shazeer"],
        identifiers=["arxiv:1706.03762", "doi:10.5555/3295222"],
        originated_on="2017-06-12",
        summary="Transformers.",
        origin_url="https://arxiv.org/abs/1706.03762",
        notes="classic",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == SOURCE_KEYS
    assert body["slug"] == "attention"
    assert body["kind"] == "paper"
    assert body["creators"] == ["Vaswani", "Shazeer"]
    assert body["originated_on"] == "2017-06-12"
    assert body["summary"] == "Transformers."
    assert body["origin_url"] == "https://arxiv.org/abs/1706.03762"
    assert body["notes"] == "classic"
    assert body["metadata"] == {}
    assert body["media_type"] == "text/plain"
    assert body["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert body["identifiers"] == [
        {"scheme": "arxiv", "value": "1706.03762"},
        {"scheme": "doi", "value": "10.5555/3295222"},
    ]
    for key in ("added_at", "updated_at"):
        assert datetime.fromisoformat(body[key]).utcoffset() is not None
    assert str(settings.root) not in response.text
    stored = settings.sources_dir / "attention" / "source.txt"
    assert stored.read_bytes() == content


def test_create_minimal_derives_slug(upload_source) -> None:
    body = upload_source(title="Café Notes: Part 1")
    assert body["slug"] == "cafe-notes-part-1"
    assert body["creators"] == [] and body["identifiers"] == []
    assert body["originated_on"] is None and body["summary"] is None


def test_create_blank_optional_fields_are_none(upload_source) -> None:
    body = upload_source(slug="", summary="", notes=" ", origin_url="", originated_on="")
    assert body["slug"] == "test-notes"
    assert body["summary"] is None and body["notes"] is None
    assert body["origin_url"] is None and body["originated_on"] is None


def test_create_media_type_from_extension_or_declared(upload_source) -> None:
    assert upload_source(title="Md", filename="a.md")["media_type"] == "text/markdown"
    odd = upload_source(title="Odd", filename="blob", content_type="application/x-thing")
    assert odd["media_type"] == "application/x-thing"


def test_create_same_scheme_twice(upload_source) -> None:
    body = upload_source(identifiers=["isbn:111", "isbn:222", "isbn:111"])
    assert body["identifiers"] == [
        {"scheme": "isbn", "value": "111"},
        {"scheme": "isbn", "value": "222"},
    ]


@pytest.mark.parametrize("value", ["2017", "2017-06", "2017-06-12"])
def test_create_partial_dates(upload_source, value: str) -> None:
    assert upload_source(originated_on=value)["originated_on"] == value


@pytest.mark.parametrize(
    "overrides",
    [
        {"slug": "Bad Slug"},
        {"title": "!!!"},  # no slug derivable
        {"title": "  "},
        {"kind": " "},
        {"identifiers": ["no-colon"]},
        {"identifiers": [":value"]},
        {"originated_on": "June 2017"},
        {"originated_on": "2017-13-01"},
        {"originated_on": "2017-02-30"},
        {"content": b""},
        {"kind": None},
        {"title": None},
    ],
)
def test_create_invalid_is_422(post_source, overrides: dict) -> None:
    response = post_source(**overrides)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)


def test_create_without_file_is_422(client: TestClient) -> None:
    response = client.post("/api/sources", data={"kind": "note", "title": "x"})
    assert response.status_code == 422
    assert "file" in response.json()["detail"]


def test_create_duplicates_are_409(
    client: TestClient, upload_source, post_source, settings: Settings
) -> None:
    upload_source(slug="one", content="same", identifiers=["isbn:1"])
    for overrides in (
        {"slug": "one", "content": "different"},
        {"slug": "two", "content": "same"},
        {"slug": "three", "content": "other", "identifiers": ["isbn:1"]},
        {"slug": "four", "content": "other2", "identifiers": ["doi:x", "isbn:1"]},
    ):
        response = post_source(**overrides)
        assert response.status_code == 409, overrides
        assert str(settings.root) not in response.json()["detail"]
    # Nothing was left behind by the refused uploads.
    assert sorted(p.name for p in settings.sources_dir.iterdir()) == ["one"]
    assert [s["slug"] for s in client.get("/api/sources").json()] == ["one"]


# -- list / get --------------------------------------------------------------------------


def test_list_and_filter(client: TestClient, upload_source) -> None:
    assert client.get("/api/sources").json() == []
    upload_source(title="A", kind="paper")
    upload_source(title="B", kind="book")
    upload_source(title="C", kind="paper")
    listed = client.get("/api/sources").json()
    assert [s["slug"] for s in listed] == ["a", "b", "c"]
    assert all(set(s) == SOURCE_KEYS for s in listed)
    papers = client.get("/api/sources", params={"kind": "paper"}).json()
    assert [s["slug"] for s in papers] == ["a", "c"]
    assert client.get("/api/sources", params={"kind": "video"}).json() == []


def test_get_with_artifacts(client: TestClient, upload_source, settings: Settings) -> None:
    upload_source(title="Book", identifiers=["isbn:9"])
    body = client.get("/api/sources/book").json()
    assert set(body) == SOURCE_KEYS | {"artifacts"}
    assert body["artifacts"] == []
    assert body["identifiers"] == [{"scheme": "isbn", "value": "9"}]

    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug("book")
        ArtifactManager(session, settings).create(
            slug="study-book", kind="study_page", title="Study: Book", generator="stub-v1",
            sources=[source],
        )
    body = client.get("/api/sources/book").json()
    assert body["artifacts"] == [
        {"slug": "study-book", "title": "Study: Book", "kind": "study_page", "status": "generating"}
    ]


def test_get_unknown_is_404(client: TestClient) -> None:
    response = client.get("/api/sources/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "no knowledge source with slug 'nope'"}


# -- patch -------------------------------------------------------------------------------


def test_patch(client: TestClient, upload_source) -> None:
    created = upload_source(title="Old", summary="s", notes="n")
    response = client.patch(
        "/api/sources/old",
        json={
            "title": "New",
            "creators": ["Ann"],
            "originated_on": "1999",
            "origin_url": "https://example.org",
            "notes": None,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["slug"] == "old"  # slug never changes
    assert body["title"] == "New"
    assert body["creators"] == ["Ann"]
    assert body["originated_on"] == "1999"
    assert body["origin_url"] == "https://example.org"
    assert body["notes"] is None
    assert body["summary"] == "s"  # untouched
    assert body["updated_at"] >= created["updated_at"]
    assert client.get("/api/sources/old").json()["title"] == "New"


def test_patch_empty_body_is_noop(client: TestClient, upload_source) -> None:
    created = upload_source()
    body = client.patch("/api/sources/test-notes", json={}).json()
    assert body["title"] == created["title"]


@pytest.mark.parametrize(
    "payload",
    [
        {"title": ""},
        {"title": None},
        {"kind": "book"},  # not patchable through the API
        {"slug": "x"},
        {"metadata": {}},
        {"creators": "Ann"},
        {"originated_on": "yesterday"},
    ],
)
def test_patch_invalid_is_422(client: TestClient, upload_source, payload: dict) -> None:
    upload_source()
    response = client.patch("/api/sources/test-notes", json=payload)
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], str)
    assert client.get("/api/sources/test-notes").json()["title"] == "Test Notes"


def test_patch_unknown_is_404(client: TestClient) -> None:
    assert client.patch("/api/sources/nope", json={"title": "x"}).status_code == 404


# -- delete ------------------------------------------------------------------------------


def test_delete(client: TestClient, upload_source, settings: Settings) -> None:
    upload_source()
    response = client.delete("/api/sources/test-notes")
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/sources/test-notes").status_code == 404
    assert not (settings.sources_dir / "test-notes").exists()
    assert client.delete("/api/sources/test-notes").status_code == 404


def test_delete_in_use_is_409(client: TestClient, upload_source, settings: Settings) -> None:
    upload_source()
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug("test-notes")
        ArtifactManager(session, settings).create(
            slug="study", kind="study_page", title="S", generator="g", sources=[source]
        )
    response = client.delete("/api/sources/test-notes")
    assert response.status_code == 409
    assert "used by 1 artifact" in response.json()["detail"]
    assert client.get("/api/sources/test-notes").status_code == 200
    assert (settings.sources_dir / "test-notes" / "source.txt").is_file()


# -- file --------------------------------------------------------------------------------


def test_file_download(client: TestClient, upload_source) -> None:
    upload_source(content=b"%PDF-1.4 fake", filename="Paper.PDF", title="Paper")
    response = client.get("/api/sources/paper/file")
    assert response.status_code == 200
    assert response.content == b"%PDF-1.4 fake"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")
    assert "paper.pdf" in response.headers["content-disposition"]


def test_file_unknown_or_missing_is_404(
    client: TestClient, upload_source, settings: Settings
) -> None:
    assert client.get("/api/sources/nope/file").status_code == 404
    upload_source()
    (settings.sources_dir / "test-notes" / "source.txt").unlink()
    response = client.get("/api/sources/test-notes/file")
    assert response.status_code == 404
    assert str(settings.root) not in response.text


# -- segments ----------------------------------------------------------------------------


def test_segments(client: TestClient, upload_source, settings: Settings) -> None:
    upload_source()
    assert client.get("/api/sources/test-notes/segments").json() == []
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).get_by_slug("test-notes")
        segs = SourceSegmentManager(session, settings)
        section = segs.add(
            source, kind="section", label="s1", title="Intro",
            locator={"type": "text_span", "start_char": 0, "end_char": 10},
        )
        segs.add(
            source, kind="figure", label="fig-1", parent=section,
            locator={"type": "pdf_regions",
                     "regions": [{"page": 1, "x0": 0, "y0": 0, "x1": 0.5, "y1": 0.5}]},
        )
        section_id = section.id

    listed = client.get("/api/sources/test-notes/segments").json()
    assert listed == [
        {
            "id": section_id,
            "kind": "section",
            "label": "s1",
            "title": "Intro",
            "parent_id": None,
            "position": 1,
            "locator": {"type": "text_span", "start_char": 0, "end_char": 10},
        },
        {
            "id": listed[1]["id"],
            "kind": "figure",
            "label": "fig-1",
            "title": None,
            "parent_id": section_id,
            "position": 2,
            "locator": {
                "type": "pdf_regions",
                "regions": [{"page": 1, "x0": 0.0, "y0": 0.0, "x1": 0.5, "y1": 0.5}],
            },
        },
    ]
    figures = client.get("/api/sources/test-notes/segments", params={"kind": "figure"}).json()
    assert [s["label"] for s in figures] == ["fig-1"]


def test_segments_unknown_source_is_404(client: TestClient) -> None:
    assert client.get("/api/sources/nope/segments").status_code == 404


def test_source_concepts(client: TestClient, upload_source, settings: Settings) -> None:
    from teacher.db.engine import session_scope
    from teacher.managers import (
        ConceptManager,
        KnowledgeSourceManager,
        LinkSpec,
        SourceConceptManager,
    )

    upload_source(slug="paper", title="A paper")
    response = client.get("/api/sources/paper/concepts")
    assert response.status_code == 200 and response.json() == []
    with session_scope(settings) as session:
        concepts = ConceptManager(session, settings)
        kd = concepts.create("knowledge distillation", "Train a student.", "paper")
        kl = concepts.create("KL divergence", "A divergence.", "user")
        prior = concepts.create("probability", "Chance.", "user")
        concepts.mark_reviewed(kl)
        SourceConceptManager(session, settings).replace(
            KnowledgeSourceManager(session, settings).get_by_slug("paper"),
            [
                LinkSpec(prior, "assumes", "exact", "probability"),
                LinkSpec(kl, "uses", "judged", "Kullback-Leibler divergence",
                         judge_reason="Same idea."),
                LinkSpec(kd, "introduces", "created", "knowledge distillation"),
            ],
        )
    assert client.get("/api/sources/paper/concepts").json() == [
        {"slug": "knowledge-distillation", "name": "knowledge distillation",
         "role": "introduces", "resolution": "created", "reviewed": False},
        {"slug": "kl-divergence", "name": "KL divergence", "role": "uses",
         "resolution": "judged", "reviewed": True},
        {"slug": "probability", "name": "probability", "role": "assumes",
         "resolution": "exact", "reviewed": False},
    ]
    missing = client.get("/api/sources/nope/concepts")
    assert missing.status_code == 404 and "nope" in missing.json()["detail"]
    # Deleting the source deletes its links (the concepts stay).
    assert client.delete("/api/sources/paper").status_code == 204
    with session_scope(settings) as session:
        assert len(ConceptManager(session, settings).list()) == 3


def test_offline_generation_keeps_a_sources_concepts(
    client: TestClient, upload_source, settings: Settings
) -> None:
    """The stub generator's concepts stage needs no keys and leaves existing links alone."""
    from teacher.db.engine import session_scope
    from teacher.managers import (
        ConceptManager,
        KnowledgeSourceManager,
        LinkSpec,
        SourceConceptManager,
    )

    upload_source(slug="paper", title="A paper")
    with session_scope(settings) as session:
        concept = ConceptManager(session, settings).create("attention", "Weights.", "paper")
        SourceConceptManager(session, settings).replace(
            KnowledgeSourceManager(session, settings).get_by_slug("paper"),
            [LinkSpec(concept, "uses", "created", "attention")],
        )
    created = client.post("/api/artifacts", json={"sources": ["paper"]})
    assert created.status_code == 202, created.text
    slug = created.json()["slug"]
    assert client.get(f"/api/artifacts/{slug}").json()["status"] == "ready"
    stages = [(r["stage"], r["status"]) for r in client.get(f"/api/artifacts/{slug}/runs").json()]
    assert ("concepts", "succeeded") in stages and stages[0] == ("preflight", "succeeded")
    assert [c["slug"] for c in client.get("/api/sources/paper/concepts").json()] == ["attention"]
