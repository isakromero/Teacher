// Markdown + maths rendering: protect maths -> marked -> DOMPurify -> KaTeX into the DOM.
//
// Maths is rendered after sanitising, by replacing placeholder characters in TEXT NODES with
// KaTeX output, so KaTeX markup never passes through (or around) the sanitiser as a string.

import { Marked } from "../vendor/marked-15.0.12/marked.esm.js";
import DOMPurify from "../vendor/dompurify-3.2.6/purify.es.mjs";
import katex from "../vendor/katex-0.16.22/katex.mjs";
import { PH_OPEN, PLACEHOLDER_SPLIT, protectMath, restoreSource } from "./math.js";

const marked = new Marked({ gfm: true, breaks: false, async: false });

const ABSOLUTE_URL = /^(?:[a-z][a-z0-9+.-]*:|\/|#)/i;

function renderTex({ tex, display }) {
  const el = document.createElement("span");
  el.className = display ? "math math-display" : "math math-inline";
  try {
    katex.render(tex, el, { displayMode: display, throwOnError: false, output: "htmlAndMathml" });
  } catch (err) {
    el.className = "math-error";
    el.textContent = display ? `$$${tex}$$` : `$${tex}$`;
    el.title = `Could not render maths: ${err.message}`;
  }
  return el;
}

function insertMath(root, maths) {
  if (!maths.length) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) {
    if (walker.currentNode.nodeValue.includes(PH_OPEN)) textNodes.push(walker.currentNode);
  }
  for (const node of textNodes) {
    const parts = node.nodeValue.split(new RegExp(PLACEHOLDER_SPLIT.source, "g"));
    const frag = document.createDocumentFragment();
    parts.forEach((part, k) => {
      if (k % 2 === 0) {
        if (part) frag.append(part);
      } else {
        const item = maths[Number(part)];
        frag.append(item ? renderTex(item) : part);
      }
    });
    node.replaceWith(frag);
  }
  for (const el of root.querySelectorAll("*")) {
    for (const attr of [...el.attributes]) {
      if (attr.value.includes(PH_OPEN)) el.setAttribute(attr.name, restoreSource(attr.value, maths));
    }
  }
}

function fixLinks(root, baseUrl) {
  for (const a of root.querySelectorAll("a[href]")) {
    const href = a.getAttribute("href");
    if (baseUrl && !ABSOLUTE_URL.test(href)) a.setAttribute("href", baseUrl + href);
    if (/^https?:/i.test(a.getAttribute("href"))) {
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener noreferrer");
    }
  }
  if (baseUrl) {
    for (const img of root.querySelectorAll("img[src]")) {
      const src = img.getAttribute("src");
      if (!ABSOLUTE_URL.test(src)) img.setAttribute("src", baseUrl + src);
    }
  }
}

/**
 * Render markdown (with $...$ / $$...$$ maths) to a sanitised element.
 * `inline: true` renders without block wrappers (for options and short labels).
 * `baseUrl` (e.g. "/api/artifacts/<slug>/") resolves relative image and link URLs.
 */
export function renderMarkdown(source, { inline = false, baseUrl = "" } = {}) {
  const { text, maths } = protectMath(String(source ?? ""));
  const html = inline ? marked.parseInline(text) : marked.parse(text);
  const template = document.createElement("template");
  template.innerHTML = DOMPurify.sanitize(html);
  const fragment = template.content;
  insertMath(fragment, maths);
  fixLinks(fragment, baseUrl);
  const wrapper = document.createElement(inline ? "span" : "div");
  wrapper.className = inline ? "md md-inline" : "md";
  wrapper.append(fragment);
  return wrapper;
}
