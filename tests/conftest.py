"""Shared fixtures: a migrated tmp-root `settings` and a `session` bound to it.

`_no_anthropic_api` (autouse) makes sure no test can reach the real Anthropic API: it removes
every credential source the SDK would pick up (env vars, profiles, workload identity), points
the SDK's config dir at an empty temp dir, and points the base URL at a closed local port.

`_no_openai_api` (autouse) does the same for the OpenAI SDK used for embeddings: no key, org or
project from the environment, and a base URL at a closed local port.

`fake_anthropic` is the `FakeAnthropic` class: a stand-in client for `LLMJudgeGrader(client)`
and `MetadataExtractor(client)` (`messages.create`), and `ClaudeGenerator(client)` and
`Tutor(client)` (`messages.stream`, with the SDK's `text_stream` and `get_final_message()`).

`fake_embedder` is the `FakeEmbedder` class: a deterministic embedder for the concept graph (a
vector per text's first line, i.e. per concept name), with `check_available()`, recorded calls
and injectable failures; `FakeAnthropic.concepts_reply(...)` / `judge_reply(...)` build the two
concept-extraction replies.

`make_pdf(pages)` returns the bytes of a real PDF with `pages` blank pages, built with pypdf;
page i (0-based) is `100 + i` points wide, so tests can tell which pages were kept.

`local_runner` is a `LocalPythonRunner`: a stand-in for `teacher.pyodide_runtime.PythonRunner`
that runs a lab with the test interpreter in a subprocess (same semantics as the contract:
the entry as `__main__`, then each check in a fresh namespace; every check fails with "the
program did not run" when the entry fails). It records its calls; `available = False` makes
`check_available()` raise.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from pypdf import PdfWriter
from sqlalchemy.orm import Session

from teacher.config import Settings
from teacher.db.engine import dispose_engine, session_factory
from teacher.db.migrate import upgrade_to_head

ANTHROPIC_CREDENTIAL_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_WORKSPACE_ID",
    "ANTHROPIC_CUSTOM_HEADERS",
)
UNREACHABLE_BASE_URL = "http://127.0.0.1:9"  # discard port: any stray request fails fast
OPENAI_CREDENTIAL_ENV = (
    "OPENAI_API_KEY",
    "OPENAI_ADMIN_KEY",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "OPENAI_WEBHOOK_SECRET",
    "OPENAI_CUSTOM_HEADERS",
)


@pytest.fixture(autouse=True)
def _no_anthropic_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    for name in ANTHROPIC_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path_factory.mktemp("anthropic-config")))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", UNREACHABLE_BASE_URL)


@pytest.fixture(autouse=True)
def _no_openai_api(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in OPENAI_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", UNREACHABLE_BASE_URL)


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    """Settings rooted at a fresh `tmp_path / "home"`, with the database migrated to head."""
    s = Settings(root=tmp_path / "home")
    upgrade_to_head(s)
    yield s
    dispose_engine(s)


@pytest.fixture
def session(settings: Settings) -> Iterator[Session]:
    """A session on the migrated database. Tests may commit; anything uncommitted is rolled back."""
    sess = session_factory(settings)()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()


# -- a fake Anthropic client for the LLM judge ---------------------------------------------


class MidStreamError:
    """A queued outcome for `messages.stream`: the stream opens, `text_stream` yields `deltas`
    and then raises `error`, and `get_final_message()` raises `error` (as an SSE `error` event
    does in the real SDK)."""

    def __init__(self, error: BaseException, deltas: Sequence[str] = ()) -> None:
        self.error = error
        self.deltas = tuple(deltas)


def text_chunks(text: str) -> list[str]:
    """`text` split the way a stream delivers it: word by word, whitespace attached."""
    return re.findall(r"\s*\S+\s*", text) or [text]


class FakeStream:
    """What `FakeAnthropic.messages.stream(...)` returns: a context manager with the SDK's
    `text_stream` (the queued Message's text, in chunks) and `get_final_message()` (the queued
    Message, or a MidStreamError's error). If `gate` is set, `text_stream` waits for it before
    yielding anything."""

    def __init__(
        self, outcome: Message | MidStreamError, gate: threading.Event | None = None
    ) -> None:
        self.outcome = outcome
        self.gate = gate
        self.closed = False

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.closed = True

    @property
    def text_stream(self) -> Iterator[str]:
        return self._text()

    def _text(self) -> Iterator[str]:
        if self.gate is not None:
            assert self.gate.wait(10), "FakeStream: gate never opened"
        if isinstance(self.outcome, MidStreamError):
            yield from self.outcome.deltas
            raise self.outcome.error
        for block in self.outcome.content:
            if block.type == "text":
                yield from text_chunks(block.text)

    def get_final_message(self) -> Message:
        if isinstance(self.outcome, MidStreamError):
            raise self.outcome.error
        return self.outcome


class FakeAnthropic:
    """Stands in for `anthropic.Anthropic` in `LLMJudgeGrader(client)` and
    `ClaudeGenerator(client)`.

    `messages.create(**kwargs)` and `messages.stream(**kwargs)` record the call in `calls` and
    use the next queued outcome: a Message is returned (by `stream`'s `get_final_message()`), an
    exception is raised when the call is made, and a `MidStreamError` (stream only) is raised by
    `get_final_message()`. Build outcomes with `reply(...)`, `status_error(...)`, `timeout()`,
    `connection_error()` and `mid_stream(...)`. While `gate` (a `threading.Event`) is set on the
    fake and not yet open, streams wait for it before delivering text.
    """

    def __init__(self, *outcomes: Message | BaseException | MidStreamError) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []
        self.messages = self
        self.gate: threading.Event | None = None

    def _next(self, kwargs: dict[str, Any]) -> Message | MidStreamError:
        self.calls.append(kwargs)
        assert self.outcomes, "FakeAnthropic: unexpected API call"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def create(self, **kwargs: Any) -> Message:
        outcome = self._next(kwargs)
        assert isinstance(outcome, Message), "FakeAnthropic: mid-stream error for create()"
        return outcome

    def stream(self, **kwargs: Any) -> FakeStream:
        stream = FakeStream(self._next(kwargs), self.gate)
        self.streams.append(stream)
        return stream

    @staticmethod
    def mid_stream(error: BaseException, deltas: Sequence[str] = ()) -> MidStreamError:
        return MidStreamError(error, deltas)

    @property
    def prompt(self) -> str:
        """The user turn of the last call."""
        return self.calls[-1]["messages"][-1]["content"]

    @staticmethod
    def reply(
        criteria: Sequence[tuple[bool, str]] = (),
        *,
        feedback: str = "Good effort.",
        numbers: Sequence[int] | None = None,
        text: str | None = None,
        stop_reason: str = "end_turn",
        input_tokens: int = 321,
        output_tokens: int = 54,
        cache_creation_input_tokens: int | None = None,
        cache_read_input_tokens: int | None = None,
        stop_details: dict[str, Any] | None = None,
        model: str = "claude-haiku-5-5",
    ) -> Message:
        """A Message whose text block is a JudgeVerdict built from `(met, justification)` pairs
        (numbered 1.. unless `numbers` is given), or exactly `text` if given."""
        if text is None:
            nums = list(numbers) if numbers is not None else range(1, len(criteria) + 1)
            text = json.dumps(
                {
                    "criteria": [
                        {"number": n, "justification": why, "met": met}
                        for n, (met, why) in zip(nums, criteria, strict=True)
                    ],
                    "feedback": feedback,
                }
            )
        return Message.model_validate(
            {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": text},
                ],
                "stop_reason": stop_reason,
                "stop_details": stop_details,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": cache_creation_input_tokens,
                    "cache_read_input_tokens": cache_read_input_tokens,
                },
            }
        )

    @classmethod
    def metadata_reply(
        cls,
        *,
        title: str | None = "Attention Is All You Need",
        authors: Sequence[str] = ("Ashish Vaswani", "Noam Shazeer"),
        year: int | None = 2017,
        arxiv_id: str | None = "1706.03762",
        doi: str | None = None,
        summary: str | None = "The paper proposes the Transformer. It relies on attention only.",
        **kwargs: Any,
    ) -> Message:
        """A Message whose text block is a `WireMetadata` reply (for `MetadataExtractor`);
        other keyword arguments go to `reply` (e.g. `stop_reason`, `model`, `text`)."""
        if "text" not in kwargs:
            kwargs["text"] = json.dumps(
                {
                    "title": title,
                    "authors": list(authors),
                    "year": year,
                    "arxiv_id": arxiv_id,
                    "doi": doi,
                    "summary": summary,
                }
            )
        return cls.reply(**kwargs)

    @classmethod
    def concepts_reply(cls, concepts: Sequence[dict[str, Any]], **kwargs: Any) -> Message:
        """A `WireConcepts` reply (for `ConceptExtractor`'s extraction call)."""
        kwargs.setdefault("model", "claude-test-concepts")
        return cls.reply(text=json.dumps({"concepts": list(concepts)}), **kwargs)

    @classmethod
    def judge_reply(cls, judgements: Sequence[dict[str, Any]], **kwargs: Any) -> Message:
        """A `WireJudgements` reply (for `ConceptExtractor`'s judge call)."""
        kwargs.setdefault("model", "claude-test-concepts")
        return cls.reply(text=json.dumps({"judgements": list(judgements)}), **kwargs)

    @staticmethod
    def request() -> httpx2.Request:
        return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    @classmethod
    def status_error(
        cls, error_class: type[anthropic.APIStatusError], status: int, error_type: str = "api_error"
    ) -> anthropic.APIStatusError:
        response = httpx2.Response(
            status, request=cls.request(), headers={"request-id": "req_fake"}
        )
        body = {"type": "error", "error": {"type": error_type, "message": "fake"}}
        return error_class("fake", response=response, body=body)

    @classmethod
    def timeout(cls) -> anthropic.APITimeoutError:
        return anthropic.APITimeoutError(request=cls.request())

    @classmethod
    def connection_error(cls) -> anthropic.APIConnectionError:
        return anthropic.APIConnectionError(request=cls.request())


@pytest.fixture
def fake_anthropic() -> type[FakeAnthropic]:
    return FakeAnthropic


# -- a fake embedder for the concept graph -------------------------------------------------


class FakeEmbedder:
    """Deterministic embeddings keyed by a text's first line (a concept's name), casefolded.

    Names in `table` get that vector (padded with zeros to `DIMENSIONS`); every other name gets
    its own unit axis from `FREE_AXIS` on, in order of first use, so unrelated names are
    orthogonal (cosine 0). `calls` records every `embed` call's texts. `fail_on` (1-based call
    numbers) makes those calls raise `EmbeddingUnavailableError`; `before` maps a call number
    to a function run before that call answers (to change the database mid-run).
    `available = False` makes `check_available()` raise like a missing key.
    """

    DIMENSIONS = 64
    FREE_AXIS = 16

    def __init__(
        self, table: dict[str, Sequence[float]] | None = None, model: str = "fake-embed"
    ) -> None:
        self.model = model
        self.table = {key.casefold(): list(value) for key, value in (table or {}).items()}
        self.axes: dict[str, int] = {}
        self.calls: list[list[str]] = []
        self.fail_on: set[int] = set()
        self.before: dict[int, Callable[[], None]] = {}
        self.available = True

    def check_available(self) -> None:
        from teacher.embeddings import EmbeddingUnavailableError

        if not self.available:
            raise EmbeddingUnavailableError("Embedding is unavailable: no OpenAI API key (fake)")

    def vector(self, text: str) -> list[float]:
        key = text.split("\n", 1)[0].casefold()
        if key in self.table:
            values = self.table[key]
            return values + [0.0] * (self.DIMENSIONS - len(values))
        if key not in self.axes:
            self.axes[key] = self.FREE_AXIS + len(self.axes)
            assert self.axes[key] < self.DIMENSIONS, "FakeEmbedder: out of axes"
        vector = [0.0] * self.DIMENSIONS
        vector[self.axes[key]] = 1.0
        return vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        from teacher.embeddings import EmbeddingUnavailableError

        self.calls.append(list(texts))
        number = len(self.calls)
        if number in self.before:
            self.before[number]()
        if number in self.fail_on:
            raise EmbeddingUnavailableError(f"Embedding failed: fake failure on call {number}")
        return [self.vector(text) for text in texts]


@pytest.fixture
def fake_embedder() -> type[FakeEmbedder]:
    return FakeEmbedder


def build_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for i in range(pages):
        writer.add_blank_page(width=100 + i, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def make_pdf() -> Callable[[int], bytes]:
    return build_pdf


@pytest.fixture
def forbid_real_messages_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a real SDK client ever reaches `messages.create` or `messages.stream`."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a real Anthropic messages API call was attempted")

    monkeypatch.setattr(anthropic.resources.messages.Messages, "create", refuse)
    monkeypatch.setattr(anthropic.resources.messages.Messages, "stream", refuse)


# -- a local stand-in for the lab runtime --------------------------------------------------


@dataclass(frozen=True)
class LocalRunResult:
    ok: bool
    stdout: str
    stderr: str
    error: str | None
    timed_out: bool
    figures: int


@dataclass(frozen=True)
class LocalCheckResult:
    passed: bool
    output: str
    error: str | None


@dataclass(frozen=True)
class LocalLabRun:
    run: LocalRunResult
    checks: tuple[LocalCheckResult, ...]


_LOCAL_DRIVER = r"""
import contextlib, io, json, os, runpy, sys, traceback
work, entry, spec_file, out_file = sys.argv[1:5]
os.chdir(work)
sys.path.insert(0, work)
os.environ["MPLBACKEND"] = "Agg"
checks = json.loads(open(spec_file, encoding="utf-8").read())
out, err = io.StringIO(), io.StringIO()
ok, error = True, None
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    try:
        runpy.run_path(entry, run_name="__main__")
    except BaseException:
        ok, error = False, traceback.format_exc(limit=-3)
figures = 0
if "matplotlib.pyplot" in sys.modules:
    figures = len(sys.modules["matplotlib.pyplot"].get_fignums())
results = []
for number, check in enumerate(checks, start=1):
    if not ok:
        results.append({"passed": False, "output": "", "error": "the program did not run"})
        continue
    buffer = io.StringIO()
    passed, message = True, None
    with contextlib.redirect_stdout(buffer):
        try:
            exec(compile(check, f"<check {number}>", "exec"), {"__name__": "__check__"})
        except BaseException as exc:
            passed = False
            message = "".join(traceback.format_exception_only(exc)).strip()
    results.append({"passed": passed, "output": buffer.getvalue(), "error": message})
result = {"ok": ok, "stdout": out.getvalue(), "stderr": err.getvalue(), "error": error,
          "figures": figures, "checks": results}
open(out_file, "w", encoding="utf-8").write(json.dumps(result))
"""


class LocalPythonRunner:
    """Runs a lab with this interpreter in a subprocess (see the module docstring)."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.available = True
        self.calls: list[dict[str, Any]] = []

    def check_available(self) -> None:
        from teacher.pyodide_runtime import RuntimeUnavailableError

        if not self.available:
            raise RuntimeUnavailableError(
                "the Python runtime is not installed (fake); run `teacher labs setup`"
            )

    def execute(
        self,
        files: Mapping[str, str],
        *,
        entry: str,
        checks: Sequence[str] = (),
        packages: Sequence[str] = (),
    ) -> LocalLabRun:
        self.calls.append(
            {"files": dict(files), "entry": entry, "checks": list(checks), "packages": list(packages)}
        )
        with tempfile.TemporaryDirectory(prefix="teacher-lab-test-") as tmp:
            work = Path(tmp) / "work"
            work.mkdir()
            for name, content in files.items():
                (work / name).write_text(content, encoding="utf-8")
            spec_file, out_file = Path(tmp) / "checks.json", Path(tmp) / "result.json"
            spec_file.write_text(json.dumps(list(checks)), encoding="utf-8")
            try:
                subprocess.run(
                    [sys.executable, "-I", "-c", _LOCAL_DRIVER, str(work), entry,
                     str(spec_file), str(out_file)],
                    check=True, timeout=self.timeout, capture_output=True,
                )
            except subprocess.TimeoutExpired:
                return LocalLabRun(
                    LocalRunResult(False, "", "", "the program timed out", True, 0),
                    tuple(LocalCheckResult(False, "", "the program did not run") for _ in checks),
                )
            data = json.loads(out_file.read_text(encoding="utf-8"))
        return LocalLabRun(
            LocalRunResult(data["ok"], data["stdout"], data["stderr"], data["error"], False,
                           data["figures"]),
            tuple(LocalCheckResult(**item) for item in data["checks"]),
        )


@pytest.fixture
def local_runner() -> LocalPythonRunner:
    return LocalPythonRunner()


# -- a small library for the block tags, the standing and the suggestions --------------------

TAG_MC = {
    "question": "What does attention compute?",
    "options": ["A weighted sum of values", "A convolution", "A sort"],
    "private": {"correct_index": 0, "explanation": "Attention weighs the values."},
}
TAG_BLURB = {
    "prompt": "Explain how a transformer uses attention.",
    "min_words": 3,
    "private": {
        "model_answer": "A transformer stacks attention layers.",
        "rubric": [{"criterion": "Names attention", "required": True}],
    },
}
TAG_MC_SOFTMAX = {
    "question": "Which function turns the scores into weights?",
    "options": ["softmax", "relu"],
    "private": {"correct_index": 0, "explanation": "The softmax normalises them."},
}


@dataclass
class TagLibrary:
    """What `tag_library()` built (ids and slugs; every row committed)."""

    settings: Settings
    source: str
    page: str
    concepts: dict[str, int]  # slug -> id
    blocks: dict[str, int]  # key -> id


def build_tag_library(settings: Settings, tmp_path: Path) -> TagLibrary:
    """A source `paper` linked to the concepts attention, softmax, dot product and transformer
    (transformer builds on attention, attention on softmax and dot product), and a ready study
    page `study-paper` of it: b01 prose about attention, b02 a multiple choice on attention, b03
    prose about softmax, b04 a blurb on transformers, b05 a multiple choice on softmax. No block
    is tagged and nothing is answered."""
    from teacher.db.engine import session_scope
    from teacher.managers import (
        ArtifactBlockManager,
        ArtifactManager,
        ConceptManager,
        ConceptRelationManager,
        KnowledgeSourceManager,
        LinkSpec,
        SourceConceptManager,
    )

    path = tmp_path / "paper.txt"
    path.write_text("A paper about attention and the softmax.\n", encoding="utf-8")
    with session_scope(settings) as session:
        source = KnowledgeSourceManager(session, settings).add(
            path, slug="paper", kind="paper", title="The Paper"
        )
        concepts = ConceptManager(session, settings)
        made = {
            name: concepts.create(name, f"About {name}.", "paper")
            for name in ("attention", "softmax", "dot product", "transformer")
        }
        relations = ConceptRelationManager(session, settings)
        for dependent, prerequisite in (
            ("transformer", "attention"), ("attention", "softmax"), ("attention", "dot product"),
        ):
            relations.add(
                "builds_on", made[dependent], made[prerequisite],
                f"The {dependent} uses the {prerequisite}.", "paper",
            )
        SourceConceptManager(session, settings).replace(
            source,
            [
                LinkSpec(made["attention"], "introduces", "created", "attention", evidence="we attend"),
                LinkSpec(made["softmax"], "uses", "created", "softmax", evidence="a softmax"),
                LinkSpec(made["dot product"], "assumes", "created", "dot product"),
                LinkSpec(made["transformer"], "uses", "created", "transformer", evidence="layers"),
            ],
        )
        artifacts = ArtifactManager(session, settings)
        page = artifacts.create(
            slug="study-paper", kind="study_page", title="Study: The Paper", generator="test",
            sources=[source],
        )
        blocks = ArtifactBlockManager(session, settings)
        rows = [
            blocks.add(page, type="prose", content={"markdown": "# Attention\n\nIt weighs values."}),
            blocks.add(page, type="multiple_choice", content=TAG_MC, is_gate=True),
            blocks.add(page, type="prose", content={"markdown": "## Softmax\n\nScores to weights."}),
            blocks.add(page, type="blurb", content=TAG_BLURB, is_gate=True),
            blocks.add(page, type="multiple_choice", content=TAG_MC_SOFTMAX, is_gate=True),
        ]
        artifacts.mark_ready(page)
        return TagLibrary(
            settings=settings,
            source=source.slug,
            page=page.slug,
            concepts={c.slug: c.id for c in made.values()},
            blocks={b.key: b.id for b in rows},
        )


@pytest.fixture
def tag_library(settings: Settings, tmp_path: Path) -> TagLibrary:
    return build_tag_library(settings, tmp_path)
