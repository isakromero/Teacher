// Run with `node figure_test.mjs`: the checked view of a figure block's content (image URL with
// encoded segments, alt text, rejection of content not in the stored shape).

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const { altText, assetUrl, figureModel } = await import(new URL("js/figure_model.js", STATIC));

const content = {
  asset: "figures/figure-3-0123456789.png",
  label: "Figure 3",
  source_caption: "Overview of the\n  model architecture.",
  page: 4,
  width: 900,
  height: 420,
  caption: "Follow the **encoder** from left to right.",
};

const model = figureModel(content, "/api/artifacts/my-page/");
assert.equal(model.src, "/api/artifacts/my-page/assets/figures/figure-3-0123456789.png");
assert.equal(model.alt, "Figure 3 from the source: Overview of the model architecture.");
assert.equal(model.label, "Figure 3");
assert.equal(model.caption, content.caption);
assert.equal(model.sourceCaption, content.source_caption);
assert.deepEqual([model.page, model.width, model.height], [4, 900, 420]);

// Segments are encoded; the separators are kept.
assert.equal(assetUrl("/a/", "x_y/fig-1.2.png"), "/a/assets/x_y/fig-1.2.png");
assert.equal(altText("Figure 1", "  A  plot.  "), "Figure 1 from the source: A plot.");

// Paths that leave the assets folder, absolute paths and other formats are refused.
for (const asset of ["../figure.png", "figures/../../x.png", "/etc/x.png", "figures/./x.png", "x.svg",
  "figures/x.PNG", "", null, "a b.png", "https://example.com/x.png"]) {
  assert.throws(() => assetUrl("/a/", asset), /asset/, String(asset));
}
assert.throws(() => assetUrl("/a", "x.png"), /base URL/);

// Every field is checked.
for (const [field, value] of [
  ["label", " "],
  ["source_caption", ""],
  ["caption", 3],
  ["page", 0],
  ["width", 1.5],
  ["height", "420"],
  ["asset", "figure.jpg"],
]) {
  assert.throws(() => figureModel({ ...content, [field]: value }, "/a/"), new RegExp(field), field);
}
assert.throws(() => figureModel(null, "/a/"), /missing/);

console.log("figure ok");
