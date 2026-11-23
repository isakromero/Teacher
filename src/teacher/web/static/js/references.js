// Pure helpers for the tutor chat's quoted references (no DOM globals, so node can test them):
// turning a selected DOM fragment into plain text (KaTeX maths back to its $TeX$ source),
// the limits from the API contract, and short previews.

export const MAX_REFERENCES = 5;
export const MAX_REFERENCE_CHARS = 2000;
export const MAX_MESSAGE_CHARS = 8000;
export const PREVIEW_CHARS = 80;

const TEXT_NODE = 3;
const ELEMENT_NODE = 1;
const FRAGMENT_NODE = 11;

// Elements whose text is not part of the study material the reader selected.
const SKIP = new Set(["BUTTON", "INPUT", "TEXTAREA", "SELECT", "SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT"]);
// Elements that end a paragraph (blank line) or a line.
const PARAGRAPH = new Set([
  "P", "H1", "H2", "H3", "H4", "H5", "H6", "UL", "OL", "BLOCKQUOTE", "PRE", "TABLE", "FIGURE", "HR",
]);
const LINE = new Set([
  "LI", "TR", "DIV", "SECTION", "ARTICLE", "HEADER", "FOOTER", "FIELDSET", "DL", "DT", "DD",
  "FIGCAPTION", "CAPTION",
]);

function hasClass(node, name) {
  const cls = typeof node.getAttribute === "function" ? node.getAttribute("class") : null;
  return Boolean(cls) && cls.split(/\s+/).includes(name);
}

function tagName(node) {
  return String(node.nodeName || "").toUpperCase();
}

function find(node, test) {
  for (const child of node.childNodes || []) {
    if (child.nodeType === ELEMENT_NODE) {
      if (test(child)) return child;
      const found = find(child, test);
      if (found) return found;
    }
  }
  return null;
}

/** The TeX source of a rendered KaTeX element (`.katex`), or null if it carries none. */
export function katexSource(el) {
  const annotation = find(
    el,
    (n) => tagName(n) === "ANNOTATION" && n.getAttribute("encoding") === "application/x-tex"
  );
  if (!annotation) return null;
  const math = tagName(el) === "MATH" ? el : find(el, (n) => tagName(n) === "MATH");
  return {
    tex: textContent(annotation).trim(),
    display: Boolean(math) && math.getAttribute("display") === "block",
  };
}

function textContent(node) {
  if (node.nodeType === TEXT_NODE) return node.nodeValue || "";
  let out = "";
  for (const child of node.childNodes || []) out += textContent(child);
  return out;
}

/**
 * Plain text of a DOM node or fragment as the reader sees it: paragraphs separated by a blank
 * line, soft line breaks as spaces, maths as `$tex$` / `$$tex$$`, buttons and form controls
 * left out. Works on anything with nodeType/nodeName/childNodes/getAttribute/nodeValue.
 */
export function extractText(root) {
  // Strings are text; numbers are breaks between blocks (1 = new line, 2 = blank line).
  // Adjacent breaks merge into the larger one, so nesting and the whitespace between
  // elements do not pile up empty lines.
  const parts = [];
  function walk(node, pre) {
    if (node.nodeType === TEXT_NODE) {
      const value = node.nodeValue || "";
      parts.push(pre ? value : value.replace(/\s+/g, " "));
      return;
    }
    if (node.nodeType !== ELEMENT_NODE && node.nodeType !== FRAGMENT_NODE) return;
    let sep = 0;
    if (node.nodeType === ELEMENT_NODE) {
      const tag = tagName(node);
      if (SKIP.has(tag)) return;
      if (hasClass(node, "katex")) {
        const source = katexSource(node);
        if (source) {
          if (source.display) parts.push(1, `$$${source.tex}$$`, 1);
          else parts.push(`$${source.tex}$`);
          return;
        }
      }
      if (tag === "BR") {
        parts.push("\n");
        return;
      }
      if (tag === "PRE") pre = true;
      sep = PARAGRAPH.has(tag) ? 2 : LINE.has(tag) ? 1 : 0;
    }
    if (sep) parts.push(sep);
    for (const child of node.childNodes || []) walk(child, pre);
    if (sep) parts.push(sep);
  }
  walk(root, false);

  let out = "";
  let pendingBreak = 0;
  for (const part of parts) {
    if (typeof part === "number") {
      pendingBreak = Math.max(pendingBreak, part);
    } else if (pendingBreak && !part.trim()) {
      continue; // whitespace between blocks
    } else {
      if (pendingBreak && out) out += "\n".repeat(pendingBreak);
      pendingBreak = 0;
      out += part;
    }
  }
  return normalizeText(out);
}

/** Collapse runs of spaces within lines, trim lines, at most one blank line in a row. */
export function normalizeText(text) {
  return String(text ?? "")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.replace(/[ \t\f\v ]+/g, " ").trim())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** Cut `text` to at most `max` UTF-16 units without splitting a surrogate pair. */
function cut(text, max) {
  if (text.length <= max) return text;
  let end = max;
  const code = text.charCodeAt(end - 1);
  if (code >= 0xd800 && code <= 0xdbff) end -= 1;
  return text.slice(0, end);
}

/** Limit a selection to the API's maximum reference length. */
export function clampReference(text, max = MAX_REFERENCE_CHARS) {
  const clean = String(text ?? "");
  if (clean.length <= max) return { text: clean, truncated: false };
  return { text: cut(clean, max).trimEnd(), truncated: true };
}

/** A one-line preview of at most `max` characters (plus an ellipsis when shortened). */
export function previewText(text, max = PREVIEW_CHARS) {
  const line = String(text ?? "").replace(/\s+/g, " ").trim();
  if (line.length <= max) return line;
  return `${cut(line, max).trimEnd()}…`;
}

/**
 * Add `ref` ({block_key, text, ...}) to `list` without mutating it.
 * Returns {list, error}: `error` is a message for the reader when the reference was refused.
 */
export function addReference(list, ref, max = MAX_REFERENCES) {
  if (!ref || !ref.block_key || !ref.text) {
    return { list, error: "Nothing was selected." };
  }
  if (list.some((r) => r.block_key === ref.block_key && r.text === ref.text)) {
    return { list, error: "That passage is already attached." };
  }
  if (list.length >= max) {
    return {
      list,
      error: `You can attach at most ${max} passages to one message. Remove one to add another.`,
    };
  }
  return { list: [...list, ref], error: null };
}
