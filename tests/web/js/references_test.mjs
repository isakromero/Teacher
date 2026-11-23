// Run by tests/web/test_pages.py (`node references_test.mjs`): the tutor chat's reference
// helpers (selected text extraction, limits, previews) on a minimal fake DOM.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const refs = await import(new URL("js/references.js", STATIC));
const { MAX_REFERENCES, MAX_REFERENCE_CHARS, addReference, clampReference, extractText, normalizeText, previewText } =
  refs;

// -- a tiny fake DOM: just what extractText reads -----------------------------------------------

const text = (value) => ({ nodeType: 3, nodeName: "#text", nodeValue: value, childNodes: [] });
function el(tag, attrs = {}, ...children) {
  return {
    nodeType: 1,
    nodeName: tag.toUpperCase(),
    childNodes: children.map((c) => (typeof c === "string" ? text(c) : c)),
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
  };
}
const fragment = (...children) => ({ nodeType: 11, nodeName: "#document-fragment", childNodes: children });

/** KaTeX "htmlAndMathml" output for `tex`. */
function katex(tex, display = false) {
  const node = el(
    "span",
    { class: "katex" },
    el(
      "span",
      { class: "katex-mathml" },
      el(
        "math",
        display ? { display: "block" } : {},
        el("semantics", {}, el("mi", {}, "x"), el("annotation", { encoding: "application/x-tex" }, tex))
      )
    ),
    el("span", { class: "katex-html", "aria-hidden": "true" }, "x2")
  );
  return display ? el("span", { class: "katex-display" }, node) : node;
}

// Paragraphs, soft line breaks, lists and inline maths.
{
  const frag = fragment(
    el("p", {}, "The loss\n  ", katex("\\mathcal{L}_i"), " is   averaged."),
    text("\n"),
    el("ul", {}, el("li", {}, "first"), text("\n"), el("li", {}, "second")),
    el("p", {}, "Display:"),
    el("span", { class: "math math-display" }, katex("\\sum_k x_k", true))
  );
  assert.equal(
    extractText(frag),
    "The loss $\\mathcal{L}_i$ is averaged.\n\nfirst\nsecond\n\nDisplay:\n\n$$\\sum_k x_k$$"
  );
}

// Buttons and form controls are not part of the selected text; <br> and <pre> keep lines.
{
  const frag = fragment(
    el("p", {}, "Question?", el("br"), "Next line"),
    el("button", {}, "Ask about this"),
    el("textarea", {}, "typed answer"),
    el("pre", {}, el("code", {}, "a  =  1\nb = 2"))
  );
  assert.equal(extractText(frag), "Question?\nNext line\n\na = 1\nb = 2");
}

// A partial clone (just a text node) works too.
assert.equal(extractText(fragment(text("  part of a sentence  "))), "part of a sentence");
assert.equal(extractText(fragment()), "");

// normalizeText
assert.equal(normalizeText("a  b\r\n\r\n\r\n\r\nc  "), "a b\n\nc");

// clampReference: at most 2000 characters, flagged when shortened, surrogate pairs kept whole.
{
  assert.equal(MAX_REFERENCE_CHARS, 2000);
  assert.deepEqual(clampReference("short"), { text: "short", truncated: false });
  const exact = "x".repeat(2000);
  assert.deepEqual(clampReference(exact), { text: exact, truncated: false });
  const long = clampReference("y".repeat(2500));
  assert.equal(long.text.length, 2000);
  assert.equal(long.truncated, true);
  const astral = clampReference("a".repeat(1999) + "𝔼" + "b".repeat(10));
  assert.equal(astral.text, "a".repeat(1999)); // never half of the 𝔼 surrogate pair
  assert.equal(astral.truncated, true);
}

// previewText: one line, about 80 characters.
{
  assert.equal(previewText("  two\n lines  "), "two lines");
  const preview = previewText("word ".repeat(40));
  assert.ok(preview.endsWith("…"), preview);
  assert.ok(preview.length <= 81, preview);
}

// addReference: at most five, no duplicates, never mutates the list.
{
  assert.equal(MAX_REFERENCES, 5);
  let list = [];
  for (let i = 0; i < 5; i++) {
    const result = addReference(list, { block_key: `b0${i}`, text: `passage ${i}` });
    assert.equal(result.error, null);
    assert.equal(result.list.length, i + 1);
    assert.notEqual(result.list, list);
    list = result.list;
  }
  const sixth = addReference(list, { block_key: "b09", text: "one too many" });
  assert.match(sixth.error, /at most 5/);
  assert.equal(sixth.list, list);
  assert.equal(list.length, 5);
  const dup = addReference([{ block_key: "b01", text: "same" }], { block_key: "b01", text: "same" });
  assert.match(dup.error, /already attached/);
  assert.ok(addReference([], { block_key: "b01", text: "" }).error);
}

console.log("references ok");
