"""Citations through the web app: extraction after a source is added (upload and URL), on
demand, its states (never run, running, done, failed, interrupted), re-matching when the library
changes, `cited_by`, the reading frontier, the pages' elements, the `create_app` default, and
the page model under node (tests/web/js/source_references_test.mjs)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from teacher.config import Settings
from teacher.db.engine import session_scope
from teacher.db.models import SourceReferenceExtraction
from teacher.generation import StubGenerator
from teacher.lab_generator import StubLabGenerator
from teacher.managers.source_references import STALE_RUN_AFTER
from teacher.reference_extraction import (
    ERROR_PREFIX,
    ReferenceExtractor,
    ReferenceOutputError,
    StubReferenceExtractor,
)
from teacher.url_sources import KIND_WEB_PAGE, FetchedSource
from teacher.web.app import create_app
from tests.web.conftest import send_source

JS_TESTS = Path(__file__).parent / "js"
STATIC_JS = Path(__file__).resolve().parents[2] / "src" / "teacher" / "web" / "static" / "js"

CITING = """# A survey

We build on attention.

## References

1. Vaswani et al. "Attention Is All You Need". NeurIPS 2017. arXiv:1706.03762v5.
2. He et al. "Deep Residual Learning for Image Recognition". CVPR 2016. doi:10.1109/CVPR.2016.90
3. A blog post on transformers, https://example.org/post
4. Smith et al. (2020)
"""


class ControlledStub(StubReferenceExtractor):
    """The stub, counting its calls and raising `fail_with` when it is set."""

    def __init__(self) -> None:
        self.fail_with: Exception | None = None
        self.calls = 0

    def extract(self, document):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return super().extract(document)


class FakeFetcher:
    def __init__(self, content: str) -> None:
        self.content = content

    def fetch(self, url: str) -> FetchedSource:
        return FetchedSource(
            requested_url=url, url=url, kind=KIND_WEB_PAGE, media_type="text/markdown",
            suffix=".md", content=self.content.encode("utf-8"), original_media_type="text/html",
            words=200,
        )  # fmt: skip


@pytest.fixture
def stub() -> ControlledStub:
    return ControlledStub()


@pytest.fixture
def ref_client(settings: Settings, stub: ControlledStub) -> Iterator[TestClient]:
    app = create_app(
        settings, generator=StubGenerator(), lab_generator=StubLabGenerator(),
        reference_extractor=stub, url_fetcher=FakeFetcher(CITING.replace("A survey", "A page")),
    )  # fmt: skip
    with TestClient(app) as client:
        yield client


def upload(client: TestClient, **kw: Any) -> dict[str, Any]:
    response = send_source(client, **kw)
    assert response.status_code == 201, response.text
    return response.json()


def refs(client: TestClient, slug: str) -> dict[str, Any]:
    response = client.get(f"/api/sources/{slug}/references")
    assert response.status_code == 200, response.text
    return response.json()


# -- without an extractor (the shared test app) -----------------------------------------------


def test_an_app_without_an_extractor_says_so(client: TestClient) -> None:
    source = upload(client, title="Plain", content=CITING, filename="p.md", content_type="text/markdown")
    data = refs(client, source["slug"])
    assert data["extraction"]["status"] == "never_run"
    assert data["extraction"]["available"] is False
    assert "without a reference extractor" in data["extraction"]["unavailable_reason"]
    assert data["references"] == [] and data["cited_by"] == []
    response = client.post(f"/api/sources/{source['slug']}/references/extract")
    assert response.status_code == 503
    assert "without a reference extractor" in response.json()["detail"]
    assert refs(client, source["slug"])["extraction"]["status"] == "never_run"


def test_unknown_source_is_404(client: TestClient) -> None:
    assert client.get("/api/sources/nope/references").status_code == 404
    assert client.post("/api/sources/nope/references/extract").status_code == 404


def test_the_extractor_comes_with_the_default_generator_only(settings: Settings) -> None:
    default = create_app(settings).state.reference_extractor
    assert isinstance(default, ReferenceExtractor) and default.model == "claude-haiku-5-5"
    assert create_app(settings, generator=StubGenerator()).state.reference_extractor is None
    stub = StubReferenceExtractor()
    app = create_app(settings, generator=StubGenerator(), reference_extractor=stub)
    assert app.state.reference_extractor is stub


# -- after a source is added ----------------------------------------------------------------


def test_upload_extracts_in_the_background(ref_client: TestClient, stub: ControlledStub) -> None:
    source = upload(ref_client, title="A survey", content=CITING, filename="s.md",
                    content_type="text/markdown")  # fmt: skip
    data = refs(ref_client, source["slug"])
    ext = data["extraction"]
    assert (ext["status"], ext["trigger"], ext["reference_count"], ext["found_in"]) == (
        "done", "after_create", 4, "reference_list"
    )
    assert ext["available"] is True and ext["error"] is None and ext["extractor"] == "stub-references-v1"
    assert [r["position"] for r in data["references"]] == [1, 2, 3, 4]
    first, second, third, fourth = data["references"]
    assert first["title"] == "Attention Is All You Need" and first["arxiv_id"] == "1706.03762"
    assert first["match"] is None
    assert (first["add_url"], first["add_url_kind"]) == ("https://arxiv.org/pdf/1706.03762", "arxiv")
    assert (second["add_url"], second["add_url_kind"]) == ("https://doi.org/10.1109/cvpr.2016.90", "doi")
    assert (third["add_url"], third["add_url_kind"]) == ("https://example.org/post", "url")
    assert (fourth["add_url"], fourth["add_url_kind"]) == (None, None)
    assert stub.calls == 1


def test_from_url_extracts_too(ref_client: TestClient) -> None:
    response = ref_client.post(
        "/api/sources/from-url", json={"url": "https://example.org/page", "title": "A page"}
    )
    assert response.status_code == 201, response.text
    ext = refs(ref_client, response.json()["slug"])["extraction"]
    assert (ext["status"], ext["reference_count"]) == ("done", 4)


def test_a_failed_extraction_keeps_the_source_and_offers_a_retry(
    ref_client: TestClient, stub: ControlledStub
) -> None:
    stub.fail_with = ReferenceOutputError(f"{ERROR_PREFIX}the model's reply breaks 1 rule(s).")
    source = upload(ref_client, title="A survey", content=CITING, filename="s.md",
                    content_type="text/markdown")  # fmt: skip
    assert ref_client.get(f"/api/sources/{source['slug']}").status_code == 200  # still there
    ext = refs(ref_client, source["slug"])["extraction"]
    assert ext["status"] == "failed" and ext["error"].endswith("breaks 1 rule(s).")
    assert ext["finished_at"] is not None and ext["reference_count"] is None
    stub.fail_with = None
    response = ref_client.post(f"/api/sources/{source['slug']}/references/extract")
    assert response.status_code == 202
    assert response.json()["extraction"]["status"] == "running"  # as returned, before the job
    ext = refs(ref_client, source["slug"])["extraction"]
    assert (ext["status"], ext["trigger"], ext["run_number"], ext["reference_count"]) == (
        "done", "on_demand", 2, 4
    )


def test_a_source_that_cites_nothing_is_done_with_zero(ref_client: TestClient) -> None:
    source = upload(ref_client, title="Notes", content="# Notes\n\nNothing cited.\n",
                    filename="n.md", content_type="text/markdown")  # fmt: skip
    ext = refs(ref_client, source["slug"])["extraction"]
    assert (ext["status"], ext["found_in"], ext["reference_count"]) == ("done", "none", 0)


def test_a_second_extraction_while_one_runs_is_409(
    settings: Settings, ref_client: TestClient, stub: ControlledStub
) -> None:
    source = upload(ref_client, title="A survey", content=CITING, filename="s.md",
                    content_type="text/markdown")  # fmt: skip
    with session_scope(settings) as session:  # a run in progress (as another process holds it)
        row = session.get(SourceReferenceExtraction, 1)
        row.status, row.finished_at, row.error = "running", None, None
    response = ref_client.post(f"/api/sources/{source['slug']}/references/extract")
    assert response.status_code == 409 and "already being extracted" in response.json()["detail"]
    assert refs(ref_client, source["slug"])["extraction"]["status"] == "running"
    with session_scope(settings) as session:  # the server stopped during it long ago
        row = session.get(SourceReferenceExtraction, 1)
        row.started_at = row.started_at - STALE_RUN_AFTER - timedelta(minutes=1)
    assert refs(ref_client, source["slug"])["extraction"]["status"] == "interrupted"
    assert ref_client.post(f"/api/sources/{source['slug']}/references/extract").status_code == 202
    assert refs(ref_client, source["slug"])["extraction"]["status"] == "done"


# -- matching when the library changes --------------------------------------------------------


def test_references_resolve_to_sources_added_later_and_cited_by(ref_client: TestClient) -> None:
    survey = upload(ref_client, title="A survey", content=CITING, filename="s.md",
                    content_type="text/markdown")  # fmt: skip
    attention = upload(ref_client, title="Attention Is All You Need",
                       identifiers=["arxiv:1706.03762"])  # fmt: skip
    first = refs(ref_client, survey["slug"])["references"][0]
    assert first["match"] == {"slug": attention["slug"], "title": "Attention Is All You Need",
                              "method": "arxiv"}  # fmt: skip
    assert first["add_url"] is None
    cited = refs(ref_client, attention["slug"])["cited_by"]
    assert cited == [{"slug": survey["slug"], "title": "A survey", "positions": [1]}]

    # A title edit re-matches: the ResNet reference matches by title once the source has it.
    resnet = upload(ref_client, title="Residual networks")
    assert refs(ref_client, survey["slug"])["references"][1]["match"] is None
    patched = ref_client.patch(
        f"/api/sources/{resnet['slug']}",
        json={"title": "Deep residual learning for image recognition", "originated_on": "2016"},
    )
    assert patched.status_code == 200
    second = refs(ref_client, survey["slug"])["references"][1]
    assert second["match"]["slug"] == resnet["slug"] and second["match"]["method"] == "title"

    # Deleting the matched source unmatches the reference again.
    assert ref_client.delete(f"/api/sources/{resnet['slug']}").status_code == 204
    second = refs(ref_client, survey["slug"])["references"][1]
    assert second["match"] is None and second["add_url_kind"] == "doi"


# -- the frontier -----------------------------------------------------------------------------


def test_frontier_ranks_and_groups_across_sources(ref_client: TestClient) -> None:
    empty = ref_client.get("/api/references/frontier").json()
    assert empty == {"items": [], "total": 0, "coverage": {
        "sources": 0, "never_run": 0, "running": 0, "interrupted": 0, "done": 0, "failed": 0}}
    upload(ref_client, title="A survey", content=CITING, filename="s.md", content_type="text/markdown")
    upload(ref_client, title="Another survey", filename="t.md", content_type="text/markdown",
           content="# T\n\n## References\n\n- Attention paper, arXiv:1706.03762\n")  # fmt: skip
    data = ref_client.get("/api/references/frontier").json()
    assert data["total"] == 4 and data["coverage"]["done"] == 2 and data["coverage"]["sources"] == 2
    top = data["items"][0]
    assert top["key"] == "arxiv:1706.03762"
    assert (top["citing_count"], top["reference_count"]) == (2, 2)
    assert top["title"] == "Attention Is All You Need"  # from the reference that has one
    assert top["add_url"] == "https://arxiv.org/pdf/1706.03762" and top["add_url_kind"] == "arxiv"
    assert [s["title"] for s in top["cited_by"]] == ["A survey", "Another survey"]
    assert [i["citing_count"] for i in data["items"]] == [2, 1, 1, 1]
    limited = ref_client.get("/api/references/frontier?limit=1").json()
    assert len(limited["items"]) == 1 and limited["total"] == 4
    assert ref_client.get("/api/references/frontier?limit=0").status_code == 422
    # Adding the most cited work takes it off the frontier.
    upload(ref_client, title="Attention Is All You Need", identifiers=["arxiv:1706.03762v2"])
    data = ref_client.get("/api/references/frontier").json()
    assert data["total"] == 3 and all(i["key"] != "arxiv:1706.03762" for i in data["items"])


# -- the pages --------------------------------------------------------------------------------


def test_source_page_has_the_references_section(client: TestClient) -> None:
    source = upload(client, title="Plain")
    html = client.get(f"/sources/{source['slug']}").text
    for marker in (
        'id="source-references-section"', 'id="source-references-status"',
        'id="source-references-extract"', 'id="source-references-error"',
        'id="source-references-list"', 'id="source-cited-by"',
        "/static/js/source_references.js", "/static/css/references.css",
    ):
        assert marker in html, marker


def test_add_page_has_the_frontier_list(client: TestClient) -> None:
    html = client.get("/add").text
    for marker in ('id="add-frontier"', 'id="add-frontier-list"', "/static/js/add_frontier.js"):
        assert marker in html, marker
    assert "prefilledUrl" in (STATIC_JS / "add.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_references_model_under_node() -> None:
    result = subprocess.run(
        ["node", str(JS_TESTS / "source_references_test.mjs")],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr + result.stdout
    assert "source references ok" in result.stdout
    for name in ("source_references.js", "source_references_model.js", "add_frontier.js", "add.js"):
        check = subprocess.run(
            ["node", "--check", str(STATIC_JS / name)], capture_output=True, text=True,
            timeout=60, check=False,
        )  # fmt: skip
        assert check.returncode == 0, check.stderr
