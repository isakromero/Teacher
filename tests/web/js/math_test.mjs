// Run by tests/web/test_pages.py (`node math_test.mjs`): maths survives markdown parsing.
// protectMath() swaps maths for placeholders, marked parses, restoreSource() puts the TeX back;
// the TeX must come back byte for byte and KaTeX must render it.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const { protectMath, restoreSource } = await import(new URL("js/math.js", STATIC));
const { Marked } = await import(new URL("vendor/marked-15.0.12/marked.esm.js", STATIC));
const katex = (await import(new URL("vendor/katex-0.16.22/katex.mjs", STATIC))).default;

const marked = new Marked({ gfm: true, async: false });

function roundTrip(src) {
  const { text, maths } = protectMath(src);
  return { html: restoreSource(marked.parse(text), maths), maths };
}

// Underscores, backslashes, stars and pipes inside maths are untouched.
const inline = String.raw`\mathcal{L}_{i} = \sum_{j \ne i} w_{ij}^{*} \, x_j`;
const display = String.raw`\hat{y}_t = \frac{1}{N}\sum_{k=1}^{N} y_{t-k} \\ z_1 * z_2`;
{
  const src = `Loss $${inline}$ and *emph* here.\n\n$$\n${display}\n$$\n`;
  const { html, maths } = roundTrip(src);
  assert.equal(maths.length, 2);
  assert.equal(maths[0].tex, inline);
  assert.equal(maths[0].display, false);
  assert.equal(maths[1].tex.trim(), display);
  assert.equal(maths[1].display, true);
  assert.ok(html.includes(`$${inline}$`), html);
  assert.ok(html.includes("<em>emph</em>"), html);
  for (const m of maths) katex.renderToString(m.tex, { displayMode: m.display, throwOnError: true });
}

// \( \) and \[ \] delimiters.
{
  const { maths } = protectMath(String.raw`Inline \(a_1 + b_2\) and \[c_{n}^{2}\].`);
  assert.deepEqual(
    maths.map((m) => [m.tex, m.display]),
    [
      ["a_1 + b_2", false],
      ["c_{n}^{2}", true],
    ]
  );
}

// Currency, escaped dollars and code are not maths.
{
  const src = "Costs $5 and $10. Escaped \\$ sign. Code `$a_b$` stays.\n\n```\n$x_y$\n```\n";
  const { text, maths } = protectMath(src);
  assert.equal(maths.length, 0, JSON.stringify(maths));
  const html = marked.parse(text);
  assert.ok(html.includes("<code>$a_b$</code>"), html);
  assert.ok(html.includes("$x_y$"), html);
  assert.ok(html.includes("Escaped $ sign"), html);
}

// A pipe inside maths does not split a table cell.
{
  const { html } = roundTrip("| a | b |\n|---|---|\n| $|x|_1$ | y |\n");
  assert.ok(html.includes("<td>$|x|_1$</td>"), html);
}

console.log("math ok");
