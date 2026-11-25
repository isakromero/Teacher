// The document an `interactable` block's widget runs in, and the check of the messages it posts.
//
// Pure strings and functions: no DOM access, so this module also loads under node (see
// tests/web/js/interactable_test.mjs). blocks/interactable.js puts the document into an
// <iframe sandbox="allow-scripts" srcdoc=...>; the sandbox (an opaque origin: no cookies, no
// storage, no same-origin requests, no top navigation) is the security boundary, the CSP below
// additionally blocks every network fetch, and the server-side lint of the fragment is only a
// first filter.
//
// What the frame promises to the widget (keep the generation prompt in sync): the CSS variables
// --fg --muted --bg --surface --border --accent --accent-fg --series-1 --series-2 --series-3
// (light and dark), the app's UI font, and plain default styles for text, headings, buttons,
// inputs, selects, range sliders, labels, tables, svg/canvas and focus rings; on touch screens
// 44px controls, 16px text fields, and range sliders that a vertical swipe scrolls past. Widgets
// may override any of it.

/** The `source` tag on every message the shell posts to the host page. */
export const MESSAGE_SOURCE = "teacher-interactable";

/** Bounds of the frame height the host applies, in CSS pixels. */
export const MIN_FRAME_HEIGHT = 60;
export const MAX_FRAME_HEIGHT = 1600;
/** A reported height above this is not a measurement but garbage: the message is rejected. */
export const MAX_REPORTED_HEIGHT = 100000;
/** Longer error messages are cut to this many characters. */
export const MAX_ERROR_CHARS = 300;

export const FRAME_CSP = [
  "default-src 'none'",
  "script-src 'unsafe-inline'",
  "style-src 'unsafe-inline'",
  "img-src data: blob:",
  "media-src data: blob:",
  "font-src data:",
  "form-action 'none'",
  "base-uri 'none'",
].join("; ");

// Tokens copied from css/app.css (light :root and its prefers-color-scheme: dark override), so
// the widget matches the page around it; the series colours are for data (plots, bars, legends).
const LIGHT_TOKENS = {
  "--fg": "#1e1d1a",
  "--muted": "#64615a",
  "--bg": "#fbfaf7",
  "--surface": "#ffffff",
  "--border": "#dcd8cf",
  "--accent": "#2f5d8a",
  "--accent-fg": "#ffffff",
  "--series-1": "#2a78d6",
  "--series-2": "#eb6834",
  "--series-3": "#1baf7a",
};
const DARK_TOKENS = {
  "--fg": "#e8e6e1",
  "--muted": "#a19d94",
  "--bg": "#161615",
  "--surface": "#1f1f1d",
  "--border": "#393835",
  "--accent": "#8db8e6",
  "--accent-fg": "#10161d",
  "--series-1": "#3987e5",
  "--series-2": "#d95926",
  "--series-3": "#199e70",
};

/** Names of the CSS variables the frame defines (in both colour schemes). */
export const FRAME_CSS_VARIABLES = Object.freeze(Object.keys(LIGHT_TOKENS));

const declarations = (tokens) =>
  Object.entries(tokens)
    .map(([name, value]) => `  ${name}: ${value};`)
    .join("\n");

export const BASE_STYLESHEET = `
:root {
${declarations(LIGHT_TOKENS)}
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
${declarations(DARK_TOKENS)}
  }
}
*, *::before, *::after { box-sizing: border-box; }
html {
  background: transparent;
  overflow-x: hidden;
  -webkit-text-size-adjust: 100%;
}
body {
  margin: 6px;
  background: transparent;
  color: var(--fg);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-size: 1rem;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
h1, h2, h3, h4 { margin: 0.75em 0 0.4em; line-height: 1.25; font-weight: 650; }
h1 { font-size: 1.3rem; }
h2 { font-size: 1.15rem; }
h3, h4 { font-size: 1rem; }
p, ul, ol { margin: 0.5em 0; }
small, .muted { color: var(--muted); }
a { color: var(--accent); }
code, kbd, pre, samp { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em; }
pre { overflow-x: auto; }
output, .readout { font-variant-numeric: tabular-nums; font-weight: 600; }
button {
  font: inherit;
  font-size: 0.95rem;
  font-weight: 600;
  line-height: 1.2;
  min-height: 2.25rem;
  padding: 0.45rem 0.9rem;
  border-radius: 6px;
  border: 1px solid color-mix(in srgb, var(--muted) 55%, var(--border));
  background: var(--surface);
  color: var(--fg);
  cursor: pointer;
}
button:hover { background: color-mix(in srgb, var(--fg) 7%, var(--surface)); }
button[aria-pressed="true"], button.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-fg);
}
button:disabled { opacity: 0.55; cursor: not-allowed; }
input[type="text"], input[type="number"], input:not([type]), select, textarea {
  font: inherit;
  color: var(--fg);
  background: var(--bg);
  border: 1px solid color-mix(in srgb, var(--muted) 55%, var(--border));
  border-radius: 6px;
  padding: 0.35rem 0.5rem;
  max-width: 100%;
}
input[type="number"] { width: 7em; }
input[type="range"] { accent-color: var(--accent); max-width: 100%; vertical-align: middle; }
input[type="checkbox"], input[type="radio"] { accent-color: var(--accent); width: 1.05rem; height: 1.05rem; margin: 0; }
label { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 0.4rem 0.5rem; cursor: pointer; }
fieldset { border: 1px solid var(--border); border-radius: 6px; margin: 0.5em 0; padding: 0.5rem 0.75rem; }
table { border-collapse: collapse; max-width: 100%; margin: 0.5em 0; font-variant-numeric: tabular-nums; }
th, td { padding: 0.3rem 0.6rem; border-bottom: 1px solid var(--border); text-align: left; vertical-align: top; }
th { font-weight: 650; }
svg, canvas, img, video { max-width: 100%; }
svg, img, video { height: auto; }
svg { overflow: visible; }
:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
input:focus-visible, select:focus-visible, textarea:focus-visible { outline-offset: 1px; }
input[type="range"] { touch-action: pan-y; }
@media (pointer: coarse) {
  button, select, input[type="text"], input[type="number"], input:not([type]) { min-height: 2.75rem; }
  input[type="text"], input[type="number"], input:not([type]), select, textarea { font-size: 1rem; }
  input[type="range"] { min-height: 2.75rem; }
  input[type="checkbox"], input[type="radio"] { width: 1.35rem; height: 1.35rem; }
}
`;

// Runs before the fragment. Plain ES5-ish code in an IIFE so it leaves no globals behind.
// Heights: the root element's border box, which (unlike scrollHeight) follows the content down
// as well as up, because the root's height is its content's, not the viewport's. A height is
// posted only when it changed, so applying it to the frame (which changes only the viewport
// height, not the width the content lays out in) does not trigger another report.
// Browsers skip the rendering steps (where ResizeObserver callbacks run) for off-screen
// cross-origin frames, so heights are also posted synchronously on DOMContentLoaded and load,
// and after DOM mutations (MutationObserver callbacks are microtasks, which are not throttled):
// otherwise a frame below the fold would keep a stale height, and the host's watchdog would
// mistake it for a broken one.
// Error line numbers are made relative to the fragment (line 1 = its first line).
// This source must never contain a closing script tag or an HTML comment opener; the test checks.
const shellScript = (fragmentFirstLine) => `(function () {
  "use strict";
  var SOURCE = ${JSON.stringify(MESSAGE_SOURCE)};
  var FRAGMENT_FIRST_LINE = ${fragmentFirstLine};
  var host = window.parent;
  var lastHeight = -1;
  var pending = false;
  function post(message) {
    message.source = SOURCE;
    host.postMessage(message, "*");
  }
  function reportHeight() {
    var root = document.documentElement;
    var height = Math.ceil(root.getBoundingClientRect().height);
    if (height !== lastHeight) {
      lastHeight = height;
      post({ type: "height", height: height });
    }
  }
  function reportError(message) {
    post({ type: "error", message: String(message || "unknown error") });
  }
  function reportHeightSoon() {
    if (pending) return;
    pending = true;
    setTimeout(function () {
      pending = false;
      reportHeight();
    }, 0);
  }
  window.addEventListener("error", function (event) {
    var text = event.message || (event.error && String(event.error)) || "script error";
    var line = event.lineno - FRAGMENT_FIRST_LINE + 1;
    if (event.lineno && line >= 1) text += " (line " + line + " of the widget)";
    reportError(text);
  });
  window.addEventListener("unhandledrejection", function (event) {
    var reason = event.reason;
    reportError("unhandled promise rejection: " + (reason && reason.message ? reason.message : String(reason)));
  });
  document.addEventListener("securitypolicyviolation", function (event) {
    reportError("blocked by the frame's security policy: " + event.effectiveDirective + " " + (event.blockedURI || ""));
  });
  document.addEventListener("DOMContentLoaded", reportHeight);
  window.addEventListener("load", reportHeight);
  new MutationObserver(reportHeightSoon).observe(document.documentElement, {
    subtree: true,
    childList: true,
    attributes: true,
    characterData: true,
  });
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(reportHeight).observe(document.documentElement);
  } else {
    reportError("this browser has no ResizeObserver, so the frame cannot follow the widget's size");
  }
})();`;

/**
 * The complete HTML document for a widget fragment (markup, a <style> and a <script>, as inside
 * <body>). The fragment is inserted verbatim as HTML, after </head>: it cannot precede or replace
 * the CSP, because a <meta http-equiv> CSP is only honoured in <head> and the head is closed
 * before the fragment starts (the server also rejects <meta>, <head> and friends). It is HTML,
 * not a string inside a script, so it needs no escaping: a "</script>" in it closes the
 * fragment's own script, exactly as it would in any page. The CSP meta is the first element of
 * head, so it applies before anything else in the document is fetched or run; the charset meta
 * after it is informational only (srcdoc is already decoded text).
 */
export function buildInteractableDocument(html) {
  if (typeof html !== "string") throw new TypeError("interactable html must be a string");
  return `${DOCUMENT_HEAD}${html}
</body>
</html>
`;
}

const documentHead = (script) => `<!doctype html>
<html lang="en">
<head>
<meta http-equiv="Content-Security-Policy" content="${FRAME_CSP}">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>${BASE_STYLESHEET}</style>
<script>${script}</script>
</head>
<body>
`;

// Everything before the fragment is fixed, so the fragment's first line number is a constant;
// the number's digits add no newlines, so measuring with a placeholder gives the right count.
const FRAGMENT_FIRST_LINE = documentHead(shellScript(0)).split("\n").length;
export const SHELL_SCRIPT = shellScript(FRAGMENT_FIRST_LINE);
const DOCUMENT_HEAD = documentHead(SHELL_SCRIPT);

function clampHeight(height) {
  return Math.min(MAX_FRAME_HEIGHT, Math.max(MIN_FRAME_HEIGHT, Math.ceil(height)));
}

/**
 * Validate one message's data from the frame. Returns `{ type: "height", height }` (height
 * clamped to MIN_FRAME_HEIGHT..MAX_FRAME_HEIGHT), `{ type: "error", message }` (cut to
 * MAX_ERROR_CHARS), or null for anything else, which the host must ignore. Checking that the
 * message came from the right frame (event.source) is the caller's job.
 */
export function parseFrameMessage(data) {
  if (data === null || typeof data !== "object" || Array.isArray(data)) return null;
  if (data.source !== MESSAGE_SOURCE) return null;
  if (data.type === "height") {
    const height = data.height;
    if (typeof height !== "number" || !Number.isFinite(height)) return null;
    if (height < 0 || height > MAX_REPORTED_HEIGHT) return null;
    return { type: "height", height: clampHeight(height) };
  }
  if (data.type === "error") {
    if (typeof data.message !== "string") return null;
    let message = data.message.trim() || "(no message)";
    if (message.length > MAX_ERROR_CHARS) message = `${message.slice(0, MAX_ERROR_CHARS - 1)}…`;
    return { type: "error", message };
  }
  return null;
}
