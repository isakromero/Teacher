// Run by tests/web/test_pages.py (`node glossary_match_test.mjs`): the glossary matcher that
// marks terms on the study page, and that a marked term leaves the chat's selected text alone.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const { compileTerms, findMatches, isShorthand, isWordChar, normaliseSpace } = await import(
  new URL("js/glossary_match.js", STATIC)
);
const { extractText } = await import(new URL("js/references.js", STATIC));

const t = (term, kind = "acronym", aliases = []) => ({ term, kind, aliases, definition: "d", expansion: null });

/** The matched substrings (and their keys) of `text` for `terms`. */
function marks(text, terms, seen) {
  return findMatches(text, compileTerms(terms), seen).map((m) => [text.slice(m.start, m.end), m.key]);
}

// Substrings are not matches; punctuation and hyphens are boundaries.
assert.deepEqual(marks("The ORGAN grew.", [t("GAN")]), []);
assert.deepEqual(marks("GANs and GAN2 and GAN_x", [t("GAN")]), []);
assert.deepEqual(marks("A GAN-based (GAN) model.", [t("GAN")]), [["GAN", "GAN"], ["GAN", "GAN"]]);
assert.deepEqual(marks("'GAN', GAN.", [t("GAN")]), [["GAN", "GAN"], ["GAN", "GAN"]]);
assert.deepEqual(marks("pre-GAN era", [t("GAN")]), [["GAN", "GAN"]]);
assert.deepEqual(marks("self-attention", [t("attention", "term")]), [["attention", "attention"]]);
assert.deepEqual(marks("multi-head self-attention", [t("self-attention", "term")]), [
  ["self-attention", "self-attention"],
]);

// Shorthand is marked at every occurrence, also for a key in `seen`.
assert.deepEqual(marks("GAN then GAN again", [t("GAN")]), [["GAN", "GAN"], ["GAN", "GAN"]]);
assert.deepEqual(marks("GAN here", [t("GAN")], new Set(["GAN"])), [["GAN", "GAN"]]);
assert.equal(isShorthand("SFT", "term"), true);
assert.equal(isShorthand("R1-Zero", "term"), true);
assert.equal(isShorthand("word2vec", "term"), true);
assert.equal(isShorthand("ε", "symbol"), true);
assert.equal(isShorthand("policy", "term"), false);
assert.equal(isShorthand("Adam", "term"), false);
assert.equal(isShorthand("KL divergence", "acronym"), false);

// Other forms: the first occurrence only, per call; none for a key in `seen`.
assert.deepEqual(marks("a policy and a policy", [t("policy", "term")]), [["policy", "policy"]]);
assert.equal(findMatches("a policy and a policy", compileTerms([t("policy", "term")]))[0].start, 2);
assert.deepEqual(marks("policy here", [t("policy", "term")], new Set(["policy"])), []);

// A long form with a shorthand alias: the long form once, the shorthand everywhere.
{
  const sft = [t("supervised fine-tuning", "term", ["SFT"])];
  const text = "supervised fine-tuning (SFT) first; SFT, then supervised fine-tuning and SFT.";
  assert.deepEqual(marks(text, sft).map(([m]) => m), ["supervised fine-tuning", "SFT", "SFT", "SFT"]);
  assert.deepEqual(findMatches(text, compileTerms(sft)).map((m) => m.every), [false, true, true, true]);
  assert.deepEqual(marks(text, sft, new Set(["supervised fine-tuning"])).map(([m]) => m), ["SFT", "SFT", "SFT"]);
  // A longer form covers the shorthand inside it once; after that the shorthand is marked.
  const kl = [t("KL", "acronym", ["KL divergence"])];
  assert.deepEqual(marks("KL divergence, a KL divergence, KL", kl).map(([m]) => m), ["KL divergence", "KL", "KL"]);
}

// Case rules: acronyms and symbols exact, terms case-insensitive.
assert.deepEqual(marks("a map of MAP", [t("MAP")]), [["MAP", "MAP"]]);
assert.deepEqual(marks("a map", [t("MAP")]), []);
assert.deepEqual(marks("Gradient Descent works", [t("gradient descent", "term")]), [
  ["Gradient Descent", "gradient descent"],
]);
assert.deepEqual(marks("an L2 penalty, not l2", [t("L2", "symbol")]), [["L2", "L2"]]);

// Aliases mark the same term; whitespace in a form matches any whitespace.
assert.deepEqual(marks("Two GANs fight; one GAN wins.", [t("GAN", "acronym", ["GANs"])]), [
  ["GANs", "GAN"],
  ["GAN", "GAN"],
]);
assert.deepEqual(marks("stochastic\n  gradient descent", [t("stochastic gradient descent", "term")]), [
  ["stochastic\n  gradient descent", "stochastic gradient descent"],
]);

// Overlapping terms: the longest wins where they overlap; the shorter can match later on.
{
  const terms = [t("GAN"), t("conditional GAN", "term")];
  assert.deepEqual(marks("A conditional GAN extends a GAN.", terms), [
    ["conditional GAN", "conditional GAN"],
    ["GAN", "GAN"],
  ]);
  assert.deepEqual(marks("A conditional GAN only.", terms), [["conditional GAN", "conditional GAN"]]);
  const partial = [t("neural network", "term"), t("network architecture search", "term")];
  assert.deepEqual(marks("neural network architecture search", partial), [
    ["network architecture search", "network architecture search"],
  ]);
  const ranges = findMatches("generative adversarial network (GAN)", compileTerms([
    t("adversarial network", "term"), t("generative adversarial network", "term"), t("GAN"),
  ]));
  assert.deepEqual(ranges.map((r) => r.key), ["generative adversarial network", "GAN"]);
}

// Regex-special characters in terms are literal.
assert.deepEqual(marks("C++ and C# and C", [t("C++", "term"), t("C#", "term")]), [
  ["C++", "C++"],
  ["C#", "C#"],
]);
assert.deepEqual(marks("axb a.b", [t("a.b", "term")]), [["a.b", "a.b"]]);
assert.deepEqual(marks("k-NN (k-NN)", [t("k-NN", "symbol")]), [["k-NN", "k-NN"], ["k-NN", "k-NN"]]);
assert.deepEqual(marks("f(x) [1] {a} a|b $x$ ^y", [
  t("f(x)", "symbol"), t("[1]", "symbol"), t("{a}", "symbol"), t("a|b", "symbol"),
  t("$x$", "symbol"), t("^y", "symbol"),
]).map(([m]) => m), ["f(x)", "[1]", "{a}", "a|b", "$x$", "^y"]);
assert.deepEqual(marks("C++x", [t("C++", "term")]), [["C++", "C++"]]); // ends in a non-word char

// Non-ASCII: letters with accents, other scripts, astral characters, combining marks.
assert.deepEqual(marks("Die Schrödinger-Gleichung", [t("Schrödinger", "term")]), [["Schrödinger", "Schrödinger"]]);
assert.deepEqual(marks("Schrödingers", [t("Schrödinger", "term")]), []);
assert.deepEqual(marks("ÄRZTE und Ärzte", [t("ärzte", "term")]), [["ÄRZTE", "ärzte"]]);
assert.deepEqual(marks("café", [t("caf", "term")]), []);
assert.deepEqual(marks("café", [t("cafe", "term")]), []); // a combining accent continues the word
assert.deepEqual(marks("в модели ГАН и ГАНы", [t("ГАН")]), [["ГАН", "ГАН"]]);
assert.deepEqual(marks("𝑥GAN GAN", [t("GAN")]).length, 1);
assert.equal(findMatches("𝑥GAN GAN", compileTerms([t("GAN")]))[0].start, 6);
assert.deepEqual(marks("Tokens 東京 here", [t("東京", "term")]), [["東京", "東京"]]);
assert.equal(isWordChar("é"), true);
assert.equal(isWordChar("-"), false);
assert.equal(normaliseSpace("  a \n b "), "a b");

// Bad input is ignored, not fatal.
assert.deepEqual(compileTerms([null, { kind: "term" }, t("  ", "term")]), []);
assert.deepEqual(findMatches(null, compileTerms([t("GAN")])), []);

// A marked term is an inline span: the chat's selection text is exactly the same as before,
// and the popover (appended to <body>, outside the blocks) is never part of a block.
{
  const text = (value) => ({ nodeType: 3, nodeName: "#text", nodeValue: value, childNodes: [] });
  const el = (tag, attrs = {}, ...children) => ({
    nodeType: 1,
    nodeName: tag.toUpperCase(),
    childNodes: children.map((c) => (typeof c === "string" ? text(c) : c)),
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
  });
  const plain = el("p", {}, "We train a GAN with SGD.");
  const marked = el(
    "p",
    {},
    "We train a ",
    el("span", { class: "gloss-term", tabindex: "0", "data-term": "GAN" }, "GAN"),
    " with ",
    el("span", { class: "gloss-term", tabindex: "0", "data-term": "SGD" }, "SGD"),
    "."
  );
  assert.equal(extractText(marked), extractText(plain));
  assert.equal(extractText(marked), "We train a GAN with SGD.");
}

console.log("glossary ok");
