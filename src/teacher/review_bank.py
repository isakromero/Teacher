"""The review bank: fresh forms ("variants") of a study page's questions for the review queue.

Re-asking the identical question trains recognition of the question rather than recall of the
idea, so each question (multiple_choice, blurb) gets `VARIANTS_PER_QUESTION` variants that test
the same idea and concept with a different surface: reworded, a new scenario, a different
angle. A variant has the shape of its question's block type (`teacher.blocks`): a multiple
choice has `option_explanations`, a blurb a rubric; a blurb variant keeps the original's
`min_words`.

Building. One model call per page (`ReviewBankBuilder`, structured output `WireBank`) receives
the page's questions with their private parts (answer, explanations, model answer, rubric),
never the source document. The model is the artifact's own generation model
(`teacher.generation.generation_model`). The reply is then checked (`check_bank`), and any
problem fails the whole build loudly (`ReviewBankOutputError`, listing every problem):
- every question has exactly `VARIANTS_PER_QUESTION` variants of its own type, and nothing
  refers to a question that does not exist;
- every variant is valid block content (`teacher.blocks.validate_block_content`), and a
  multiple-choice variant has `option_explanations`;
- the page's own quality rules, implemented here as plain functions (the generator's checks in
  `teacher.claude_generator` are not imported, so this module does not depend on the page
  writer): the correct option is not clearly longer than the distractors
  (`correct_option_violation`: at most `CORRECT_LENGTH_RATIO` times the longest distractor, in
  characters), and a blurb rubric has one or two required criteria (`rubric_violation`);
- a variant's question text differs from the original's and from the other variants'.
A refusal, a truncated reply, a reply that does not parse and every API failure are errors too
(`ReviewBankUnavailableError` for the API side, `ReviewBankOutputError` for the reply).

Storing (`build_review_bank`). The build is recorded as a generation run of stage
`REVIEW_BANK_STAGE` on the artifact (model, tokens, error, and the output file
`generation/review_bank.json`); a run that fails records why. Its model call is recorded in
the cost ledger (`teacher.costs`, purpose "review_bank") with the artifact and the run. A successful build replaces the
page's variants in one transaction; a failed one leaves them as they were (so a page whose
rebuild failed keeps an older bank, and says so: `bank_status`). A bank never changes or fails
the page itself: without variants, reviews ask the original questions.

The bank is built in the background right after a study page's generation succeeds
(`build_after_generation`, scheduled by `POST /api/artifacts` and regenerate when the app has
a builder), and on demand for any ready page (`POST /api/artifacts/{slug}/review-bank`).

`StubReviewBankBuilder` is the model-free builder for offline work and tests (TEST/OFFLINE TOOL
ONLY): its variants are the original questions marked "Review form k" with the options rotated,
put through the same checks.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from teacher.blocks import is_question_type, validate_block_content
from teacher.claude_api import default_client, describe_api_error
from teacher.config import Settings
from teacher.costs import cost_scope, record_reply, require_meter
from teacher.db.engine import session_scope
from teacher.db.models import Artifact, ArtifactBlock, GenerationRun, ReviewVariant, utcnow
from teacher.labs import LAB_KIND
from teacher.managers import ArtifactBlockManager, ArtifactManager, GenerationRunManager

logger = logging.getLogger(__name__)

REVIEW_BANK_STAGE = "review_bank"
VARIANTS_PER_QUESTION = 2
CORRECT_LENGTH_RATIO = 1.3  # the correct option at most this times the longest distractor
MAX_REQUIRED_CRITERIA = 2
OUTPUT_DIR = "generation"  # the artifact's folder of stage outputs (as the generator's)
BANK_FILE = "review_bank.json"
MAX_TOKENS = 32000
DEFAULT_TIMEOUT = 600.0
# A run still 'running' after this long was interrupted (the server stopped during the call):
# it is reported as such, and a new build may start.
STALE_RUN_AFTER = timedelta(minutes=30)
ERROR_PREFIX = "The review bank could not be built: "


# -- errors ---------------------------------------------------------------------------------


class ReviewBankError(Exception):
    """A build failed. `tokens` are the (input, output) tokens of a reply that was received."""

    def __init__(self, message: str, *, tokens: tuple[int | None, int | None] = (None, None)):
        super().__init__(message)
        self.tokens = tokens


class ReviewBankUnavailableError(ReviewBankError):
    """The model could not be reached or used (credentials, API errors): HTTP 503."""


class ReviewBankOutputError(ReviewBankError):
    """The model's reply is unusable: refused, cut off, unparsable, or breaking a rule (502)."""


class ReviewBankInputError(ReviewBankError):
    """There is nothing to build from (not a ready study page, no questions): HTTP 422."""


class ReviewBankBusyError(ReviewBankError):
    """A build of this page's bank is already running: HTTP 409."""


# -- questions and variants -----------------------------------------------------------------


@dataclass(frozen=True)
class BankQuestion:
    """One question of the page: its number in the prompt (1..), block key and id, type and
    full stored content (private part included)."""

    number: int
    block_id: int
    key: str
    type: str
    content: dict[str, Any]


@dataclass(frozen=True)
class BankVariant:
    """One variant: the number of the question it varies, and its block content."""

    number: int
    type: str
    content: dict[str, Any]


@dataclass(frozen=True)
class BankBuild:
    """What a builder returns: the checked variants and the bookkeeping for the run."""

    variants: tuple[BankVariant, ...]
    model: str
    input_tokens: int | None
    output_tokens: int | None
    record: dict[str, Any] = field(default_factory=dict)


def page_questions(session: Session, settings: Settings, artifact: Artifact) -> list[BankQuestion]:
    """The page's questions (multiple_choice and blurb, not predictions) in reading order."""
    blocks = ArtifactBlockManager(session, settings)
    out = []
    for block in blocks.list(artifact):
        if is_question_type(block.type):
            out.append(
                BankQuestion(len(out) + 1, block.id, block.key, block.type, blocks.read_content(block))
            )
    return out


# -- the quality rules ----------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def correct_option_violation(options: Sequence[str], correct_index: int) -> str | None:
    """Why the correct option gives itself away by its length, or None: it may be at most
    `CORRECT_LENGTH_RATIO` times as long (in characters, surrounding space ignored) as the
    longest distractor."""
    correct = len(options[correct_index].strip())
    longest = max(len(o.strip()) for i, o in enumerate(options) if i != correct_index)
    if correct > CORRECT_LENGTH_RATIO * longest:
        return (
            f"the correct option is {correct} characters long, more than {CORRECT_LENGTH_RATIO} "
            f"times the longest distractor ({longest}): its length gives the answer away"
        )
    return None


def rubric_violation(rubric: Sequence[dict[str, Any]]) -> str | None:
    """Why a blurb rubric breaks the page's rule, or None: one or two required criteria."""
    required = sum(1 for item in rubric if item.get("required", True))
    if not 1 <= required <= MAX_REQUIRED_CRITERIA:
        return (
            f"the rubric has {required} required criteria; it needs 1 to "
            f"{MAX_REQUIRED_CRITERIA}, each plainly asked for by the prompt"
        )
    return None


def _text_of(content: dict[str, Any]) -> str:
    return content.get("question") or content.get("prompt") or ""


def check_bank(questions: Sequence[BankQuestion], variants: Sequence[BankVariant]) -> list[BankVariant]:
    """The variants, validated and normalised, in question order; ReviewBankOutputError
    listing every problem (see the module docstring)."""
    problems: list[str] = []
    by_number = {q.number: q for q in questions}
    grouped: dict[int, list[BankVariant]] = {q.number: [] for q in questions}
    for variant in variants:
        question = by_number.get(variant.number)
        if question is None:
            problems.append(f"a variant refers to question {variant.number}, which does not exist")
            continue
        if variant.type != question.type:
            problems.append(
                f"question {variant.number} is a {question.type}, but a variant of it is a "
                f"{variant.type}"
            )
            continue
        grouped[variant.number].append(variant)
    checked: list[BankVariant] = []
    for question in questions:
        found = grouped[question.number]
        where = f"question {question.number} ({question.key})"
        if len(found) != VARIANTS_PER_QUESTION:
            problems.append(
                f"{where} has {len(found)} variants; exactly {VARIANTS_PER_QUESTION} are needed"
            )
        seen = {_norm(_text_of(question.content))}
        for index, variant in enumerate(found, start=1):
            at = f"{where}, variant {index}"
            try:
                content = validate_block_content(variant.type, variant.content)
            except ValueError as exc:
                problems.append(f"{at}: {exc}")
                continue
            private = content["private"]
            if variant.type == "multiple_choice":
                if "option_explanations" not in private:
                    problems.append(f"{at}: it has no option_explanations")
                why = correct_option_violation(content["options"], private["correct_index"])
                if why:
                    problems.append(f"{at}: {why}")
            else:
                why = rubric_violation(private["rubric"])
                if why:
                    problems.append(f"{at}: {why}")
            text = _norm(_text_of(content))
            if text in seen:
                problems.append(f"{at}: it asks the same question as the original or another variant")
            seen.add(text)
            checked.append(BankVariant(variant.number, variant.type, content))
    if problems:
        raise ReviewBankOutputError(
            f"{ERROR_PREFIX}the variants break the rules: " + "; ".join(problems)
        )
    return checked


# -- the model call -------------------------------------------------------------------------


class WireCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: str = Field(description="What a good answer must show, plainly asked for by the prompt.")
    required: bool = Field(description="Whether the answer must meet it to pass (one or two per rubric).")


class WireChoiceVariant(BaseModel):
    """A variant of a multiple-choice question."""

    model_config = ConfigDict(extra="forbid")

    question_number: int = Field(description="The number of the original question it varies.")
    question: str
    options: list[str] = Field(description="2 to 8 options; the correct one not longer than the others.")
    correct_index: int = Field(description="0-based index of the correct option.")
    explanation: str = Field(description="Why the correct option is right.")
    option_explanations: list[str] = Field(
        description="One entry per option, in option order: for the correct option why it is "
        "right; for a wrong option why it is wrong, without revealing which option is correct."
    )


class WireWrittenVariant(BaseModel):
    """A variant of a written-answer (blurb) question."""

    model_config = ConfigDict(extra="forbid")

    question_number: int = Field(description="The number of the original question it varies.")
    prompt: str
    model_answer: str
    rubric: list[WireCriterion] = Field(description="One or two required criteria, plus optional ones.")


class WireBank(BaseModel):
    """The structured output: every variant, of either kind."""

    model_config = ConfigDict(extra="forbid")

    multiple_choice: list[WireChoiceVariant]
    blurb: list[WireWrittenVariant]


BANK_FORMAT: dict[str, Any] = {"type": "json_schema", "schema": anthropic.transform_schema(WireBank)}

SYSTEM_PROMPT = f"""\
You write review questions for a self-study tool. A learner has read a study page and answered \
its questions. Days or weeks later the tool asks them again, spaced out, to strengthen what \
they learned. If it asked the identical question every time, the learner would soon recognise \
the question and its answer instead of recalling the idea, so each question needs fresh forms.

You receive the page's questions with their answers: for a multiple-choice question its \
options, the correct one, the explanation and why each option is right or wrong; for a written \
question its prompt, a model answer and a rubric. For every question write exactly \
{VARIANTS_PER_QUESTION} variants. Each variant tests the same idea, about the same concept, at \
about the same difficulty, with a different surface: reword it, set it in a new concrete \
scenario, or come at it from another angle (ask for the cause instead of the effect, apply the \
idea to a case, spot the wrong statement). The two variants should differ from each other too. \
A learner who understood the idea should be able to answer every variant; one who only \
remembers the original's wording should not be helped by it. Stay within what the question, its \
answer and its explanations establish: you do not have the study material itself, so do not \
introduce facts it may not support.

A variant has the same kind as its question.
- Multiple choice: 3 to 5 options with exactly one correct. Make the wrong options plausible \
mistakes a learner could make, not jokes. Keep the options alike in length and style: the \
correct option must not be noticeably longer or more carefully qualified than the others (the \
tool rejects a correct option longer than {CORRECT_LENGTH_RATIO} times the longest wrong one). \
No "all of the above" or "none of the above". Give the explanation of the correct answer, and \
one entry per option in option order: for the correct option why it is right; for each wrong \
option why that option is wrong, written so that it does not reveal which option is correct, \
because the learner sees it after picking that option.
- Written answer: a prompt that plainly asks for what the rubric requires, a model answer, and \
a rubric with one or two required criteria (never more), each one something the prompt asks \
for, plus optional criteria if useful.

Write each variant so it stands on its own: do not refer to "the original question", "the \
page" or "as above". Respond in the requested JSON format, with every variant under the list of \
its kind and `question_number` set to the number of the question it varies.\
"""

_TAG_RE = re.compile(r"<(/?)\s*(question)\b", re.IGNORECASE)


def _neutralise(text: str) -> str:
    """Escape anything that looks like our <question> delimiter inside a field."""
    return _TAG_RE.sub(lambda m: f"&lt;{m.group(1)}{m.group(2)}", text)


def render_question(question: BankQuestion) -> str:
    """One question with its private part, in the tags the prompt uses."""
    content = question.content
    private = content["private"]
    lines = [f'<question number="{question.number}" type="{question.type}">']
    if question.type == "multiple_choice":
        lines.append(f"Question: {_neutralise(content['question'])}")
        lines.append("Options:")
        for index, option in enumerate(content["options"]):
            mark = " (correct)" if index == private["correct_index"] else ""
            lines.append(f"  {index}. {_neutralise(option)}{mark}")
        lines.append(f"Explanation: {_neutralise(private.get('explanation') or '')}")
        explanations = private.get("option_explanations")
        if explanations:
            lines.append("Why each option is right or wrong:")
            lines += [f"  {i}. {_neutralise(text)}" for i, text in enumerate(explanations)]
    else:
        lines.append(f"Prompt: {_neutralise(content['prompt'])}")
        lines.append(f"Model answer: {_neutralise(private['model_answer'])}")
        lines.append("Rubric:")
        for item in private["rubric"]:
            kind = "required" if item.get("required", True) else "optional"
            lines.append(f"  - ({kind}) {_neutralise(item['criterion'])}")
    lines.append("</question>")
    return "\n".join(lines)


def build_user_prompt(questions: Sequence[BankQuestion]) -> str:
    body = "\n\n".join(render_question(q) for q in questions)
    return (
        f"{body}\n\nWrite exactly {VARIANTS_PER_QUESTION} variants for each of the "
        f"{len(questions)} questions above."
    )


def variants_from_wire(questions: Sequence[BankQuestion], wire: WireBank) -> list[BankVariant]:
    """The reply's variants as block content (a blurb variant takes its question's
    `min_words`); not yet checked."""
    by_number = {q.number: q for q in questions}
    out: list[BankVariant] = []
    for v in wire.multiple_choice:
        out.append(BankVariant(v.question_number, "multiple_choice", {
            "question": v.question,
            "options": list(v.options),
            "private": {
                "correct_index": v.correct_index,
                "explanation": v.explanation,
                "option_explanations": list(v.option_explanations),
            },
        }))
    for v in wire.blurb:
        original = by_number.get(v.question_number)
        min_words = original.content.get("min_words", 20) if original is not None else 20
        out.append(BankVariant(v.question_number, "blurb", {
            "prompt": v.prompt,
            "min_words": min_words,
            "private": {
                "model_answer": v.model_answer,
                "rubric": [{"criterion": c.criterion, "required": c.required} for c in v.rubric],
            },
        }))
    return out


def _tokens(message: Any) -> tuple[int | None, int | None]:
    usage = getattr(message, "usage", None)
    if usage is None:
        return (None, None)
    total = usage.input_tokens + (usage.cache_creation_input_tokens or 0) + (
        usage.cache_read_input_tokens or 0
    )
    return (total, usage.output_tokens)


class ReviewBankBuilder(Protocol):
    """What builds a page's variants (`ClaudeReviewBankBuilder`, `StubReviewBankBuilder`)."""

    name: str

    def check_available(self) -> None:
        """Raise ReviewBankUnavailableError now if `build` could not succeed (credentials)."""
        ...

    def build(self, questions: Sequence[BankQuestion], *, model: str) -> BankBuild:
        """The checked variants of `questions`, made with `model`; ReviewBankError otherwise."""
        ...


def _unavailable(reason: str) -> ReviewBankUnavailableError:
    return ReviewBankUnavailableError(f"{ERROR_PREFIX}{reason}")


class ClaudeReviewBankBuilder:
    """One streamed Claude call per page with structured output, then `check_bank`.

    `client` may be any object with an Anthropic-compatible `messages.stream`; by default an
    `anthropic.Anthropic()` is created on first use, so the app starts without credentials."""

    name = "claude-review-bank-v1"

    def __init__(self, client: Any = None, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._client = client
        self.timeout = timeout

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = default_client(_unavailable)
        return self._client

    def check_available(self) -> None:
        self._get_client()

    def request(self, questions: Sequence[BankQuestion], *, model: str) -> dict[str, Any]:
        """The keyword arguments of the `messages.stream` call."""
        if not questions:
            raise ReviewBankInputError(f"{ERROR_PREFIX}the page has no questions to vary")
        return {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": build_user_prompt(questions)}],
            "output_config": {"effort": "medium", "format": BANK_FORMAT},
            "timeout": self.timeout,
        }

    def build(self, questions: Sequence[BankQuestion], *, model: str) -> BankBuild:
        request = self.request(questions, model=model)
        client = self._get_client()
        meter = require_meter(what="review bank call to Claude")
        try:
            with client.messages.stream(**request) as stream:
                message = stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise _unavailable(
                describe_api_error(exc, model=model, timeout=self.timeout, with_api_message=True)
            ) from exc
        record_reply(message, meter=meter, purpose="review_bank", step="review_bank")
        tokens = _tokens(message)
        text = self._text(message, tokens)
        try:
            wire = WireBank.model_validate_json(text)
        except ValidationError as exc:
            raise ReviewBankOutputError(
                f"{ERROR_PREFIX}the model's reply does not match the schema "
                f"({exc.error_count()} error(s))",
                tokens=tokens,
            ) from exc
        try:
            variants = check_bank(questions, variants_from_wire(questions, wire))
        except ReviewBankOutputError as exc:
            exc.tokens = tokens
            raise
        return BankBuild(
            tuple(variants),
            model=getattr(message, "model", model),
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            record={"builder": self.name, "model": model, "reply": json.loads(text)},
        )

    @staticmethod
    def _text(message: Any, tokens: tuple[int | None, int | None]) -> str:
        stop = message.stop_reason
        if stop == "refusal":
            raise ReviewBankOutputError(
                f"{ERROR_PREFIX}the model declined to write variants for this page", tokens=tokens
            )
        if stop == "max_tokens":
            raise ReviewBankOutputError(
                f"{ERROR_PREFIX}the model's reply was cut off (max_tokens={MAX_TOKENS})",
                tokens=tokens,
            )
        if stop != "end_turn":
            raise ReviewBankOutputError(
                f"{ERROR_PREFIX}the model stopped unexpectedly ({stop!r})", tokens=tokens
            )
        text = next((b.text for b in message.content if b.type == "text"), None)
        if text is None:
            raise ReviewBankOutputError(f"{ERROR_PREFIX}the model returned no text", tokens=tokens)
        return text


class StubReviewBankBuilder:
    """TEST/OFFLINE TOOL ONLY: variants without a model. Variant k of a question is the
    question with "(Review form k)" in front and its options rotated by k (the answer and the
    explanations follow them); a blurb keeps its model answer and rubric. Never a default."""

    name = "stub-review-bank-v1"

    def check_available(self) -> None:
        return None

    def build(self, questions: Sequence[BankQuestion], *, model: str) -> BankBuild:
        if not questions:
            raise ReviewBankInputError(f"{ERROR_PREFIX}the page has no questions to vary")
        variants = []
        for q in questions:
            for k in range(1, VARIANTS_PER_QUESTION + 1):
                content = json.loads(json.dumps(q.content))
                private = content["private"]
                if q.type == "multiple_choice":
                    n = len(content["options"])
                    order = [(i + k) % n for i in range(n)]  # new position -> old index
                    content["question"] = f"(Review form {k}) {content['question']}"
                    content["options"] = [q.content["options"][i] for i in order]
                    private["correct_index"] = order.index(q.content["private"]["correct_index"])
                    if "option_explanations" in private:
                        old = q.content["private"]["option_explanations"]
                        private["option_explanations"] = [old[i] for i in order]
                else:
                    content["prompt"] = f"(Review form {k}) {content['prompt']}"
                variants.append(BankVariant(q.number, q.type, content))
        checked = check_bank(questions, variants)
        return BankBuild(tuple(checked), model="none", input_tokens=None, output_tokens=None,
                         record={"builder": self.name})


# -- storing and status ---------------------------------------------------------------------


@dataclass(frozen=True)
class BankOutcome:
    """A successful build: how many questions and variants, and the run's id."""

    questions: int
    variants: int
    run_id: int


def _error_text(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{exc.__class__.__name__}: {message}" if message else exc.__class__.__name__


def _is_stale(run: GenerationRun, now: datetime) -> bool:
    return run.status == "running" and now - run.started_at > STALE_RUN_AFTER


def require_bank_page(artifact: Artifact) -> None:
    """ReviewBankInputError unless `artifact` is a ready study page (not a lab)."""
    if artifact.kind == LAB_KIND:
        raise ReviewBankInputError(f"{ERROR_PREFIX}{artifact.slug!r} is a lab, which has no questions")
    if artifact.status != "ready":
        raise ReviewBankInputError(
            f"{ERROR_PREFIX}{artifact.slug!r} is {artifact.status}, not ready"
        )


def build_review_bank(
    settings: Settings,
    slug: str,
    builder: ReviewBankBuilder,
    *,
    model: str,
    trigger: str,
) -> BankOutcome:
    """Build and store the bank of page `slug` now, recorded as a `review_bank` run (see the
    module docstring). `trigger` ("after_generation" or "on_demand") goes into the output file.
    Raises ReviewBankInputError (not a ready study page, no questions: nothing is recorded),
    ReviewBankBusyError (a build is running: nothing is recorded), and every other failure
    after recording it on the run. No database session is open during the model call."""
    now = utcnow()
    with session_scope(settings) as session:
        artifact = ArtifactManager(session, settings).get_by_slug(slug)
        require_bank_page(artifact)
        questions = page_questions(session, settings, artifact)
        if not questions:
            raise ReviewBankInputError(f"{ERROR_PREFIX}{slug!r} has no questions to vary")
        runs = GenerationRunManager(session, settings)
        latest = runs.latest(artifact, REVIEW_BANK_STAGE)
        if latest is not None and latest.status == "running":
            if not _is_stale(latest, now):
                raise ReviewBankBusyError(
                    f"{ERROR_PREFIX}a build of this page's variants is already running "
                    f"(started {latest.started_at.isoformat()} UTC)"
                )
            runs.fail(latest, "interrupted: the build never finished (the server probably "
                      "stopped while it ran)")
        run_id = runs.start(artifact, stage=REVIEW_BANK_STAGE, model=model).id
        artifact_id = artifact.id
        path = ArtifactManager(session, settings).dir(artifact) / OUTPUT_DIR / BANK_FILE
    tokens: tuple[int | None, int | None] = (None, None)
    try:
        builder.check_available()
        with cost_scope(
            settings, purpose="review_bank", artifact_id=artifact_id, generation_run_id=run_id
        ):
            result = builder.build(questions, model=model)
        tokens = (result.input_tokens, result.output_tokens)
        _write(path, {
            **result.record,
            "trigger": trigger,
            "stage": REVIEW_BANK_STAGE,
            "questions": [{"number": q.number, "block": q.key, "type": q.type} for q in questions],
            "variants": [
                {"question": v.number, "block": questions[v.number - 1].key, "content": v.content}
                for v in result.variants
            ],
        })
        with session_scope(settings) as session:
            run = session.get(GenerationRun, run_id)
            if run is None:
                raise ReviewBankInputError(f"{ERROR_PREFIX}{slug!r} was deleted during the build")
            ids = [q.block_id for q in questions]
            session.execute(delete(ReviewVariant).where(ReviewVariant.block_id.in_(ids)))
            positions: dict[int, int] = {}
            for variant in result.variants:
                block_id = questions[variant.number - 1].block_id
                positions[block_id] = positions.get(block_id, 0) + 1
                session.add(ReviewVariant(
                    block_id=block_id, position=positions[block_id], type=variant.type,
                    content=variant.content, model=result.model, run_id=run_id,
                ))
            GenerationRunManager(session, settings).succeed(
                run, input_tokens=tokens[0], output_tokens=tokens[1], output_file=path
            )
    except BaseException as exc:
        if isinstance(exc, ReviewBankError) and exc.tokens != (None, None):
            tokens = exc.tokens
        with session_scope(settings) as session:
            run = session.get(GenerationRun, run_id)
            if run is not None and run.status == "running":
                GenerationRunManager(session, settings).fail(
                    run, _error_text(exc), input_tokens=tokens[0], output_tokens=tokens[1]
                )
        raise
    return BankOutcome(len(questions), len(result.variants), run_id)


def _write(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_after_generation(
    settings: Settings, slug: str, builder: ReviewBankBuilder, generator: Any
) -> str | None:
    """The background step after a study page's generation: build its bank if the page is now
    ready and has questions; never raises. Returns "built", "failed" (recorded on the run), or
    None when there was nothing to build (the generation failed, the page has no questions, or
    it is gone; logged)."""
    from teacher.generation import generation_model  # generation imports the managers

    try:
        with session_scope(settings) as session:
            artifact = ArtifactManager(session, settings).get_by_slug(slug)
            if artifact.kind == LAB_KIND or artifact.status != "ready":
                logger.info("review bank of %s skipped: the page is %s", slug, artifact.status)
                return None
            if not page_questions(session, settings, artifact):
                logger.info("review bank of %s skipped: the page has no questions", slug)
                return None
            model = generation_model(generator, dict(artifact.params or {}))
    except Exception:
        logger.exception("review bank of %s could not start", slug)
        return None
    try:
        build_review_bank(settings, slug, builder, model=model, trigger="after_generation")
    except Exception as exc:
        logger.error("review bank of %s failed: %s", slug, exc)
        return "failed"
    return "built"


def variant_counts(session: Session, block_ids: Sequence[int]) -> dict[int, int]:
    """Per block id: how many variants it has (blocks without any are left out)."""
    if not block_ids:
        return {}
    return dict(session.execute(
        select(ReviewVariant.block_id, func.count())
        .where(ReviewVariant.block_id.in_(list(block_ids)))
        .group_by(ReviewVariant.block_id)
    ).all())


def bank_status(
    session: Session, settings: Settings, artifact: Artifact, *, now: datetime
) -> dict[str, Any]:
    """The bank of one page, for people: `status` is one of
    - "built": the latest build succeeded and the questions have their variants;
    - "building": a build is running;
    - "failed": the latest build failed (`error`); `variants` may still hold an older bank;
    - "interrupted": a build never finished (the server stopped while it ran);
    - "not_built": no build was ever recorded (a page made before the review queue, or by a
      server without a bank builder);
    - "no_questions": the page has no questions (nothing to vary);
    plus `questions`, `variants`, `questions_with_variants`, `built_at` (of the variants in
    use), `run` (the latest run: id, status, model, started_at, finished_at, error) and
    `reason`, a sentence saying what that means for the reviews."""
    questions = page_questions(session, settings, artifact) if artifact.status == "ready" else []
    counts = variant_counts(session, [q.block_id for q in questions])
    variants = sum(counts.values())
    built_at = session.scalar(
        select(func.max(ReviewVariant.created_at))
        .where(ReviewVariant.block_id.in_([q.block_id for q in questions]))
    ) if questions else None
    run = GenerationRunManager(session, settings).latest(artifact, REVIEW_BANK_STAGE)
    older = (
        f" The variants built on {built_at:%Y-%m-%d} are still used." if variants else
        " Reviews ask the original questions only."
    )
    if not questions:
        status, reason = "no_questions", "This page has no questions, so it has nothing to review."
    elif run is None:
        status = "not_built"
        reason = (
            "No variants have been built for this page yet, so reviews ask its original "
            "questions only."
        )
    elif run.status == "running" and not _is_stale(run, now):
        status, reason = "building", "The variants are being built now." + older
    elif run.status == "running":
        status = "interrupted"
        reason = "The last build of the variants never finished (the server stopped while it ran)." + older
    elif run.status == "failed":
        status = "failed"
        reason = f"The last build of the variants failed: {run.error}." + older
    else:
        status = "built"
        missing = len(questions) - len(counts)
        reason = (
            f"Every question has {VARIANTS_PER_QUESTION} variants."
            if not missing else
            f"{missing} of the page's {len(questions)} questions have no variants (they were "
            "added after the build); those are asked in their original form."
        )
    return {
        "status": status,
        "questions": len(questions),
        "variants": variants,
        "questions_with_variants": len(counts),
        "built_at": built_at,
        "run": None if run is None else {
            "id": run.id,
            "status": run.status,
            "model": run.model,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "error": run.error,
        },
        "reason": reason,
    }


__all__ = [
    "BANK_FILE",
    "BANK_FORMAT",
    "CORRECT_LENGTH_RATIO",
    "MAX_REQUIRED_CRITERIA",
    "REVIEW_BANK_STAGE",
    "STALE_RUN_AFTER",
    "SYSTEM_PROMPT",
    "VARIANTS_PER_QUESTION",
    "BankBuild",
    "BankOutcome",
    "BankQuestion",
    "BankVariant",
    "ClaudeReviewBankBuilder",
    "ReviewBankBuilder",
    "ReviewBankBusyError",
    "ReviewBankError",
    "ReviewBankInputError",
    "ReviewBankOutputError",
    "ReviewBankUnavailableError",
    "StubReviewBankBuilder",
    "WireBank",
    "bank_status",
    "build_after_generation",
    "build_review_bank",
    "build_user_prompt",
    "check_bank",
    "correct_option_violation",
    "page_questions",
    "require_bank_page",
    "rubric_violation",
    "variant_counts",
    "variants_from_wire",
]
