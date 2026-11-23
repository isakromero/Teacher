// The study page's glossary (artifact status `ready`): marks glossary terms in the rendered
// blocks, shows a term's definition in a popover, and fills the "Glossary" panel above the
// blocks (an alphabetical list, or a "Build glossary" button when none has been built).
//
// API: GET /api/artifacts/{slug}/glossary -> {terms: [{term, kind, expansion, definition,
// aliases}], generated}; it lists only terms that occur in what the reader can see, so the
// reader calls `refresh()` whenever a gate is cleared or blocks unlock. POST builds (or
// rebuilds) the glossary and returns the same plus `dropped_count`.
//
// Marking: inside each block's rendered markdown (`.md`: prose, questions, options,
// explanations, model answers, feedback), text nodes are searched with glossary_match.js and
// a term is wrapped in a focusable <span class="gloss-term">: every occurrence of its shorthand
// forms ("SFT"), and the first occurrence per block of its other forms ("supervised fine-tuning").
// Code, links, KaTeX output, form controls and existing marks are skipped. The span holds
// exactly the matched text, so the chat's selection text is unchanged, and the popover lives on
// <body>, outside the blocks, so its text can never be selected as part of a block. A
// MutationObserver re-marks a block whenever its content is re-rendered (gates repaint on
// submit) and marks blocks as they are added.
//
// Study history (activity.js): opening the panel logs `glossary_opened`, and showing a term's
// popover `glossary_term_viewed` (at most once per term every TERM_LOG_MS, so moving the pointer
// back and forth over a term is one look-up).

import { logEvent } from "./activity.js";
import { api, describeError, seg } from "./api.js";
import { byId, h, plural, setChildren, setError } from "./dom.js";
import { compileTerms, findMatches } from "./glossary_match.js";

const TERM_CLASS = "gloss-term";
const SHOW_DELAY_MS = 300;
const HIDE_DELAY_MS = 200;
const MARGIN_PX = 8;
const GAP_PX = 6;
const MAX_WIDTH_PX = 352;
const TERM_LOG_MS = 30_000;

const SKIP_TAGS = new Set([
  "A", "CODE", "PRE", "KBD", "SAMP", "SCRIPT", "STYLE", "TEMPLATE", "BUTTON", "INPUT",
  "TEXTAREA", "SELECT", "OPTION", "MATH", "ANNOTATION", "SVG",
]);
const SKIP_CLASSES = ["katex", "katex-display", "math", "math-error", TERM_CLASS];
const OBSERVE = { childList: true, subtree: true };

/** Whether `el` (inside `root`) or an ancestor up to `root` is never marked. */
function skipped(el, root) {
  for (let node = el; node; node = node.parentElement) {
    if (SKIP_TAGS.has(node.tagName.toUpperCase())) return true;
    if (SKIP_CLASSES.some((name) => node.classList.contains(name))) return true;
    if (node === root) return false;
  }
  return false;
}

/** Whether `el` sits in a hidden part of `block` (e.g. a passed gate's answer form). */
function hiddenIn(el, block) {
  const hidden = el.closest("[hidden]");
  return Boolean(hidden) && block.contains(hidden);
}

/** Text nodes inside `root` (part of `block`) that may hold a term, in document order. */
function textNodes(root, block) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      const parent = node.parentElement;
      return skipped(parent, root) || hiddenIn(parent, block)
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  return nodes;
}

/** The outermost rendered-markdown elements of a block. */
function markdownRoots(block) {
  return [...block.querySelectorAll(".md")].filter((el) => {
    const outer = el.parentElement && el.parentElement.closest(".md");
    return !outer || !block.contains(outer);
  });
}

/** Mark the terms in `block`: every shorthand occurrence, and each term's first visible
 * long-form occurrence (marks already there are kept; one inside a hidden part does not count,
 * so the term can be marked where the reader sees it). */
function markBlock(block, compiled) {
  if (!compiled.length) return;
  const seen = new Set(
    [...block.querySelectorAll(`.${TERM_CLASS}`)]
      .filter((el) => el.dataset.form === "long" && !hiddenIn(el, block))
      .map((el) => el.dataset.term)
  );
  for (const root of markdownRoots(block)) {
    for (const node of textNodes(root, block)) {
      const text = node.nodeValue;
      const matches = findMatches(text, compiled, seen);
      if (!matches.length) continue;
      const frag = document.createDocumentFragment();
      let at = 0;
      for (const match of matches) {
        if (match.start > at) frag.append(text.slice(at, match.start));
        frag.append(
          h(
            "span",
            {
              class: TERM_CLASS,
              tabindex: "0",
              dataset: { term: match.key, form: match.every ? "short" : "long" },
            },
            text.slice(match.start, match.end)
          )
        );
        if (!match.every) seen.add(match.key);
        at = match.end;
      }
      if (at < text.length) frag.append(text.slice(at));
      node.replaceWith(frag);
    }
  }
}

function unmarkAll(blocksRoot) {
  const parents = new Set();
  for (const el of blocksRoot.querySelectorAll(`.${TERM_CLASS}`)) {
    parents.add(el.parentNode);
    el.replaceWith(document.createTextNode(el.textContent));
  }
  for (const parent of parents) parent.normalize();
}

// -- the popover -----------------------------------------------------------------------------

function createPopover(blocksRoot, lookup, onShow) {
  const pop = h("div", { id: "glossary-popover", class: "gloss-popover", role: "tooltip", hidden: true });
  document.body.append(pop);
  let anchor = null;
  let showTimer = 0;
  let hideTimer = 0;
  let lastPointer = "mouse";
  let pointerDown = false; // focus that comes from a press is handled by the click instead

  const clearTimers = () => {
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    showTimer = hideTimer = 0;
  };

  function position() {
    if (!anchor) return;
    if (!anchor.isConnected) {
      hide();
      return;
    }
    const rects = anchor.getClientRects();
    const rect = rects.length ? rects[0] : anchor.getBoundingClientRect();
    const vw = document.documentElement.clientWidth || window.innerWidth;
    const vh = window.innerHeight;
    if (rect.bottom < 0 || rect.top > vh) {
      hide(); // scrolled far away from the term
      return;
    }
    pop.style.maxWidth = `${Math.max(0, Math.min(MAX_WIDTH_PX, vw - 2 * MARGIN_PX))}px`;
    pop.style.left = "0px";
    pop.style.top = "0px";
    const width = pop.offsetWidth;
    const height = pop.offsetHeight;
    let top = rect.bottom + GAP_PX;
    let placement = "below";
    if (top + height > vh - MARGIN_PX && rect.top - GAP_PX - height >= MARGIN_PX) {
      top = rect.top - GAP_PX - height;
      placement = "above";
    }
    let left = rect.left + rect.width / 2 - width / 2;
    left = Math.max(MARGIN_PX, Math.min(left, vw - width - MARGIN_PX));
    pop.style.left = `${Math.round(left)}px`;
    pop.style.top = `${Math.round(top)}px`;
    pop.dataset.placement = placement;
  }

  function show(term) {
    clearTimers();
    const entry = lookup(term.dataset.term);
    if (!entry) return;
    if (anchor && anchor !== term) anchor.removeAttribute("aria-describedby");
    anchor = term;
    setChildren(
      pop,
      h(
        "p",
        { class: "gloss-pop-head" },
        h("strong", { class: "gloss-pop-term" }, entry.term),
        entry.expansion ? [" — ", h("span", { class: "gloss-pop-expansion" }, entry.expansion)] : null
      ),
      h("p", { class: "gloss-pop-definition" }, entry.definition)
    );
    pop.hidden = false;
    term.setAttribute("aria-describedby", pop.id);
    term.classList.add("is-active");
    position();
    onShow(entry);
  }

  function hide() {
    clearTimers();
    if (anchor) {
      anchor.removeAttribute("aria-describedby");
      anchor.classList.remove("is-active");
    }
    anchor = null;
    pop.hidden = true;
  }

  const isOpen = () => !pop.hidden && anchor !== null;
  const termOf = (target) =>
    target instanceof Element ? target.closest(`.${TERM_CLASS}`) : null;

  function scheduleShow(term) {
    clearTimers();
    showTimer = setTimeout(() => show(term), SHOW_DELAY_MS);
  }
  function scheduleHide() {
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hide, HIDE_DELAY_MS);
  }

  blocksRoot.addEventListener("pointerover", (event) => {
    const term = termOf(event.target);
    if (!term || event.pointerType === "touch") return;
    if (anchor === term) {
      clearTimeout(hideTimer);
      return;
    }
    scheduleShow(term);
  });
  blocksRoot.addEventListener("pointerout", (event) => {
    const term = termOf(event.target);
    if (!term || event.pointerType === "touch") return;
    if (event.relatedTarget instanceof Node && term.contains(event.relatedTarget)) return;
    if (anchor === term) scheduleHide();
    else clearTimeout(showTimer);
  });
  pop.addEventListener("pointerenter", () => clearTimeout(hideTimer));
  pop.addEventListener("pointerleave", (event) => {
    if (event.pointerType !== "touch") scheduleHide();
  });

  blocksRoot.addEventListener("focusin", (event) => {
    const term = termOf(event.target);
    if (term && anchor !== term && !pointerDown) show(term); // keyboard focus
  });
  blocksRoot.addEventListener("focusout", (event) => {
    const term = termOf(event.target);
    if (term && anchor === term && !pop.contains(event.relatedTarget)) hide();
  });
  blocksRoot.addEventListener("click", (event) => {
    const term = termOf(event.target);
    if (!term) return;
    if (isOpen() && anchor === term && lastPointer === "touch") hide();
    else show(term);
  });

  const release = () => {
    pointerDown = false;
  };
  document.addEventListener("pointerup", release, true);
  document.addEventListener("pointercancel", release, true);
  document.addEventListener(
    "pointerdown",
    (event) => {
      lastPointer = event.pointerType || "mouse";
      pointerDown = true;
      if (!isOpen()) return;
      const target = event.target;
      if (target instanceof Node && (pop.contains(target) || anchor.contains(target))) return;
      hide();
    },
    true
  );
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isOpen()) hide();
  });
  window.addEventListener("scroll", () => isOpen() && position(), { capture: true, passive: true });
  window.addEventListener("resize", () => isOpen() && position());

  return { hide, isOpen };
}

// -- the panel and the whole --------------------------------------------------------------------

function byTerm(a, b) {
  return a.term.localeCompare(b.term, undefined, { sensitivity: "base" }) || a.term.localeCompare(b.term);
}

function checkGlossary(data) {
  if (!data || !Array.isArray(data.terms) || typeof data.generated !== "boolean") {
    throw new Error("Unexpected response from the server: the glossary is missing its terms.");
  }
  for (const item of data.terms) {
    if (!item || typeof item.term !== "string" || typeof item.definition !== "string") {
      throw new Error("Unexpected response from the server: a glossary term is malformed.");
    }
  }
  return data;
}

export function mountGlossary(artifact) {
  const url = `/api/artifacts/${seg(artifact.slug)}/glossary`;
  const blocksRoot = byId("blocks");
  const panel = byId("glossary");
  const count = byId("glossary-count");
  const list = byId("glossary-list");
  const emptyNote = byId("glossary-empty");
  const buildBox = byId("glossary-build");
  const buildButton = byId("glossary-build-button");
  const buildStatus = byId("glossary-build-status");
  const errorBox = byId("glossary-error");

  let terms = [];
  let byKey = new Map();
  let compiled = [];
  let signature = "";
  let building = false;
  let loads = 0;

  const logged = new Map(); // term -> when its look-up was last logged
  const popover = createPopover(
    blocksRoot,
    (key) => byKey.get(key),
    (entry) => {
      const now = Date.now();
      if (now - (logged.get(entry.term) ?? -Infinity) < TERM_LOG_MS) return;
      logged.set(entry.term, now);
      logEvent("glossary_term_viewed", { artifact: artifact.slug, payload: { term: entry.term } });
    }
  );
  // A click on the summary toggles the panel (keyboard activation clicks too); `open` is still
  // the state before the click. Opening it to show a load error is not the reader's doing.
  panel.querySelector("summary").addEventListener("click", () => {
    if (!panel.open) logEvent("glossary_opened", { artifact: artifact.slug });
  });

  const observer = new MutationObserver((records) => {
    const blocks = new Set();
    for (const record of records) {
      const target = record.target.nodeType === Node.ELEMENT_NODE ? record.target : record.target.parentElement;
      if (target === blocksRoot) {
        for (const node of record.addedNodes) {
          if (node.nodeType === Node.ELEMENT_NODE && node.dataset.blockKey) blocks.add(node);
        }
      } else if (target) {
        const block = target.closest("[data-block-key]");
        if (block && block.parentElement === blocksRoot) blocks.add(block);
      }
    }
    if (blocks.size) quietly(() => blocks.forEach((block) => markBlock(block, compiled)));
  });

  /** Change the blocks without the observer seeing our own changes. */
  function quietly(change) {
    observer.disconnect();
    try {
      change();
    } finally {
      observer.observe(blocksRoot, OBSERVE);
    }
  }

  function blocks() {
    return [...blocksRoot.children].filter((el) => el.dataset && el.dataset.blockKey);
  }

  function renderPanel(generated) {
    panel.hidden = false;
    setError(errorBox, null);
    buildBox.hidden = generated;
    list.hidden = !generated || !terms.length;
    emptyNote.hidden = !generated || terms.length > 0;
    count.textContent = generated ? plural(terms.length, "term") : "not built yet";
    setChildren(
      list,
      [...terms].sort(byTerm).map((item) => [
        h(
          "dt",
          {},
          h("span", { class: "glossary-term" }, item.term),
          item.expansion ? h("span", { class: "glossary-expansion" }, item.expansion) : null
        ),
        h("dd", {}, item.definition),
      ])
    );
  }

  function apply(data) {
    checkGlossary(data);
    const next = JSON.stringify(data.terms);
    if (next !== signature) {
      signature = next;
      terms = data.terms;
      byKey = new Map(terms.map((item) => [item.term, item]));
      compiled = compileTerms(terms);
      popover.hide();
      quietly(() => {
        unmarkAll(blocksRoot);
        blocks().forEach((block) => markBlock(block, compiled));
      });
    }
    renderPanel(data.generated);
  }

  function showLoadError(err) {
    panel.hidden = false;
    panel.open = true;
    count.textContent = "could not load";
    setError(errorBox, `Could not load the glossary: ${describeError(err)}`);
  }

  async function refresh() {
    const ticket = ++loads;
    try {
      const data = await api.get(url);
      if (ticket === loads) apply(data); // an older, slower answer must not win
    } catch (err) {
      if (ticket === loads) showLoadError(err);
    }
  }

  buildButton.addEventListener("click", async () => {
    if (building) return;
    building = true;
    buildButton.disabled = true;
    buildButton.textContent = "Building...";
    buildBox.setAttribute("aria-busy", "true");
    setError(errorBox, null);
    buildStatus.textContent = "Building the glossary with Claude; this can take a minute.";
    try {
      const data = await api.post(url);
      loads += 1; // supersede any refresh still in flight
      apply(data);
      const dropped = Number(data.dropped_count) || 0;
      buildStatus.textContent =
        `Glossary built: ${plural(data.terms.length, "term")} on the part of the page you can see.` +
        (dropped
          ? ` ${plural(dropped, "proposed term")} ${dropped === 1 ? "was" : "were"} left out because ` +
            `${dropped === 1 ? "it does" : "they do"} not appear in the page text.`
          : "");
    } catch (err) {
      buildStatus.textContent = "";
      setError(errorBox, `Could not build the glossary: ${describeError(err)}`);
    } finally {
      building = false;
      buildButton.disabled = false;
      buildButton.textContent = "Build glossary";
      buildBox.removeAttribute("aria-busy");
    }
  });

  observer.observe(blocksRoot, OBSERVE);
  refresh();
  return { refresh };
}
