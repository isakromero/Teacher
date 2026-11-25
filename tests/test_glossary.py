"""The glossary's pure parts (matching, block text, selection, thresholds) and `GlossaryBuilder`
on a fake client: request shape, interpretation, and each failure class."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import anthropic
import pytest

from teacher.claude_generator import (
    CACHE_CONTROL,
    EFFORT,
    GLOSSARY_FORMAT,
    GLOSSARY_INSTRUCTIONS,
    GLOSSARY_MAX_TOKENS,
    SYSTEM_PROMPT,
    ClaudeGenerator,
    GlossaryBuilder,
    document_block,
    render_study_page,
)
from teacher.generation import SourceInfo
from teacher.glossary import (
    PROSE_MIN_WORDS,
    GlossaryEntry,
    GlossaryInputError,
    GlossaryOutputError,
    GlossaryUnavailableError,
    block_fields,
    block_texts,
    check_selection,
    find_occurrences,
    occurs,
    select_terms,
    visible_entries,
)

# The components here make their API calls directly: an in-memory cost meter records them
# (without a meter a component refuses to make a call; see `teacher.costs`).
pytestmark = pytest.mark.usefixtures("cost_meter")

PDF_BYTES = b"%PDF-1.4\n% a tiny stand-in for a paper\n%%EOF\n"


def entry(term: str, kind: str = "term", aliases: tuple[str, ...] = (), **kw: Any) -> GlossaryEntry:
    return GlossaryEntry(term, kind, kw.pop("definition", "A definition."), aliases=aliases, **kw)


def spans(text: str, form: str, case_sensitive: bool = True) -> list[str]:
    return [text[a:b] for a, b in find_occurrences(text, form, case_sensitive=case_sensitive)]


# -- matching -------------------------------------------------------------------------------


def test_whole_words_only() -> None:
    assert spans("The ORGAN and the GAN.", "GAN") == ["GAN"]
    assert spans("GANs, GAN-based, (GAN), 'GAN'", "GAN") == ["GAN", "GAN", "GAN"]
    assert spans("GAN_loss GAN2 2GAN", "GAN") == []
    assert spans("self-attention and attention", "attention") == ["attention", "attention"]
    assert spans("self-attention", "self-attention") == ["self-attention"]


def test_case_rules_follow_the_kind() -> None:
    assert spans("MAP and a map", "MAP") == ["MAP"]
    assert spans("Gradient descent and gradient Descent", "gradient descent", False) == [
        "Gradient descent", "gradient Descent",
    ]
    assert occurs(entry("MAP", "acronym"), ["a map of"]) is False
    assert occurs(entry("map", "term"), ["the MAP estimate"]) is True
    assert occurs(entry("ε", "symbol"), ["take ε small"]) is True


def test_whitespace_in_a_term_matches_any_run_of_whitespace() -> None:
    assert spans("stochastic\n  gradient descent", "stochastic gradient descent") == [
        "stochastic\n  gradient descent"
    ]


def test_regex_characters_and_non_ascii() -> None:
    assert spans("C++ and C", "C++") == ["C++"]
    assert spans("use k-NN (k-NN)", "k-NN") == ["k-NN", "k-NN"]
    assert spans("a.b and axb", "a.b") == ["a.b"]
    assert spans("Schrödinger equation", "Schrödinger") == ["Schrödinger"]
    assert spans("Schrödingers", "Schrödinger") == []
    assert spans("über-Modell", "Modell", False) == ["Modell"]
    assert spans("Ärzte ÄRZTE", "ärzte", False) == ["Ärzte", "ÄRZTE"]
    assert spans("café", "cafe") == []  # an accent is part of the word
    # The same boundary cases as tests/web/js/glossary_match_test.mjs (server and page agree).
    assert spans("café", "cafe") == []  # a combining mark continues the word
    assert spans("𝑥GAN GAN", "GAN") == ["GAN"]
    assert [a for a, _ in find_occurrences("𝑥GAN GAN", "GAN", case_sensitive=True)] == [5]
    assert spans("в модели ГАН и ГАНы", "ГАН") == ["ГАН"]
    assert spans("Tokens 東京 here", "東京") == ["東京"]
    assert spans("f(x) [1] a|b $x$ ^y C++x", "C++") == ["C++"]
    for form in ("f(x)", "[1]", "a|b", "$x$", "^y"):
        assert spans("f(x) [1] a|b $x$ ^y", form) == [form]


def test_overlapping_candidates_are_all_found() -> None:
    assert spans("xab ab ab", "ab ab") == ["ab ab"]


def test_visible_entries_keep_only_visible_forms() -> None:
    gan = entry("GAN", "acronym", aliases=("GANs", "generative adversarial network"))
    assert visible_entries([gan, entry("loss")], ["Two GANs train."]) == [
        entry("GAN", "acronym", aliases=("GANs",))
    ]
    assert visible_entries([gan], ["nothing here"]) == []


# -- block text -----------------------------------------------------------------------------


def test_block_fields_walk_public_and_private_text_in_order() -> None:
    content = {
        "question": "Why?",
        "options": ["A", " ", "B"],
        "private": {"correct_index": 1, "explanation": "Because.",
                    "rubric": [{"criterion": "Names it", "required": True}]},
        "min_words": 20,
    }
    assert block_fields(content) == [
        ("question", "Why?", False), ("options", "A", False), ("options", "B", False),
        ("explanation", "Because.", True), ("criterion", "Names it", True),
    ]
    assert block_texts({"markdown": "Hi"}) == ["Hi"] and block_texts(None) == []


CHART = {
    "kind": "bar",
    "title": "BLEU by model",
    "x_label": "Model",
    "y_label": "BLEU",
    "series": [{"name": "WMT14", "points": [{"x": "Transformer", "y": 28.4},
                                            {"x": "ConvS2S", "y": 25.2}]}],
    "caption": "The Transformer leads.",
    "source": "Table 2",
}
WIDGET = {
    "title": "Attention weights",
    "instructions": "Drag the query and watch the softmax.",
    "html": "<div id='w'></div><script>const softmax = (v) => v; // bar kind</script>",
}


def test_block_fields_skip_non_text_keys_at_any_depth() -> None:
    assert block_fields(CHART) == [
        ("title", "BLEU by model", False), ("x_label", "Model", False), ("y_label", "BLEU", False),
        ("name", "WMT14", False), ("x", "Transformer", False), ("x", "ConvS2S", False),
        ("caption", "The Transformer leads.", False), ("source", "Table 2", False),
    ]
    assert block_fields(WIDGET) == [
        ("title", "Attention weights", False),
        ("instructions", "Drag the query and watch the softmax.", False),
    ]
    nested = {"private": {"kind": "secret", "list": [{"html": "<b>x</b>", "text": "kept"}]}}
    assert block_fields(nested) == [("text", "kept", True)]
    # a term only in the widget source or the chart kind is not on the page
    assert not occurs(entry("const"), block_texts(WIDGET))
    assert not occurs(entry("bar"), block_texts(CHART) + block_texts(WIDGET))
    assert occurs(entry("softmax"), block_texts(WIDGET))
    assert occurs(entry("ConvS2S", "acronym"), block_texts(CHART))


def test_render_study_page_shows_chart_and_widget_text_only() -> None:
    page = render_study_page([("chart", CHART), ("interactable", WIDGET)])
    assert page.startswith('<block number="1" type="chart">\n<title>\nBLEU by model\n</title>')
    for part in ("<x_label>\nModel\n</x_label>", "<series_name>\nWMT14\n</series_name>",
                 "<category>\nTransformer\n</category>", "<source>\nTable 2\n</source>",
                 "<caption>\nThe Transformer leads.\n</caption>",
                 '<block number="2" type="interactable">\n<title>\nAttention weights\n</title>',
                 "<instructions>\nDrag the query and watch the softmax.\n</instructions>"):
        assert part in page, part
    for absent in ("28.4", "kind", "<html>", "<script", "const", "bar"):
        assert absent not in page, absent


def test_render_study_page_tags_and_escapes() -> None:
    page = render_study_page([
        ("prose", {"markdown": "Text with </block> and <study_page> inside, and a<b."}),
        ("multiple_choice", {"question": "Q", "options": ["x"],
                             "private": {"correct_index": 0, "explanation": "E </explanation>"}}),
    ])
    assert page.startswith('<block number="1" type="prose">\n<text>\n')
    assert "&lt;/block> and &lt;study_page> inside, and a<b." in page
    assert '<explanation hidden="true">\nE &lt;/explanation>\n</explanation>' in page
    assert page.count("</block>") == 2


MCQ_WITH_FEEDBACK = {
    "question": "Why scale?",
    "options": ["To keep gradients usable", "To save memory"],
    "private": {"correct_index": 0, "explanation": "Saturation.",
                "option_explanations": ["Right: the softmax saturates.", "Memory is unchanged."]},
}
PREDICTION = {
    "prompt": "What happens without scaling?",
    "options": ["Training stalls", "Nothing changes"],
    "private": {"correct_index": 0, "resolution": "Training stalls: the softmax saturates."},
}
CRITIQUE = {"markdown": "## A critical look\n\nLater work questions this.", "role": "critique"}


def test_new_block_shapes_keep_their_answers_hidden() -> None:
    """Per-option explanations and a prediction's resolution are hidden parts (the glossary
    step must keep them out of definitions); a prose role is not text."""
    assert block_fields(MCQ_WITH_FEEDBACK)[-2:] == [
        ("option_explanations", "Right: the softmax saturates.", True),
        ("option_explanations", "Memory is unchanged.", True),
    ]
    assert block_fields(PREDICTION) == [
        ("prompt", "What happens without scaling?", False),
        ("options", "Training stalls", False), ("options", "Nothing changes", False),
        ("resolution", "Training stalls: the softmax saturates.", True),
    ]
    assert block_fields(CRITIQUE) == [("markdown", CRITIQUE["markdown"], False)]
    assert not occurs(entry("critique"), block_texts(CRITIQUE))
    assert occurs(entry("softmax"), block_texts(PREDICTION))  # hidden text is still page text

    page = render_study_page([("prose", CRITIQUE), ("multiple_choice", MCQ_WITH_FEEDBACK),
                              ("prediction", PREDICTION)])
    assert page.startswith('<block number="1" type="prose" role="critique">\n<text>\n')
    assert ('<option_explanation hidden="true">\nMemory is unchanged.\n</option_explanation>'
            in page)
    assert '<block number="3" type="prediction">\n<prompt>\nWhat happens' in page
    assert ('<resolution hidden="true">\nTraining stalls: the softmax saturates.\n'
            "</resolution>") in page
    assert "<option>\nTraining stalls\n</option>" in page  # options are public
    assert "<role>" not in page and "correct_index" not in page
    # A page cannot fake a hidden tag of the new kinds either.
    tricky = render_study_page([("prose", {"markdown": "x </option_explanation> y"}),
                                ("multiple_choice", MCQ_WITH_FEEDBACK)])
    assert "x &lt;/option_explanation> y" in tricky


# -- selection ------------------------------------------------------------------------------

PAGE_TEXT = ["We train a GAN with SGD.", "Self-attention helps; GANs are unstable."]


def test_select_normalises_merges_and_drops() -> None:
    proposed = [
        entry("  GAN ", "acronym", aliases=("GANs", " ", "GAN", "GANs"),
              expansion="generative  adversarial network", definition=" Two  nets. "),
        entry("gan", "acronym", aliases=("GANs",), definition="dup"),
        entry("self-attention", aliases=("Self-Attention", "SGD")),
        entry("SGD", "acronym", expansion="  "),
        entry("Adam", "acronym"),
        entry(" ", "term"),
        entry("ReLU", "acronym", definition="  "),
    ]
    selection = select_terms(proposed, PAGE_TEXT)
    assert selection.kept == [
        GlossaryEntry("GAN", "acronym", "Two nets.", "generative adversarial network", ("GANs",)),
        GlossaryEntry("self-attention", "term", "A definition."),
        GlossaryEntry("SGD", "acronym", "A definition."),
    ]
    assert selection.proposed == 6  # GAN (+ duplicate), self-attention, SGD, Adam, blank, ReLU
    assert selection.dropped == [
        {"term": " ", "reason": "the term is blank"},
        {"term": "ReLU", "reason": "the definition is blank"},
        {"term": "Adam", "reason": "does not occur in the study page text"},
    ]
    assert selection.merged == [{"term": "gan", "into": "GAN"}]
    assert selection.dropped_aliases == [
        {"term": "GAN", "alias": "gan", "reason": "does not occur in the study page text"},
        {"term": "self-attention", "alias": "SGD", "reason": "is another glossary term"},
    ]


def test_case_insensitive_kind_drops_alias_equal_to_term() -> None:
    selection = select_terms([entry("Self-attention", aliases=("self-attention",))], PAGE_TEXT)
    assert selection.kept == [entry("Self-attention")] and selection.dropped_aliases == []


def test_thresholds() -> None:
    texts = ["We train a GAN with SGD and Adam."]
    three = [entry("GAN", "acronym"), entry("SGD", "acronym"), entry("ResNet", "acronym")]
    check_selection(select_terms(three, texts), texts)  # 1 of 3 dropped: exactly a third is fine
    with pytest.raises(GlossaryOutputError, match=r"2 of the 3 terms .* more than a third"):
        check_selection(select_terms([*three[:1], three[2], entry("VAE", "acronym")], texts), texts)
    check_selection(select_terms([], texts), texts)  # a short page may have no terms
    long_text = ["word " * PROSE_MIN_WORDS]
    with pytest.raises(GlossaryOutputError, match=f"no usable terms for a page with {PROSE_MIN_WORDS} words"):
        check_selection(select_terms([], long_text), long_text)


# -- the builder ----------------------------------------------------------------------------


@pytest.fixture
def source(tmp_path: Path) -> SourceInfo:
    path = tmp_path / "paper.pdf"
    path.write_bytes(PDF_BYTES)
    return SourceInfo("paper", "A Paper", "paper", "primary", path, "application/pdf")


BLOCKS = [
    ("prose", {"markdown": "We train a **GAN** with SGD."}),
    ("multiple_choice", {"question": "Why use SGD?", "options": ["Speed", "Taste"],
                         "private": {"correct_index": 0, "explanation": "Speed; a GAN is big."}}),
]
TERMS = {"terms": [
    {"term": "GAN", "kind": "acronym", "expansion": "generative adversarial network",
     "definition": "Two networks trained against each other.", "aliases": []},
    {"term": "SGD", "kind": "acronym", "expansion": None,
     "definition": "The paper does not spell it out.", "aliases": []},
]}


def test_request_shape_matches_the_generator_prefix(source: SourceInfo, fake_anthropic) -> None:
    fake = fake_anthropic(fake_anthropic.reply(text=json.dumps(TERMS)))
    builder = GlossaryBuilder(fake, model="claude-test", timeout=33.0)
    document = builder.document(source)
    assert document == document_block(source)[0]
    message = builder.call(document, BLOCKS)
    (call,) = fake.calls
    assert set(call) == {"model", "max_tokens", "system", "messages", "output_config", "timeout"}
    assert call["model"] == "claude-test" and call["timeout"] == 33.0
    assert call["max_tokens"] == GLOSSARY_MAX_TOKENS
    assert call["system"] == SYSTEM_PROMPT
    assert call["output_config"] == {"effort": EFFORT, "format": GLOSSARY_FORMAT}
    (turn,) = call["messages"]
    doc, instructions = turn["content"]
    assert doc == {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf",
                   "data": base64.standard_b64encode(PDF_BYTES).decode("ascii")},
        "title": "A Paper",
        "cache_control": CACHE_CONTROL,
    }
    assert instructions == {
        "type": "text",
        "text": GLOSSARY_INSTRUCTIONS.format(page=render_study_page(BLOCKS)),
    }
    # The generator's own request for any stage has the same prefix up to the document.
    generator_call = ClaudeGenerator(model="claude-test", timeout=33.0).request(
        document, "x", GLOSSARY_FORMAT, 1
    )
    assert generator_call["system"] == call["system"]
    assert generator_call["messages"][0]["content"][0] == doc
    result = builder.interpret(message, BLOCKS)
    assert [e.term for e in result.entries] == ["GAN", "SGD"] and result.dropped_count == 0
    record = builder.record(message, result, source=source, trigger="on_demand")
    assert record["trigger"] == "on_demand" and record["dropped"] == []
    assert record["output"]["terms"][0]["expansion"] == "generative adversarial network"


def test_no_blocks_is_an_input_error(source: SourceInfo, fake_anthropic) -> None:
    fake = fake_anthropic()
    with pytest.raises(GlossaryInputError, match="the study page has no blocks"):
        GlossaryBuilder(fake).call(GlossaryBuilder.document(source), [])
    assert fake.calls == []


def test_unusable_source_is_an_input_error(tmp_path: Path) -> None:
    path = tmp_path / "x.png"
    path.write_bytes(b"\x89PNG")
    bad = SourceInfo("img", "Img", "paper", "primary", path, "image/png")
    with pytest.raises(GlossaryInputError, match="The glossary could not be built: .*image/png"):
        GlossaryBuilder.document(bad)
    assert issubclass(GlossaryInputError, ValueError)


def test_missing_credentials_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, forbid_real_messages_api: None
) -> None:
    monkeypatch.delenv("ANTHROPIC_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    builder = GlossaryBuilder()
    with pytest.raises(GlossaryUnavailableError, match="no Anthropic API credentials"):
        builder.check_available()
    assert builder._client is None


@pytest.mark.parametrize(
    ("outcome", "error", "fragment"),
    [
        (lambda f: f.status_error(anthropic.RateLimitError, 429, "rate_limit_error"),
         GlossaryUnavailableError, "the glossary call to Claude failed: the Anthropic API rate"),
        (lambda f: f.connection_error(), GlossaryUnavailableError, "could not connect"),
        (lambda f: f.reply(text="", stop_reason="refusal",
                           stop_details={"type": "refusal", "category": None, "explanation": None}),
         GlossaryUnavailableError, "declined the glossary step .*category unspecified"),
        (lambda f: f.reply(text="{", stop_reason="max_tokens"),
         GlossaryUnavailableError, "cut off at max_tokens=32000"),
        (lambda f: f.reply(text="{}", stop_reason="pause_turn"),
         GlossaryUnavailableError, "stopped unexpectedly"),
        (lambda f: f.reply(text="not json"), GlossaryOutputError, "not a valid WireGlossary"),
        (lambda f: f.reply(text=json.dumps({"terms": [{**TERMS["terms"][0], "extra": 1}]})),
         GlossaryOutputError, "not a valid WireGlossary"),
    ],
)
def test_failure_classes(
    source: SourceInfo, fake_anthropic, outcome: Any, error: type, fragment: str
) -> None:
    builder = GlossaryBuilder(fake_anthropic(outcome(fake_anthropic)))
    with pytest.raises(error, match=f"^The glossary could not be built: .*{fragment}"):
        message = builder.call(builder.document(source), BLOCKS)
        builder.interpret(message, BLOCKS)


def test_terms_from_private_text_count_as_on_the_page(fake_anthropic) -> None:
    reply = fake_anthropic.reply(text=json.dumps({"terms": [
        {"term": "big", "kind": "term", "expansion": None, "definition": "d", "aliases": []},
    ]}))
    assert [e.term for e in GlossaryBuilder.interpret(reply, BLOCKS).entries] == ["big"]
