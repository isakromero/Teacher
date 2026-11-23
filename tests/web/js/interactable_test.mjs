// Run with `node interactable_test.mjs`: the document an interactable widget runs in (CSP first,
// the fragment verbatim inside body, the promised CSS variables) and the validation of the
// messages the frame posts to the host page.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const shell = await import(new URL("js/interactable_shell.js", STATIC));
const {
  BASE_STYLESHEET,
  FRAME_CSP,
  FRAME_CSS_VARIABLES,
  MAX_FRAME_HEIGHT,
  MESSAGE_SOURCE,
  MIN_FRAME_HEIGHT,
  SHELL_SCRIPT,
  buildInteractableDocument,
  parseFrameMessage,
} = shell;

const count = (haystack, needle) => haystack.split(needle).length - 1;

// -- the document -------------------------------------------------------------------------------

const fragment = [
  '<div id="w"><input type="range" id="r"><output id="o"></output></div>',
  "<style>#w { color: var(--series-1); }</style>",
  '<script>const s = "</scr" + "ipt>"; document.getElementById("o").textContent = "1 < 2 && 3 > 2";</script>',
].join("\n");
const doc = buildInteractableDocument(fragment);

assert.ok(doc.startsWith("<!doctype html>"), doc.slice(0, 40));
assert.throws(() => buildInteractableDocument(undefined), TypeError);

{
  const headStart = doc.indexOf("<head>");
  const headEnd = doc.indexOf("</head>");
  const bodyStart = doc.indexOf("<body>");
  const bodyEnd = doc.lastIndexOf("</body>");
  assert.ok(headStart > 0 && headStart < headEnd && headEnd < bodyStart && bodyStart < bodyEnd);

  // The CSP meta is the first element of head: nothing but whitespace between them.
  const csp = `<meta http-equiv="Content-Security-Policy" content="${FRAME_CSP}">`;
  assert.equal(count(doc, csp), 1);
  const cspAt = doc.indexOf(csp);
  assert.equal(doc.slice(headStart + "<head>".length, cspAt).trim(), "");
  assert.ok(cspAt < doc.indexOf('<meta charset="utf-8">'));
  assert.ok(cspAt < doc.indexOf("<style>"));
  assert.ok(cspAt < doc.indexOf("<script>"));

  // The fragment: verbatim (not escaped), exactly once, inside body, after the head is closed.
  assert.equal(count(doc, fragment), 1);
  const fragmentAt = doc.indexOf(fragment);
  assert.ok(fragmentAt > bodyStart && fragmentAt + fragment.length < bodyEnd);
  assert.ok(fragmentAt > cspAt && fragmentAt > headEnd);
  // Only the fragment's own script follows the shell's.
  assert.ok(doc.indexOf(SHELL_SCRIPT) < fragmentAt);
  // The shell reports error lines relative to the fragment: it knows the fragment's first line.
  const firstLine = Number(/FRAGMENT_FIRST_LINE = (\d+);/.exec(SHELL_SCRIPT)[1]);
  assert.equal(doc.split("\n")[firstLine - 1], fragment.split("\n")[0]);
  assert.equal(buildInteractableDocument("").split("\n").length, doc.split("\n").length - 2);
}

for (const directive of [
  "default-src 'none'",
  "script-src 'unsafe-inline'",
  "style-src 'unsafe-inline'",
  "img-src data: blob:",
  "media-src data: blob:",
  "font-src data:",
  "form-action 'none'",
  "base-uri 'none'",
]) {
  assert.ok(FRAME_CSP.split("; ").includes(directive), directive);
}
assert.ok(!FRAME_CSP.includes('"'), "the CSP sits in a double-quoted attribute");

// The shell's inline script and style cannot end their element early or open a comment.
assert.ok(!/<\/script/i.test(SHELL_SCRIPT), "literal closing script tag in the shell script");
assert.ok(!/<!--|<script/i.test(SHELL_SCRIPT));
assert.ok(!/<\/style/i.test(BASE_STYLESHEET));
assert.ok(SHELL_SCRIPT.includes(JSON.stringify(MESSAGE_SOURCE)));

// -- the promised CSS variables -----------------------------------------------------------------

const PROMISED = [
  "--fg",
  "--muted",
  "--bg",
  "--surface",
  "--border",
  "--accent",
  "--accent-fg",
  "--series-1",
  "--series-2",
  "--series-3",
];
assert.deepEqual([...FRAME_CSS_VARIABLES].sort(), [...PROMISED].sort());
{
  const darkAt = BASE_STYLESHEET.indexOf("@media (prefers-color-scheme: dark)");
  assert.ok(darkAt > 0);
  const lightBlock = BASE_STYLESHEET.slice(BASE_STYLESHEET.indexOf(":root {"), darkAt);
  const darkBlock = BASE_STYLESHEET.slice(darkAt, BASE_STYLESHEET.indexOf("}\n}", darkAt));
  const defined = (block, name) => new RegExp(`(^|\\s)${name}:\\s*#[0-9a-f]{6};`, "m").test(block);
  for (const name of PROMISED) {
    assert.ok(defined(lightBlock, name), `light ${name}`);
    assert.ok(defined(darkBlock, name), `dark ${name}`);
  }
  assert.match(lightBlock, /--series-1: #2a78d6;[\s\S]*--series-2: #eb6834;[\s\S]*--series-3: #1baf7a;/);
  assert.match(darkBlock, /--series-1: #3987e5;[\s\S]*--series-2: #d95926;[\s\S]*--series-3: #199e70;/);
  assert.match(BASE_STYLESHEET, /color-scheme: light dark/);
  assert.match(BASE_STYLESHEET, /accent-color: var\(--accent\)/);
  assert.match(BASE_STYLESHEET, /:focus-visible/);
}

// -- message validation -------------------------------------------------------------------------

const msg = (fields) => ({ source: MESSAGE_SOURCE, ...fields });

assert.deepEqual(parseFrameMessage(msg({ type: "height", height: 321 })), { type: "height", height: 321 });
assert.deepEqual(parseFrameMessage(msg({ type: "height", height: 320.2 })), { type: "height", height: 321 });
assert.deepEqual(parseFrameMessage(msg({ type: "height", height: 0 })), { type: "height", height: MIN_FRAME_HEIGHT });
assert.deepEqual(parseFrameMessage(msg({ type: "height", height: 5000 })), {
  type: "height",
  height: MAX_FRAME_HEIGHT,
});
assert.deepEqual(parseFrameMessage(msg({ type: "error", message: "  x is not defined (line 3) " })), {
  type: "error",
  message: "x is not defined (line 3)",
});
{
  const long = parseFrameMessage(msg({ type: "error", message: "e".repeat(5000) }));
  assert.equal(long.type, "error");
  assert.ok(long.message.length <= 300);
}

for (const bad of [
  null,
  undefined,
  "height",
  42,
  [],
  [MESSAGE_SOURCE, "height", 100],
  { type: "height", height: 100 },
  { source: "something-else", type: "height", height: 100 },
  { source: MESSAGE_SOURCE.toUpperCase(), type: "height", height: 100 },
  msg({ type: "resize", height: 100 }),
  msg({ type: "HEIGHT", height: 100 }),
  msg({}),
  msg({ type: "height" }),
  msg({ type: "height", height: "100" }),
  msg({ type: "height", height: Number.NaN }),
  msg({ type: "height", height: Number.POSITIVE_INFINITY }),
  msg({ type: "height", height: -1 }),
  msg({ type: "height", height: 1e9 }),
  msg({ type: "height", height: null }),
  msg({ type: "error" }),
  msg({ type: "error", message: 7 }),
  msg({ type: "error", message: { toString: () => "x" } }),
  msg({ type: "error", message: null }),
]) {
  assert.equal(parseFrameMessage(bad), null, JSON.stringify(bad));
}

console.log("interactable ok");
