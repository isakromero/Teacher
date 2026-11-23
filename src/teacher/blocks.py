"""Block content schemas and the public/private split.

A block's content is the dict stored by `ArtifactBlockManager.add(content=...)`. Everything the
reader must not see before clearing a gate lives under the top-level `"private"` key:

| type              | gate | public fields                          | private fields                  |
|-------------------|------|----------------------------------------|---------------------------------|
| `prose`           | no   | `markdown`, `role` (optional)          | none (no `private` key)         |
| `multiple_choice` | yes  | `question`, `options` (2..8)           | `correct_index`, `explanation`, |
|                   |      |                                        | `option_explanations` (optional)|
| `blurb`           | yes  | `prompt`, `min_words` (default 20)     | `model_answer`, `rubric`        |
| `prediction`      | yes  | `prompt`, `options` (2..4)             | `correct_index`, `resolution`   |
| `chart`           | no   | `kind`, `title`, `x_label`, `y_label`, | none (no `private` key)         |
|                   |      | `series`, `caption`, `source`          |                                 |
| `interactable`    | no   | `title`, `instructions`, `html`        | none (no `private` key)         |
| `figure`          | no   | `asset`, `label`, `source_caption`,    | none (no `private` key)         |
|                   |      | `page`, `width`, `height`, `caption`   |                                 |

A prose `role` is `"explanation"` (the source's own account; what a block without `role` is) or
`"critique"` (the one place a page may go beyond the source: outside context, criticism, open
questions; the reader is shown it set apart and labelled as such). `prose_role` reads it. An
absent `role` stays absent: pages stored before roles existed keep their shape.

A multiple-choice `option_explanations` is a list with exactly one non-blank string per option,
in option order: the correct option's entry says why it is right, a wrong option's entry says
why that option is wrong without revealing which option is correct (it is shown to a reader who
picked that option and must try again). Pages stored before it existed have no such key.

A blurb `rubric` is a non-empty list of `{"criterion": <non-blank str>, "required": <bool,
default true>}`. A blurb answer passes when every required criterion is met.

A prediction asks the reader to commit to a guess (2 to 4 non-blank `options`) before the
explanation that follows; `resolution` (non-blank, one to three sentences) says what actually
holds and why, and `correct_index` names the option that matches it. It is a gate only so that
the reader commits before reading on: any choice clears it, and it is not a test, so it never
counts towards pass rates, scores or the reader's standing (`is_question_type` is false for it).

A chart is drawn by the page from its data. `kind` is `"line"`, `"bar"` or `"scatter"`;
`series` is a list of 1..6 (at most 3 for scatter) `{"name": <str>, "points": [{"x": <number
or str>, "y": <number>}, ...]}` with 1..60 points each and names unique ignoring case and
surrounding whitespace. Every `y` is a finite number. In a bar chart every `x` is a category
name (a non-blank string, not repeated within a series, written identically in every series,
at most 24 distinct across the chart); in a line or scatter chart every `x` is a finite number,
and a line series has at least 2 points with no `x` repeated. `caption` is Markdown and may be
blank (the chart is then drawn without one); the other text fields are non-blank. Ints are accepted as numbers (and stored as floats); bools are not.

An interactable is a small widget: `instructions` (Markdown) and `html`, a self-contained
fragment of markup, `<style>` and `<script>` (at most `INTERACTABLE_HTML_MAX` characters) that
the page runs in an `<iframe sandbox="allow-scripts">` with an opaque origin and a CSP of
`default-src 'none'` plus inline script and style. `html` must contain a `<script>`, and every
construct in `SANDBOX_RULES` (document-level tags, external resources, network and storage
APIs, modal dialogs, the channel to the embedding page) is rejected with the reason it cannot
work there.

A figure is an image cropped from the source PDF (`teacher.figures`): `asset` is the PNG's path
relative to the artifact's `assets/` folder (relative segments of letters, digits, `.`, `_` and
`-`, no `..`, ending in `.png`), served by `GET /api/artifacts/{slug}/assets/{asset}`; `label` is
the figure's label in the source ("Figure 3"); `source_caption` is the source's own caption
text; `page` is the PDF page it is on (from 1); `width` and `height` are the image's size in
pixels (so the page can reserve its space before it loads); `caption` (Markdown) is the
writer's own: what the reader should look at and why. Every text field is non-blank.

Validation is strict: unknown keys are rejected and values are not coerced (`"1"` is not an int).
Optional fields without a default (`role`, `option_explanations`) may be left out but not given
as null, and are left out of the normalised content when absent.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

PRIVATE_KEY = "private"

# String fields of block content that are not reader-visible prose: an enum value (a chart's
# `kind`) and widget source code (an interactable's `html`). The glossary skips them, so the
# glossary step is not shown widget code and a term does not count as "on the page" because it
# appears in a script or because a chart is a "bar" chart.
# A prose block's `role` is an enum value too, and a figure's `asset` is a file path.
NON_TEXT_KEYS: frozenset[str] = frozenset({"kind", "html", "role", "asset"})

ProseRole = Literal["explanation", "critique"]
PROSE_ROLES: tuple[str, ...] = get_args(ProseRole)
PREDICTION_MIN_OPTIONS = 2
PREDICTION_MAX_OPTIONS = 4

ChartKind = Literal["line", "bar", "scatter"]
CHART_KINDS: tuple[str, ...] = get_args(ChartKind)
CHART_MAX_SERIES = 6
SCATTER_MAX_SERIES = 3  # more colours cannot be told apart among scattered points
CHART_MAX_POINTS = 60  # per series
BAR_MAX_CATEGORIES = 24  # distinct categories across all series of a bar chart
LINE_MIN_POINTS = 2
INTERACTABLE_HTML_MAX = 30000  # characters
# A figure's asset: relative path segments, no "..", a PNG.
FIGURE_ASSET_RE = re.compile(r"^(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\.png$")


class _Content(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _check_not_blank(value: str, info: Any) -> str:
    if not value.strip():
        raise ValueError(f"{info.field_name} must not be blank")
    return value


def _not_null(value: Any, info: Any) -> Any:
    """For an optional field that has no default value: it may be left out, not given as null."""
    if value is None:
        raise ValueError(f"{info.field_name} may be left out but must not be null")
    return value


def _check_correct_index(correct_index: int, options: list[str]) -> None:
    if correct_index >= len(options):
        raise ValueError(
            f"correct_index {correct_index} is out of range for {len(options)} options"
        )


class ProseContent(_Content):
    markdown: str = Field(min_length=1)
    role: ProseRole | None = None  # None: absent, which means "explanation"

    _role_not_null = field_validator("role", mode="before")(_not_null)


class MultipleChoicePrivate(_Content):
    correct_index: int = Field(ge=0)
    explanation: str
    option_explanations: list[str] | None = None  # None: absent (a page stored before it)

    _explanations_not_null = field_validator("option_explanations", mode="before")(_not_null)

    @field_validator("option_explanations")
    @classmethod
    def _explanations_not_blank(cls, value: list[str] | None) -> list[str] | None:
        for number, text in enumerate(value or [], start=1):
            if not text.strip():
                raise ValueError(f"option_explanations entry {number} must not be blank")
        return value


class MultipleChoiceContent(_Content):
    question: str = Field(min_length=1)
    options: list[str] = Field(min_length=2, max_length=8)
    private: MultipleChoicePrivate

    @model_validator(mode="after")
    def _check_index(self) -> MultipleChoiceContent:
        _check_correct_index(self.private.correct_index, self.options)
        explanations = self.private.option_explanations
        if explanations is not None and len(explanations) != len(self.options):
            raise ValueError(
                f"option_explanations has {len(explanations)} entries for "
                f"{len(self.options)} options; it needs exactly one per option, in option order"
            )
        return self


class RubricItem(_Content):
    criterion: str = Field(min_length=1)
    required: bool = True

    @field_validator("criterion")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("criterion must not be blank")
        return value


class BlurbPrivate(_Content):
    model_answer: str
    rubric: list[RubricItem] = Field(min_length=1)


class BlurbContent(_Content):
    prompt: str = Field(min_length=1)
    min_words: int = Field(default=20, ge=1)
    private: BlurbPrivate


class PredictionPrivate(_Content):
    correct_index: int = Field(ge=0)
    resolution: str

    @field_validator("resolution")
    @classmethod
    def _not_blank(cls, value: str, info: Any) -> str:
        return _check_not_blank(value, info)


class PredictionContent(_Content):
    prompt: str
    options: list[str] = Field(
        min_length=PREDICTION_MIN_OPTIONS, max_length=PREDICTION_MAX_OPTIONS
    )
    private: PredictionPrivate

    @field_validator("prompt")
    @classmethod
    def _not_blank(cls, value: str, info: Any) -> str:
        return _check_not_blank(value, info)

    @field_validator("options")
    @classmethod
    def _options_not_blank(cls, value: list[str]) -> list[str]:
        for number, option in enumerate(value, start=1):
            if not option.strip():
                raise ValueError(f"option {number} must not be blank")
        return value

    @model_validator(mode="after")
    def _check_index(self) -> PredictionContent:
        _check_correct_index(self.private.correct_index, self.options)
        return self


# -- chart ----------------------------------------------------------------------------------


class ChartPoint(_Content):
    # Strict mode accepts an int for a float (stored as a float) and rejects a bool; in the
    # union a string stays a string, so "3" is a category name and never the number 3.
    x: float | str
    y: float


class ChartSeries(_Content):
    name: str
    points: list[ChartPoint]

    @field_validator("name")
    @classmethod
    def _not_blank(cls, value: str, info: Any) -> str:
        return _check_not_blank(value, info)


def _category_key(name: str) -> str:
    return name.strip().casefold()


class ChartContent(_Content):
    kind: ChartKind
    title: str
    x_label: str
    y_label: str
    series: list[ChartSeries]
    caption: str
    source: str

    # `caption` may be blank: the chart is then drawn without one, which is not worth failing a
    # finished page over.
    @field_validator("title", "x_label", "y_label", "source")
    @classmethod
    def _not_blank(cls, value: str, info: Any) -> str:
        return _check_not_blank(value, info)

    @model_validator(mode="after")
    def _check_series(self) -> ChartContent:
        kind = self.kind
        limit = SCATTER_MAX_SERIES if kind == "scatter" else CHART_MAX_SERIES
        if not 1 <= len(self.series) <= limit:
            raise ValueError(f"a {kind} chart needs 1 to {limit} series, got {len(self.series)}")
        names: dict[str, int] = {}
        # category key -> (spelling, the series it was first seen in), across the whole chart
        categories: dict[str, tuple[str, str]] = {}
        for number, series in enumerate(self.series, start=1):
            where = f"series {number} ({series.name!r})"
            key = _category_key(series.name)
            if key in names:
                raise ValueError(
                    f"{where}: the name is already used by series {names[key]} (names must "
                    "differ ignoring case and surrounding whitespace)"
                )
            names[key] = number
            minimum = LINE_MIN_POINTS if kind == "line" else 1
            if not minimum <= len(series.points) <= CHART_MAX_POINTS:
                raise ValueError(
                    f"{where}: a {kind} series needs {minimum} to {CHART_MAX_POINTS} points, "
                    f"got {len(series.points)}"
                )
            seen: dict[Any, int] = {}
            for index, point in enumerate(series.points, start=1):
                at = f"{where}, point {index}"
                if not math.isfinite(point.y):
                    raise ValueError(f"{at}: y must be a finite number, got {point.y!r}")
                if kind == "bar":
                    self._check_category(point.x, at, where, seen, index, categories)
                else:
                    self._check_number(point.x, at, kind, seen, index)
        if kind == "bar" and len(categories) > BAR_MAX_CATEGORIES:
            raise ValueError(
                f"a bar chart has at most {BAR_MAX_CATEGORIES} distinct categories, "
                f"got {len(categories)}"
            )
        return self

    @staticmethod
    def _check_category(
        x: float | str,
        at: str,
        where: str,
        seen: dict[Any, int],
        index: int,
        categories: dict[str, tuple[str, str]],
    ) -> None:
        if not isinstance(x, str):  # a ValueError, which pydantic reports as a validation error
            raise ValueError(  # noqa: TRY004
                f"{at}: in a bar chart x is a category name (a string), got the number {x!r}"
            )
        if not x.strip():
            raise ValueError(f"{at}: the category name (x) must not be blank")
        key = _category_key(x)
        if key in seen:
            raise ValueError(
                f"{at}: category {x!r} is repeated (point {seen[key]} has it already); a bar "
                "series has one value per category"
            )
        seen[key] = index
        first = categories.setdefault(key, (x, where))
        if first[0] != x:
            raise ValueError(
                f"{at}: category {x!r} is written {first[0]!r} in {first[1]}; a category must "
                "be written identically in every series"
            )

    @staticmethod
    def _check_number(
        x: float | str, at: str, kind: str, seen: dict[Any, int], index: int
    ) -> None:
        if isinstance(x, str):  # a ValueError, which pydantic reports as a validation error
            raise ValueError(  # noqa: TRY004
                f"{at}: in a {kind} chart x must be a number, got the string {x!r}"
            )
        if not math.isfinite(x):
            raise ValueError(f"{at}: x must be a finite number, got {x!r}")
        if kind == "line":
            if x in seen:
                raise ValueError(
                    f"{at}: x {x!r} is repeated (point {seen[x]} has it already); a line has "
                    "one y per x"
                )
            seen[x] = index


# -- interactable ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxRule:
    """A construct an interactable's `html` may not contain, and why it is rejected."""

    construct: str
    pattern: re.Pattern[str]
    reason: str


def _tags(*names: str) -> re.Pattern[str]:
    """An opening or closing tag of one of `names` (HTML tag names end at whitespace, `/` or
    `>`, so `<header` is not `<head` and `i<link.length` is not a tag)."""
    return re.compile(rf"</?(?:{'|'.join(names)})(?=[\s/>])", re.IGNORECASE)


def _global_call(names: str, *, space: bool = True) -> re.Pattern[str]:
    """A call of a global function (bare, or through `window.`, `self.` or `globalThis.`), not
    a method of some other object (`queue.fetch(`) or the end of a longer name (`showPrompt(`).
    With `space=False` no whitespace may precede the parenthesis, so prose such as "a prompt
    (shown above)" in the widget's text is not taken for a call."""
    before = r"(?:(?<![\w$.])|\b(?:window|self|globalThis)\s*\.\s*)"
    return re.compile(rf"{before}(?:{names}){r'\s*' if space else ''}\(")


_NO_NETWORK = "the frame has no network access, so nothing can be loaded from elsewhere"
_I = re.IGNORECASE

# Checked against the whole fragment. HTML patterns ignore case, as HTML does; JavaScript
# patterns do not, as JavaScript does not.
SANDBOX_RULES: tuple[SandboxRule, ...] = (
    SandboxRule(
        "a <!doctype>, <html>, <head> or <body> tag",
        re.compile(r"<!doctype\b|</?(?:html|head|body)(?=[\s/>])", _I),
        "the page wraps the fragment in its own document; write only what goes inside <body>",
    ),
    SandboxRule(
        "a <meta> or <base> tag",
        _tags("meta", "base"),
        "they belong in the document head, which the page provides",
    ),
    SandboxRule(
        "a <link> tag",
        _tags("link"),
        f"it loads an external resource and {_NO_NETWORK}; put styles in a <style> element",
    ),
    SandboxRule(
        "an <iframe>, <frame>, <object> or <embed> tag",
        _tags("iframe", "frame", "frameset", "object", "embed"),
        "nested frames and plugins cannot load anything inside the sandbox",
    ),
    SandboxRule(
        "a <form> tag",
        _tags("form"),
        "form submission is blocked in the sandbox; use inputs and buttons with script "
        "handlers directly",
    ),
    SandboxRule(
        "a <script src>",
        re.compile(r"<script\b[^>]*?(?<![\w-])src\s*=", _I),
        f"{_NO_NETWORK}; put the code inside the <script> element",
    ),
    SandboxRule(
        "an external URL",
        re.compile(
            r"(?<![\w-])(?:src|href)\s*=\s*[\"']?\s*(?:https?:|//)"
            r"|(?<![\w-])srcset\s*=\s*[\"']?[^\"'>]*?(?:https?:|//)"
            r"|\burl\(\s*[\"']?\s*(?:https?:|//)",
            _I,
        ),
        f"{_NO_NETWORK}; draw with inline SVG or canvas instead",
    ),
    SandboxRule(
        "an @import", re.compile(r"@import\b", _I), f"it loads a stylesheet and {_NO_NETWORK}"
    ),
    SandboxRule(
        "a link to another page",
        re.compile(r"<a(?=[\s>])[^>]*?(?<![\w-])href\s*=\s*(?![\"']?#)", _I),
        "the frame cannot navigate to another page; only links to #fragments work, and "
        "buttons suit actions",
    ),
    SandboxRule("fetch()", _global_call("fetch"), _NO_NETWORK),
    SandboxRule(
        "a network API",
        re.compile(r"\b(?:XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)\b"),
        _NO_NETWORK,
    ),
    SandboxRule(
        "a dynamic import()",
        re.compile(r"(?<![\w$.])import\s*\("),
        f"modules cannot be loaded: {_NO_NETWORK}",
    ),
    SandboxRule(
        "an import statement",
        re.compile(r"(?<![\w$.])import\b\s*(?:[\w$*{][^;'\"()]{0,300}?\bfrom\s*)?[\"']"),
        f"modules cannot be loaded: {_NO_NETWORK}",
    ),
    SandboxRule(
        "a worker",
        re.compile(r"\bnew\s+(?:Shared)?Worker\s*\(|\bserviceWorker\b"),
        "a worker loads its script from a URL, which the frame cannot do",
    ),
    SandboxRule(
        "browser storage",
        re.compile(r"\b(?:localStorage|sessionStorage|indexedDB)\b|\bdocument\s*\.\s*cookie\b"),
        "storage throws an error in a sandboxed frame with an opaque origin; keep state in "
        "variables",
    ),
    SandboxRule(
        "a modal dialog",
        _global_call("alert|confirm|prompt", space=False),
        "dialogs are blocked in the sandbox; show messages inside the widget",
    ),
    SandboxRule(
        "window.open()",
        re.compile(r"\b(?:window|self|globalThis)\s*\.\s*open\s*\("),
        "pop-up windows are blocked in the sandbox",
    ),
    SandboxRule(
        "the channel to the page",
        re.compile(r"\b(?:window|self|globalThis)\s*\.\s*(?:parent|top|opener)\b|\bpostMessage\b"),
        "the page uses the channel between itself and the frame (for example to size the "
        "frame), and the widget must leave it alone",
    ),
)

_SCRIPT_TAG = re.compile(r"<script(?=[\s/>])", re.IGNORECASE)


def _sandbox_violations(html: str) -> list[str]:
    """One message per rule of `SANDBOX_RULES` that `html` breaks, quoting the first match."""
    found = []
    for rule in SANDBOX_RULES:
        match = rule.pattern.search(html)
        if match is not None:
            snippet = " ".join(match.group(0).split())[:60]
            found.append(f"{rule.construct} ({snippet!r}): {rule.reason}")
    return found


class InteractableContent(_Content):
    title: str
    instructions: str
    html: str

    @field_validator("title", "instructions")
    @classmethod
    def _not_blank(cls, value: str, info: Any) -> str:
        return _check_not_blank(value, info)

    @field_validator("html")
    @classmethod
    def _check_html(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("html must not be blank")
        if len(value) > INTERACTABLE_HTML_MAX:
            raise ValueError(
                f"html has {len(value)} characters; at most {INTERACTABLE_HTML_MAX} are allowed"
            )
        if _SCRIPT_TAG.search(value) is None:
            raise ValueError("html has no <script> element, so the widget cannot be interactive")
        violations = _sandbox_violations(value)
        if violations:
            raise ValueError(
                "html cannot work in the sandboxed frame it runs in: " + "; ".join(violations)
            )
        return value


# -- figure ---------------------------------------------------------------------------------


class FigureContent(_Content):
    asset: str
    label: str
    source_caption: str
    page: int = Field(ge=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    caption: str

    @field_validator("label", "source_caption", "caption")
    @classmethod
    def _not_blank(cls, value: str, info: Any) -> str:
        return _check_not_blank(value, info)

    @field_validator("asset")
    @classmethod
    def _check_asset(cls, value: str) -> str:
        if not FIGURE_ASSET_RE.match(value):
            raise ValueError(
                f"asset {value!r} must be a relative path inside the artifact's assets folder "
                "(segments of letters, digits, '.', '_' and '-', no '..') ending in .png"
            )
        return value


@dataclass(frozen=True)
class BlockType:
    """One entry of the block type registry. `is_question` is whether a gate of this type tests
    the reader, so that its responses count towards pass rates, scores and standing (a
    prediction is a gate that does not)."""

    name: str
    model: type[BaseModel]
    is_gate: bool
    is_question: bool = False


BLOCK_TYPES: dict[str, BlockType] = {
    "prose": BlockType("prose", ProseContent, is_gate=False),
    "multiple_choice": BlockType(
        "multiple_choice", MultipleChoiceContent, is_gate=True, is_question=True
    ),
    "blurb": BlockType("blurb", BlurbContent, is_gate=True, is_question=True),
    "prediction": BlockType("prediction", PredictionContent, is_gate=True),
    "chart": BlockType("chart", ChartContent, is_gate=False),
    "interactable": BlockType("interactable", InteractableContent, is_gate=False),
    "figure": BlockType("figure", FigureContent, is_gate=False),
}


def _block_type(type: str) -> BlockType:
    try:
        return BLOCK_TYPES[type]
    except (KeyError, TypeError):
        raise ValueError(
            f"unknown block type {type!r}: expected one of {sorted(BLOCK_TYPES)}"
        ) from None


def validate_block_content(type: str, content: dict[str, Any]) -> dict[str, Any]:
    """Validate `content` for block `type`; return the normalised dict (defaults filled in).

    Raises ValueError (pydantic's ValidationError is one) for an unknown type or bad content.
    """
    model = _block_type(type).model
    if not isinstance(content, dict):
        raise ValueError(f"{type} block content must be a dict, got {content.__class__.__name__}")
    try:
        # exclude_none: no stored field is ever null, so a None here is an optional field that
        # was left out (see the module docstring), and it stays out.
        return model.model_validate(content).model_dump(exclude_none=True)
    except ValidationError as exc:
        raise ValueError(f"invalid {type} block content: {exc}") from exc


def public_content(content: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of `content` without the `private` key (safe to show before the gate clears)."""
    return {k: copy.deepcopy(v) for k, v in content.items() if k != PRIVATE_KEY}


def private_content(content: dict[str, Any]) -> dict[str, Any] | None:
    """A deep copy of the `private` part of `content`, or None if it has none."""
    private = content.get(PRIVATE_KEY)
    return copy.deepcopy(private) if private is not None else None


def is_gate_type(type: str) -> bool:
    """Whether blocks of `type` are gates; ValueError for an unknown type."""
    return _block_type(type).is_gate


def is_question_type(type: str) -> bool:
    """Whether blocks of `type` are questions that test the reader, whose responses count
    towards pass rates, scores and standing: multiple choice and blurb, not a prediction (a gate
    that any guess clears) and not a block that is no gate. ValueError for an unknown type."""
    return _block_type(type).is_question


def prose_role(content: dict[str, Any]) -> str:
    """A prose block's role: `"explanation"` (also when the block has none) or `"critique"`."""
    role = content.get("role", "explanation")
    if role not in PROSE_ROLES:
        raise ValueError(f"unknown prose role {role!r}: expected one of {list(PROSE_ROLES)}")
    return role


# -- labels for people ----------------------------------------------------------------------

LABEL_MAX = 60  # characters, the ellipsis included

_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_MARKUP = (
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),  # an image: its alt text
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # a link: its text
    (re.compile(r"<[^>]+>"), " "),  # an HTML tag
    (re.compile(r"(?m)^ {0,3}(?:>[ \t]?|[-*+][ \t]+|\d+[.)][ \t]+|#{1,6}[ \t]+)"), ""),
    (re.compile(r"[*_`~]+"), ""),  # emphasis and code marks
)


def _plain(markdown: str) -> str:
    for pattern, replacement in _MARKUP:
        markdown = pattern.sub(replacement, markdown)
    return " ".join(markdown.split())


def _shorten(text: str) -> str:
    if len(text) <= LABEL_MAX:
        return text
    cut = text[: LABEL_MAX - 1]
    if " " in cut[LABEL_MAX // 2 :]:  # end at a word when one ends in the second half
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def _prose_label(markdown: str) -> str:
    """The first heading of a prose block, else its opening words (code blocks skipped, unless
    there is nothing else)."""
    fence: str | None = None
    text: list[str] = []
    for line in markdown.splitlines():
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
            continue
        if opened:
            fence = opened.group(1)
            continue
        heading = _HEADING.match(line)
        if heading and _plain(heading.group(1)):
            return _plain(heading.group(1))
        text.append(line)
    return _plain("\n".join(text)) or _plain(markdown)


def plain_text(markdown: str) -> str:
    """Markdown as plain words on one line: link and image text kept, tags, list and quote
    markers, heading marks and emphasis removed, whitespace collapsed."""
    return _plain(markdown)


def markdown_sections(markdown: str) -> list[tuple[str | None, str]]:
    """A prose block's Markdown split at its headings, as `(heading, text)` pairs in order, both
    plain (`plain_text`). The text before the first heading has heading None; a heading line
    inside a code block is text, not a heading. Pairs whose heading and text are both empty are
    left out."""
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    fence: str | None = None
    for line in markdown.splitlines():
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
            sections[-1][1].append(line)
            continue
        if opened:
            fence = opened.group(1)
            sections[-1][1].append(line)
            continue
        heading = _HEADING.match(line)
        if heading and _plain(heading.group(1)):
            sections.append((_plain(heading.group(1)), []))
            continue
        sections[-1][1].append(line)
    out = [(heading, _plain("\n".join(lines))) for heading, lines in sections]
    return [(heading, text) for heading, text in out if heading or text]


def block_label(type: str, content: dict[str, Any]) -> str:
    """A short name for a block, for people (the study history): a prose block's first heading
    or opening words, a question's text, a chart's or interactable's title, a figure's label and
    the writer's caption ("Figure 3: ..."), shortened to at most `LABEL_MAX` characters. Only
    public fields are read, never the `private` part. ValueError for an unknown type."""
    _block_type(type)
    public = public_content(content)
    if type == "figure":
        label, caption = public.get("label"), public.get("caption")
        if not isinstance(label, str) or not isinstance(caption, str):
            raise ValueError("a figure block's content has no label and caption to name it by")
        text = ": ".join(part for part in (_plain(label), _plain(caption)) if part)
        return _shorten(text) if text else "(a figure block without text)"
    field = {
        "prose": "markdown",
        "multiple_choice": "question",
        "blurb": "prompt",
        "prediction": "prompt",
        "chart": "title",
        "interactable": "title",
    }[type]
    value = public.get(field)
    if not isinstance(value, str):
        raise ValueError(f"a {type} block's content has no {field} text to name it by")
    text = _prose_label(value) if type == "prose" else _plain(value)
    return _shorten(text) if text else f"(a {type.replace('_', ' ')} block without text)"


__all__ = [
    "BAR_MAX_CATEGORIES",
    "BLOCK_TYPES",
    "CHART_KINDS",
    "CHART_MAX_POINTS",
    "CHART_MAX_SERIES",
    "FIGURE_ASSET_RE",
    "INTERACTABLE_HTML_MAX",
    "LABEL_MAX",
    "LINE_MIN_POINTS",
    "NON_TEXT_KEYS",
    "PREDICTION_MAX_OPTIONS",
    "PREDICTION_MIN_OPTIONS",
    "PRIVATE_KEY",
    "PROSE_ROLES",
    "SANDBOX_RULES",
    "SCATTER_MAX_SERIES",
    "BlockType",
    "BlurbContent",
    "BlurbPrivate",
    "ChartContent",
    "ChartPoint",
    "ChartSeries",
    "FigureContent",
    "InteractableContent",
    "MultipleChoiceContent",
    "MultipleChoicePrivate",
    "PredictionContent",
    "PredictionPrivate",
    "ProseContent",
    "ProseRole",
    "RubricItem",
    "SandboxRule",
    "block_label",
    "is_gate_type",
    "is_question_type",
    "markdown_sections",
    "plain_text",
    "private_content",
    "prose_role",
    "public_content",
    "validate_block_content",
]
