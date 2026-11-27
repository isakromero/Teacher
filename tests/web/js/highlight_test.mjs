// Run by tests/web/test_pages.py (`node highlight_test.mjs`): the highlights' text model and
// anchoring (static/js/highlight_model.js), with plain piece lists for blocks.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const m = await import(new URL("js/highlight_model.js", STATIC));
const {
  CONTEXT_CHARS,
  buildModel,
  checkHighlight,
  checkList,
  coveredPieces,
  inPageOrder,
  locate,
  makeAnchor,
  modelToRaw,
  noSplit,
  noteValue,
  pickHighlight,
  rawToModel,
  widenToAtoms,
} = m;

const t = (text) => ({ kind: "text", text });
const gap = () => ({ kind: "gap", text: " " });
const atom = (text) => ({ kind: "atom", text });

// -- the model: whitespace runs become one space, none at the ends; gaps separate paragraphs ---
{
  const model = buildModel([gap(), t("  The  loss\n"), atom("$L_i$"), t(" is\taveraged."), gap(), gap(), t("Next")]);
  assert.equal(model.text, "The loss $L_i$ is averaged. Next");
  assert.equal(model.raw, "   The  loss\n$L_i$ is\taveraged.  Next");
  assert.equal(model.map.length, model.text.length);
  for (let k = 1; k < model.map.length; k += 1) assert.ok(model.map[k] > model.map[k - 1]);
  // every model character is the raw character it maps to (or a space for a whitespace run)
  [...model.text].forEach((char, k) => {
    const raw = model.raw[model.map[k]];
    assert.ok(char === raw || (char === " " && /\s/.test(raw)), `${k}: ${char} vs ${raw}`);
  });
  assert.equal(rawToModel(model, 0), 0);
  assert.equal(rawToModel(model, model.raw.length), model.text.length);
  const start = model.text.indexOf("loss");
  const raw = modelToRaw(model, start, start + 4);
  assert.equal(model.raw.slice(raw.start, raw.end), "loss");
  assert.equal(rawToModel(model, raw.start), start);
  assert.equal(rawToModel(model, raw.end), start + 4);
  assert.throws(() => modelToRaw(model, 3, 3), /no such range/);
  assert.throws(() => modelToRaw(model, 0, model.text.length + 1), /no such range/);
  assert.throws(() => buildModel([{ kind: "text" }]), /needs its text/);
}

// The same block drawn again with other splits of its text nodes (glossary or highlight marks,
// another layout's whitespace) has the same model text.
{
  const a = buildModel([t("Supervised fine-tuning (SFT) helps.")]);
  const b = buildModel([t("Supervised "), t("fine-tuning"), t(" ("), t("SFT"), t(") helps."), t("\n")]);
  assert.equal(a.text, b.text);
}

// -- covered pieces: partial text, whole atoms, never gaps --------------------------------------
{
  const pieces = [t("Area is "), atom("$\\pi r^2$"), t(" for a circle."), gap(), t("Next.")];
  const model = buildModel(pieces);
  const s = model.text.indexOf("is");
  const e = model.text.indexOf(" for") + " for".length;
  const raw = modelToRaw(model, s, e);
  assert.deepEqual(coveredPieces(model, raw.start, raw.end), [
    { index: 0, from: 5, to: 8 },
    { index: 1, from: 0, to: 9 },
    { index: 2, from: 0, to: 4 },
  ]);
  // a range that starts or ends inside the formula takes all of it
  const inside = model.starts[1] + 3;
  assert.deepEqual(widenToAtoms(model, inside, model.starts[2] + 2), {
    start: model.starts[1],
    end: model.starts[2] + 2,
  });
  assert.deepEqual(widenToAtoms(model, 0, inside), { start: 0, end: model.starts[2] });
  const across = modelToRaw(model, model.text.indexOf("circle"), model.text.length);
  assert.deepEqual(
    coveredPieces(model, across.start, across.end).map((p) => p.index),
    [2, 4] // the gap between the paragraphs is not drawn
  );
}

// -- anchors -------------------------------------------------------------------------------------
{
  const model = buildModel([t("  Light bends when it enters water.  Light also reflects.  ")]);
  const s = model.text.indexOf("Light also");
  const anchor = makeAnchor(model, s - 1, s + "Light also".length + 1); // spaces are trimmed
  assert.deepEqual(anchor, {
    quote: "Light also",
    prefix: "Light bends when it enters water. ".slice(-CONTEXT_CHARS),
    suffix: " reflects.",
    start: s,
    end: s + "Light also".length,
  });
  assert.equal(makeAnchor(model, 5, 5), null);
  const space = model.text.indexOf(" ");
  assert.equal(makeAnchor(model, space, space + 1), null); // only whitespace
}

// A surrogate pair (an emoji, a maths letter) is never cut by the quote or its context.
{
  const text = "x".repeat(CONTEXT_CHARS - 1) + "𝓛 is the loss 𝓛";
  const model = buildModel([t(text)]);
  const s = text.indexOf(" is");
  const anchor = makeAnchor(model, s + 1, text.length - 1); // ends inside the last pair
  assert.equal(anchor.quote, "is the loss 𝓛");
  assert.ok(!/^[\udc00-\udfff]/.test(anchor.prefix), "the prefix does not start inside a pair");
  assert.equal(noSplit("a𝓛b", 2, true), 1);
  assert.equal(noSplit("a𝓛b", 2, false), 3);
  assert.equal(noSplit("ab", 1, true), 1);
  assert.equal(noSplit("ab", -4, true), 0);
  assert.equal(noSplit("ab", 9, true), 2);
}

// -- locating again --------------------------------------------------------------------------------
{
  const text = "The cat sat. The dog sat. The cat ran. The cat sat.";
  const second = text.lastIndexOf("The cat sat");
  const model = buildModel([t(text)]);
  const anchor = makeAnchor(model, second, second + "The cat sat".length);
  assert.deepEqual(locate(text, anchor), { start: second, end: second + 11 });
  // the context decides even when the offsets are wrong (the text before it grew)
  const grown = `Intro added. ${text}`;
  assert.equal(locate(grown, { ...anchor, start: 0 }).start, grown.lastIndexOf("The cat sat"));
  // with no context, the occurrence nearest the old start wins
  const bare = { quote: "The cat", prefix: "", suffix: "", start: 26 };
  assert.equal(locate(text, bare).start, text.indexOf("The cat ran"));
  // the text changed: not found
  assert.equal(locate("Completely different.", anchor), null);
  assert.equal(locate(text, { quote: "" }), null);
  assert.equal(locate(text, null), null);
}

// -- what the API sends -------------------------------------------------------------------------------
{
  const item = {
    id: "0b7f0000-0000-4000-8000-000000000000",
    artifact: "page",
    block_key: "b01",
    block_position: 1,
    quote: "q",
    prefix: "",
    suffix: "",
    start: 0,
    end: 1,
    note: null,
    created_at: "2026-11-26T10:00:00+00:00",
    updated_at: "2026-11-26T10:00:00+00:00",
  };
  assert.equal(checkHighlight(item), item);
  assert.throws(() => checkHighlight({ ...item, quote: 3 }), /no quote/);
  assert.throws(() => checkHighlight({ ...item, note: 3 }), /note/);
  assert.throws(() => checkHighlight({ ...item, block_position: "1" }), /block_position/);
  assert.throws(() => checkHighlight(null), /not an object/);
  assert.deepEqual(checkList({ artifact: "page", highlights: [item] }, "page"), [item]);
  assert.throws(() => checkList({ artifact: "page" }, "page"), /list is missing/);
  assert.throws(() => checkList({ artifact: "other", highlights: [] }, "page"), /“other”/);

  const long = { ...item, id: "a", quote: "a longer passage", created_at: "2026-11-26T09:00:00+00:00" };
  const short = { ...item, id: "b", quote: "short", created_at: "2026-11-26T08:00:00+00:00" };
  const newer = { ...short, id: "c", created_at: "2026-11-26T11:00:00+00:00" };
  const byId = new Map([long, short, newer].map((x) => [x.id, x]));
  assert.equal(pickHighlight(["a", "b"], byId).id, "b");
  assert.equal(pickHighlight(["a", "b", "c"], byId).id, "c");
  assert.equal(pickHighlight(["zzz"], byId), null);

  const later = { ...item, id: "x", block_position: 2, start: 0 };
  const orphan = { ...item, id: "y", block_position: null, start: 0 };
  const first = { ...item, id: "z", block_position: 1, start: 5 };
  assert.deepEqual(inPageOrder([orphan, later, first, item]).map((x) => x.id), [item.id, "z", "x", "y"]);

  assert.equal(noteValue("  "), null);
  assert.equal(noteValue(null), null);
  assert.equal(noteValue(" a note "), "a note");
}

console.log("highlight ok");
