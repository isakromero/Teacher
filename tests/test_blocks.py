"""teacher.blocks: content validation, registry, public/private split."""

from __future__ import annotations

import math
from typing import Any

import pytest

from teacher.blocks import (
    BLOCK_TYPES,
    LABEL_MAX,
    NON_TEXT_KEYS,
    block_label,
    is_gate_type,
    is_question_type,
    private_content,
    prose_role,
    public_content,
    validate_block_content,
)


def mc(**overrides: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "question": "2 + 2?",
        "options": ["3", "4", "5"],
        "private": {"correct_index": 1, "explanation": "Arithmetic."},
    }
    content.update(overrides)
    return content


def blurb(**overrides: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "prompt": "Explain attention.",
        "private": {
            "model_answer": "It weighs tokens.",
            "rubric": [
                {"criterion": "mentions weights", "required": True},
                {"criterion": "gives an example", "required": False},
            ],
        },
    }
    content.update(overrides)
    return content


def prediction(**overrides: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "prompt": "If you double the key dimension, what happens to raw dot products?",
        "options": ["They shrink", "They grow", "They stay the same"],
        "private": {"correct_index": 1, "resolution": "Their variance grows with the dimension."},
    }
    content.update(overrides)
    return content


def points(*pairs: tuple[Any, Any]) -> list[dict[str, Any]]:
    return [{"x": x, "y": y} for x, y in pairs]


def chart(kind: str = "bar", **overrides: Any) -> dict[str, Any]:
    if kind == "bar":
        series = [
            {"name": "Baseline", "points": points(("BLEU", 27.3), ("ROUGE", 40.1))},
            {"name": "Ours", "points": points(("BLEU", 28.4), ("ROUGE", 41.0))},
        ]
    else:
        series = [{"name": "Train loss", "points": points((1, 2.5), (2, 1.75), (4.5, 1.2))}]
    content: dict[str, Any] = {
        "kind": kind,
        "title": "Scores by metric",
        "x_label": "Metric",
        "y_label": "Score (points)",
        "series": series,
        "caption": "Ours is **ahead** on both metrics.",
        "source": "Table 2",
    }
    content.update(overrides)
    return content


def series(name: str, *pairs: tuple[Any, Any]) -> dict[str, Any]:
    return {"name": name, "points": points(*pairs)}


WIDGET = """\
<div class="w"><label>n <input id="n" type="range" min="1" max="9" value="3"></label>
<output id="out">9</output></div>
<style>.w { color: var(--fg); }</style>
<script>
const n = document.getElementById("n");
n.addEventListener("input", () => { out.textContent = String(n.value * n.value); });
</script>"""


def interactable(**overrides: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "title": "Squares",
        "instructions": "Move the slider and watch $n^2$.",
        "html": WIDGET,
    }
    content.update(overrides)
    return content


def with_script(code: str) -> dict[str, Any]:
    """An interactable whose html is a valid widget plus `code` (markup or script)."""
    return interactable(html=f"{WIDGET}\n{code}")


def test_registry() -> None:
    assert set(BLOCK_TYPES) == {
        "prose", "multiple_choice", "blurb", "prediction", "chart", "interactable", "figure"
    }
    assert is_gate_type("multiple_choice") and is_gate_type("blurb")
    assert is_gate_type("prediction")
    assert not is_gate_type("prose")
    assert not is_gate_type("chart") and not is_gate_type("interactable")
    assert not is_gate_type("figure") and not is_question_type("figure")
    assert NON_TEXT_KEYS == {"kind", "html", "role", "asset"}
    with pytest.raises(ValueError, match="unknown block type"):
        is_gate_type("video")


def test_questions_are_the_gates_that_test_the_reader() -> None:
    # A prediction unlocks what follows but is not a question: it never counts as a pass/fail.
    assert is_question_type("multiple_choice") and is_question_type("blurb")
    for type_ in ("prediction", "prose", "chart", "interactable"):
        assert not is_question_type(type_)
    with pytest.raises(ValueError, match="unknown block type"):
        is_question_type("video")


def test_prose_role_is_optional_and_stays_absent() -> None:
    # Old pages have no role; it is not filled in, and reads as an explanation.
    plain = validate_block_content("prose", {"markdown": "x"})
    assert plain == {"markdown": "x"}
    assert prose_role(plain) == "explanation"
    for role in ("explanation", "critique"):
        out = validate_block_content("prose", {"markdown": "x", "role": role})
        assert out == {"markdown": "x", "role": role}
        assert prose_role(out) == role
    with pytest.raises(ValueError, match="'explanation' or 'critique'"):
        validate_block_content("prose", {"markdown": "x", "role": "opinion"})
    with pytest.raises(ValueError, match="role may be left out but must not be null"):
        validate_block_content("prose", {"markdown": "x", "role": None})
    with pytest.raises(ValueError, match="unknown prose role"):
        prose_role({"markdown": "x", "role": "opinion"})


def test_option_explanations_are_optional_one_per_option() -> None:
    assert "option_explanations" not in validate_block_content("multiple_choice", mc())["private"]
    explained = mc(
        private={
            "correct_index": 1,
            "explanation": "Arithmetic.",
            "option_explanations": ["One short.", "Two and two make four.", "One too many."],
        }
    )
    assert validate_block_content("multiple_choice", explained) == explained
    assert "option_explanations" not in public_content(explained)


@pytest.mark.parametrize(
    ("explanations", "fragment"),
    [
        (["a", "b"], "has 2 entries for 3 options"),
        (["a", "b", "c", "d"], "has 4 entries for 3 options"),
        ([], "has 0 entries for 3 options"),
        (["a", " ", "c"], "entry 2 must not be blank"),
        (["a", "", "c"], "entry 2 must not be blank"),
        (["a", 2, "c"], "valid string"),
        (None, "must not be null"),
        ("abc", "valid list"),
    ],
)
def test_invalid_option_explanations(explanations: Any, fragment: str) -> None:
    private = {"correct_index": 1, "explanation": "x", "option_explanations": explanations}
    with pytest.raises(ValueError, match="invalid multiple_choice block content") as caught:
        validate_block_content("multiple_choice", mc(private=private))
    assert fragment in str(caught.value)


def test_prediction_is_valid_and_splits_public_and_private() -> None:
    assert validate_block_content("prediction", prediction()) == prediction()
    two = prediction(options=["Up", "Down"], private={"correct_index": 0, "resolution": "Up."})
    assert validate_block_content("prediction", two) == two
    assert public_content(prediction()) == {
        "prompt": prediction()["prompt"],
        "options": prediction()["options"],
    }
    assert private_content(prediction()) == prediction()["private"]
    assert block_label("prediction", prediction()).startswith("If you double the key")


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        (prediction(options=["only one"]), "at least 2 items"),
        (prediction(options=["a", "b", "c", "d", "e"]), "at most 4 items"),
        (prediction(options=["a", " "]), "option 2 must not be blank"),
        (prediction(prompt=" "), "prompt must not be blank"),
        (prediction(private={"correct_index": 3, "resolution": "r"}), "out of range"),
        (prediction(private={"correct_index": -1, "resolution": "r"}), "greater than or equal"),
        (prediction(private={"correct_index": True, "resolution": "r"}), "valid integer"),
        (prediction(private={"correct_index": 1, "resolution": ""}), "resolution must not be"),
        (prediction(private={"correct_index": 1}), "resolution"),
        (prediction(private={"correct_index": 1, "resolution": "r", "explanation": "x"}), "extra"),
        ({k: v for k, v in prediction().items() if k != "private"}, "private"),
    ],
)
def test_invalid_predictions(content: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValueError, match="invalid prediction block content") as caught:
        validate_block_content("prediction", content)
    assert fragment in str(caught.value)


def test_valid_contents_are_normalised() -> None:
    assert validate_block_content("prose", {"markdown": "# Hi"}) == {"markdown": "# Hi"}
    assert validate_block_content("multiple_choice", mc()) == mc()
    out = validate_block_content("blurb", blurb())
    assert out["min_words"] == 20  # default filled in
    assert out["private"] == blurb()["private"]
    assert validate_block_content("blurb", blurb(min_words=5))["min_words"] == 5


@pytest.mark.parametrize("kind", ["bar", "line", "scatter"])
def test_valid_charts_are_normalised(kind: str) -> None:
    out = validate_block_content("chart", chart(kind))
    expected = chart(kind)
    if kind != "bar":  # numbers are stored as floats; an int is accepted for one
        expected["series"][0]["points"] = points((1.0, 2.5), (2.0, 1.75), (4.5, 1.2))
    assert out == expected
    assert "private" not in out


def test_chart_numbers_ints_strings_and_bools() -> None:
    line = validate_block_content("chart", chart("line", series=[series("a", (3, 1), (4, -2))]))
    xs = [(p["x"], p["y"]) for p in line["series"][0]["points"]]
    assert xs == [(3.0, 1.0), (4.0, -2.0)]
    assert all(isinstance(v, float) for pair in xs for v in pair)
    # "3" stays a string: a category name in a bar chart, never coerced to a number
    bar = validate_block_content("chart", chart(series=[series("a", ("3", 1), ("4", 2))]))
    assert [p["x"] for p in bar["series"][0]["points"]] == ["3", "4"]
    with pytest.raises(ValueError, match="x must be a number, got the string '3'"):
        validate_block_content("chart", chart("line", series=[series("a", ("3", 1), (4, 2))]))
    with pytest.raises(ValueError, match="valid number"):
        validate_block_content("chart", chart("line", series=[series("a", (1, True), (2, 1))]))
    with pytest.raises(ValueError, match="valid number"):
        validate_block_content("chart", chart("scatter", series=[series("a", (False, 1))]))
    with pytest.raises(ValueError, match="valid number"):
        validate_block_content("chart", chart(series=[series("a", (True, 1))]))


def test_chart_errors_name_the_series_and_point() -> None:
    bad = chart(series=[series("Baseline", ("BLEU", 1)), series("Ours", ("BLEU", math.nan))])
    with pytest.raises(ValueError, match=r"series 2 \('Ours'\), point 1: y must be a finite"):
        validate_block_content("chart", bad)
    bad = chart("line", series=[series("a", (1, 1), (2, 2), (1, 3))])
    with pytest.raises(ValueError, match=r"series 1 \('a'\), point 3: x 1.0 is repeated"):
        validate_block_content("chart", bad)


def test_interactable_is_valid() -> None:
    assert validate_block_content("interactable", interactable()) == interactable()
    upper = interactable(html="<p>Hi</p><SCRIPT>let x = 1;</SCRIPT>")
    assert validate_block_content("interactable", upper) == upper
    longest = interactable(html="<script>let a = 1;</script>" + "x" * (30000 - 27))
    assert validate_block_content("interactable", longest) == longest


@pytest.mark.parametrize(
    "code",
    [
        "<script>const parent = document.body; parent.appendChild(el);</script>",
        "<script>el.style.top = '10px'; box.top = 3;</script>",
        "<script>function showPrompt(t) { msg.textContent = t; } showPrompt('x');</script>",
        "<script>const fetchData = () => 1; queue.fetch(1); myAlert(); this.confirm();</script>",
        "<script>for (let i = 0; i<link.length; i++) {}; if (a<body) {}</script>",
        "<script>const important = 'x from \"y\"'; window.topBar = 1;</script>",
        "<header>h</header><svg><title>t</title><use href='#dot'/></svg>",
        "<a href='#top'>back</a><a href=\"#sec\">sec</a><abbr title='x'>x</abbr>",
        '<svg><rect fill="url(#grad)"/></svg><style>.a { fill: url(#g); top: 0; }</style>',
        "<p>Enter a prompt (any text), then confirm (below) to raise an alert (red).</p>",
        "<p>the formula, the link, the body of the proof, a form of the bound</p>",
        "<script>// see the paper: https://example.org\nconst src = 'local';</script>",
    ],
)
def test_interactable_sandbox_false_positives_are_accepted(code: str) -> None:
    content = with_script(code)
    assert validate_block_content("interactable", content) == content


def test_interactable_rejection_names_the_construct_and_why() -> None:
    with pytest.raises(ValueError) as caught:
        validate_block_content(
            "interactable",
            with_script("<script>fetch('data.json'); localStorage.x = 1;</script>"),
        )
    message = str(caught.value)
    assert "cannot work in the sandboxed frame" in message
    assert "fetch() ('fetch('): the frame has no network access" in message
    assert "browser storage ('localStorage'): storage throws an error" in message


def test_rubric_required_defaults_to_true() -> None:
    private = {"model_answer": "m", "rubric": [{"criterion": "c"}]}
    out = validate_block_content("blurb", blurb(private=private))
    assert out["private"]["rubric"] == [{"criterion": "c", "required": True}]


def test_validate_returns_a_new_dict() -> None:
    content = mc()
    out = validate_block_content("multiple_choice", content)
    out["options"].append("x")
    assert content["options"] == ["3", "4", "5"]


@pytest.mark.parametrize(
    ("type_", "content"),
    [
        ("video", {"url": "x"}),
        ("prose", {}),
        ("prose", {"markdown": ""}),
        ("prose", {"markdown": "x", "private": {"a": 1}}),  # prose has no private part
        ("prose", {"markdown": "x", "extra": 1}),
        ("prose", ["markdown"]),
        ("multiple_choice", mc(options=["only one"])),
        ("multiple_choice", mc(options=[str(i) for i in range(9)])),
        ("multiple_choice", mc(private={"correct_index": 3, "explanation": "x"})),
        ("multiple_choice", mc(private={"correct_index": -1, "explanation": "x"})),
        ("multiple_choice", mc(private={"correct_index": "1", "explanation": "x"})),
        ("multiple_choice", mc(private={"correct_index": True, "explanation": "x"})),
        ("multiple_choice", mc(private={"correct_index": 1})),
        ("multiple_choice", {k: v for k, v in mc().items() if k != "private"}),
        ("multiple_choice", mc(options=["a", 2])),
        ("multiple_choice", mc(hint="no")),
        ("blurb", blurb(min_words=0)),
        ("blurb", blurb(min_words="20")),
        ("blurb", blurb(private={"model_answer": "x"})),
        ("blurb", blurb(private={"model_answer": "x", "rubric": [{"criterion": "c"}], "extra": 1})),
        ("blurb", blurb(private={"model_answer": "x", "rubric": []})),  # at least one item
        ("blurb", blurb(private={"model_answer": "x", "rubric": ["old string form"]})),
        ("blurb", blurb(private={"model_answer": "x", "rubric": [{"criterion": ""}]})),
        ("blurb", blurb(private={"model_answer": "x", "rubric": [{"criterion": "  "}]})),
        ("blurb", blurb(private={"model_answer": "x", "rubric": [{"required": True}]})),
        ("blurb", blurb(private={"model_answer": "x", "rubric": [{"criterion": 1}]})),
        ("blurb", blurb(private={"model_answer": "x", "rubric": [{"criterion": "c", "required": "yes"}]})),
        ("blurb", blurb(private={"model_answer": "x", "rubric": [{"criterion": "c", "required": 1}]})),
        ("blurb", blurb(private={"model_answer": "x", "rubric": [{"criterion": "c", "weight": 2}]})),
        ("blurb", blurb(private={"model_answer": "x", "rubric": {"criterion": "c"}})),
        ("blurb", {"private": blurb()["private"]}),
    ],
)
def test_invalid_contents(type_: str, content: Any) -> None:
    with pytest.raises(ValueError):
        validate_block_content(type_, content)


SIX = [series(f"s{i}", ("A", i)) for i in range(6)]


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        (chart(kind="pie"), "kind"),
        (chart(private={"a": 1}), "private"),
        (chart(colour="red"), "colour"),
        ({k: v for k, v in chart().items() if k != "source"}, "source"),
        (chart(title=" "), "title must not be blank"),
        (chart(x_label=""), "x_label must not be blank"),
        (chart(y_label="\n"), "y_label must not be blank"),
        (chart(source=""), "source must not be blank"),
        (chart(title=1), "title"),
        (chart(series=[series(" ", ("A", 1))]), "name must not be blank"),
        (chart(series=[]), "a bar chart needs 1 to 6 series, got 0"),
        (chart(series=[*SIX, series("s6", ("A", 1))]), "a bar chart needs 1 to 6 series, got 7"),
        (chart("scatter", series=[series(f"s{i}", (1, 1)) for i in range(4)]),
         "a scatter chart needs 1 to 3 series, got 4"),
        (chart(series=[series("Ours", ("A", 1)), series(" ours ", ("A", 2))]),
         "series 2 (' ours '): the name is already used by series 1"),
        (chart(series=[series("a")]), "series 1 ('a'): a bar series needs 1 to 60 points, got 0"),
        (chart("scatter", series=[series("a", *[(i, i) for i in range(61)])]),
         "a scatter series needs 1 to 60 points, got 61"),
        (chart("line", series=[series("a", (1, 1))]), "a line series needs 2 to 60 points, got 1"),
        (chart(series=[{"name": "a", "points": [{"x": "A"}]}]), "y"),
        (chart(series=[{"name": "a", "points": [{"x": "A", "y": 1, "label": "z"}]}]), "label"),
        (chart(series=[{"name": "a", "points": [["A", 1]]}]), "points"),
        (chart(series=[series("a", ("A", "1"))]), "valid number"),
        (chart(series=[series("a", ("A", None))]), "valid number"),
        (chart(series=[series("a", ("A", math.nan))]),
         "point 1: y must be a finite number, got nan"),
        (chart(series=[series("a", ("A", math.inf))]), "y must be a finite number, got inf"),
        (chart("line", series=[series("a", (1, 1), (2, -math.inf))]),
         "point 2: y must be a finite"),
        (chart(series=[series("a", (1, 1))]),
         "in a bar chart x is a category name (a string), got the number 1.0"),
        (chart(series=[series("a", (" ", 1))]), "the category name (x) must not be blank"),
        (chart(series=[series("a", ("A", 1), ("a ", 2))]),
         "point 2: category 'a ' is repeated (point 1"),
        (chart(series=[series("a", ("BLEU", 1)), series("b", ("bleu", 2))]),
         "series 2 ('b'), point 1: category 'bleu' is written 'BLEU' in series 1 ('a')"),
        (chart(series=[series("a", *[(f"c{i}", i) for i in range(13)]),
                       series("b", *[(f"c{i}", i) for i in range(12, 25)])]),
         "a bar chart has at most 24 distinct categories, got 25"),
        (chart("line", series=[series("a", ("x", 1), (2, 2))]),
         "in a line chart x must be a number"),
        (chart("scatter", series=[series("a", ("1", 1))]), "in a scatter chart x must be a number"),
        (chart("line", series=[series("a", (math.nan, 1), (2, 2))]), "x must be a finite number"),
        (chart("scatter", series=[series("a", (math.inf, 1))]), "x must be a finite number"),
        (chart("line", series=[series("a", (1, 1), (1.0, 2))]), "point 2: x 1.0 is repeated"),
    ],
)
def test_invalid_charts(content: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValueError, match="invalid chart block content") as caught:
        validate_block_content("chart", content)
    assert fragment in str(caught.value)


def test_scatter_allows_repeated_x_and_bar_allows_shared_categories() -> None:
    scatter = chart("scatter", series=[series("a", (1, 1), (1, 2)), series("b", (1, 3))])
    validate_block_content("chart", scatter)
    validate_block_content("chart", chart(series=[*SIX[:5], series("s5", ("A", 5))]))
    many = [series("a", *[(f"c{i}", i) for i in range(24)])]
    validate_block_content("chart", chart(series=many))
    long_line = chart("line", series=[series("a", *[(i, i) for i in range(60)])])
    validate_block_content("chart", long_line)


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        (interactable(private={"a": 1}), "private"),
        (interactable(height=300), "height"),
        ({k: v for k, v in interactable().items() if k != "html"}, "html"),
        (interactable(title=" "), "title must not be blank"),
        (interactable(instructions=""), "instructions must not be blank"),
        (interactable(html=" \n "), "html must not be blank"),
        (interactable(html=["<script></script>"]), "html"),
        (interactable(html="<p>No script here.</p><style>p{}</style>"), "no <script> element"),
        (interactable(html="<p>A <scripted> thing</p>"), "no <script> element"),
        (interactable(html="<script>let a = 1;</script>" + "x" * (30001 - 27)),
         "html has 30001 characters; at most 30000 are allowed"),
        # document-level tags
        (with_script("<!DOCTYPE html>"),
         "a <!doctype>, <html>, <head> or <body> tag ('<!DOCTYPE')"),
        (with_script("<HTML lang='en'>"), "<html>, <head> or <body> tag ('<HTML')"),
        (with_script("<head></head>"), "('<head')"),
        (with_script("</body>"), "('</body')"),
        (with_script("<meta charset='utf-8'>"), "a <meta> or <base> tag ('<meta')"),
        (with_script("<base href='/'>"), "('<base')"),
        (with_script("<link rel='stylesheet' href='a.css'>"), "a <link> tag ('<link')"),
        (with_script("<iframe></iframe>"), "an <iframe>, <frame>, <object> or <embed> tag"),
        (with_script("<frame/>"), "('<frame')"),
        (with_script("<object data='x'></object>"), "('<object')"),
        (with_script("<Embed src='x.swf'>"), "('<Embed')"),
        (with_script("<form><button>Go</button></form>"),
         "a <form> tag ('<form'): form submission is blocked in the sandbox"),
        # external resources
        (with_script("<script src='https://cdn.example/d3.js'></script>"), "a <script src>"),
        (with_script("<SCRIPT type='module' SRC=lib.js></SCRIPT>"), "a <script src>"),
        (with_script('<img src="https://example.org/a.png">'), "an external URL ('src=\"https:')"),
        (with_script("<img SRC=http://example.org/a.png>"), "an external URL"),
        (with_script("<img src=//example.org/a.png>"), "an external URL ('src=//')"),
        (with_script("<img srcset='a.png 1x, https://x.org/b.png 2x'>"), "an external URL"),
        (with_script("<svg><use xlink:href='https://x.org/s.svg#a'/></svg>"), "an external URL"),
        (with_script("<style>.a { background: URL('https://x.org/a.png'); }</style>"),
         "an external URL"),
        (with_script("<style>@font-face { src: url(//fonts.example/f.woff2); }</style>"),
         "an external URL"),
        (with_script("<style>@import 'theme.css';</style>"), "an @import"),
        (with_script("<a href='https://example.org'>paper</a>"), "a link to another page"),
        (with_script("<a class='x' href=\"other.html\">next</a>"), "a link to another page"),
        # network APIs
        (with_script("<script>fetch('data.json').then(r => r.json());</script>"),
         "fetch() ('fetch(')"),
        (with_script("<script>window.fetch (url);</script>"), "fetch()"),
        (with_script("<script>const r = new XMLHttpRequest();</script>"),
         "a network API ('XMLHttpRequest')"),
        (with_script("<script>new WebSocket('wss://x');</script>"), "('WebSocket')"),
        (with_script("<script>new EventSource('/s');</script>"), "('EventSource')"),
        (with_script("<script>navigator.sendBeacon('/b', d);</script>"), "('sendBeacon')"),
        (with_script("<script>importScripts('w.js');</script>"), "('importScripts')"),
        (with_script("<script>const m = await import('./m.js');</script>"), "a dynamic import()"),
        (with_script("<script type='module'>import { scaleLinear } from 'd3';</script>"),
         "an import statement"),
        (with_script("<script type='module'>import * as d3\n from \"d3\";</script>"),
         "an import statement"),
        (with_script("<script type='module'>import 'side-effect.js';</script>"),
         "an import statement"),
        (with_script("<script>const w = new Worker('w.js');</script>"), "a worker"),
        # storage
        (with_script("<script>localStorage.setItem('k', 1);</script>"),
         "browser storage ('localStorage')"),
        (with_script("<script>sessionStorage.clear();</script>"), "('sessionStorage')"),
        (with_script("<script>indexedDB.open('db');</script>"), "('indexedDB')"),
        (with_script("<script>document.cookie = 'a=1';</script>"), "('document.cookie')"),
        # modal dialogs
        (with_script("<script>alert('done');</script>"),
         "a modal dialog ('alert('): dialogs are blocked in the sandbox"),
        (with_script("<script>if (confirm('sure?')) {}</script>"), "a modal dialog ('confirm(')"),
        (with_script("<script>const v = window.prompt('n?');</script>"),
         "a modal dialog ('window.prompt(')"),
        (with_script("<script>window.open('x');</script>"), "window.open()"),
        # the channel to the page
        (with_script("<script>window.parent.document.title = 'x';</script>"),
         "the channel to the page ('window.parent')"),
        (with_script("<script>window.top.location = 'x';</script>"), "('window.top')"),
        (with_script("<script>parent.postMessage({h: 1}, '*');</script>"), "('postMessage')"),
    ],
)
def test_invalid_interactables(content: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValueError, match="invalid interactable block content") as caught:
        validate_block_content("interactable", content)
    assert fragment in str(caught.value)


def test_public_and_private_split() -> None:
    content = validate_block_content("multiple_choice", mc())
    public = public_content(content)
    assert public == {"question": "2 + 2?", "options": ["3", "4", "5"]}
    assert "private" in content  # original untouched
    private = private_content(content)
    assert private == {"correct_index": 1, "explanation": "Arithmetic."}
    private["correct_index"] = 0
    public["options"].append("6")
    assert content["private"]["correct_index"] == 1  # deep copies
    assert content["options"] == ["3", "4", "5"]


def test_prose_has_no_private_part() -> None:
    content = validate_block_content("prose", {"markdown": "text"})
    assert public_content(content) == content
    assert private_content(content) is None


def test_chart_caption_may_be_blank() -> None:
    # A chart without a caption is drawn without one; it does not fail a generated page.
    for caption in ("", " \n"):
        assert validate_block_content("chart", chart(caption=caption))["caption"] == caption


# -- labels --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("## Scaled *dot-product* attention\n\nThe scores are divided.", "Scaled dot-product attention"),
        ("The scores are **divided** by [the root](https://x.org) of d.\n\n# Later", "Later"),
        ("```python\n# not a heading\n```\nPlain `code` words.", "Plain code words."),
        ("```\nonly code\n```", "only code"),
        ("> A quote\n- and a list", "A quote and a list"),
        ("![a diagram](d.png)", "a diagram"),
    ],
)
def test_prose_label_is_its_heading_or_opening_words(markdown: str, expected: str) -> None:
    assert block_label("prose", {"markdown": markdown}) == expected


def test_labels_are_short_public_and_never_empty() -> None:
    long = "Why does dividing the scores by the square root of the key dimension help softmax?"
    label = block_label("multiple_choice", mc(question=long))
    assert len(label) <= LABEL_MAX and label.endswith("…") and long.startswith(label[:-1])
    assert block_label("multiple_choice", mc()) == "2 + 2?"
    assert block_label("blurb", blurb()) == blurb()["prompt"]
    assert block_label("chart", chart()) == chart()["title"]
    assert block_label("prose", {"markdown": "<br>"}) == "(a prose block without text)"
    with pytest.raises(ValueError, match="unknown block type"):
        block_label("video", {})
    with pytest.raises(ValueError, match="no question text"):
        block_label("multiple_choice", {"private": {"question": "hidden"}})
