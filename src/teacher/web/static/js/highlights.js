// Highlights and notes on a ready study page (artifact.js): select text in a block and choose
// Highlight, Add note or Ask the tutor from a small popover; highlights are drawn as <mark>s
// whenever the page is shown; tapping one offers Edit note and Remove; the "Highlights and
// notes" panel above the blocks lists them in page order, and each entry jumps to its place.
//
// API: GET/POST /api/artifacts/{slug}/highlights, PATCH/DELETE .../highlights/{id}
// (teacher/web/routers/highlights.py). Every failure is shown where it happened (in the
// popover or the panel), never dropped.
//
// Anchoring (highlight_model.js): a highlight is its block's key and its passage's text with a
// little context and offsets in the block's text model, so it is found again after any
// re-render. Rendered maths is one unit: a selection that starts or ends inside a formula takes
// the whole formula, a formula is marked by a class on its KaTeX element (never by changing the
// KaTeX markup), and its text is its TeX source. Code is text like any other. What is not
// text, or not the reader's to mark, is left out of the model and cannot be highlighted:
// charts and drawings (SVG, canvas), interactive examples (iframes), images, form controls and
// buttons, hidden parts, the "From the source" note, and anything marked `data-no-highlight`;
// a selection made only of those gets a message saying so instead of the actions. A highlight
// whose passage is no longer in its block (or whose block is not on the page) is listed in the
// panel as "Not found on the page", never dropped.
//
// Drawing: a highlighted run of text is wrapped in <mark class="hl" data-hl-ids="<id> ...">
// (several ids where highlights overlap; `has-note` when one of them has a note); a formula
// gets class `hl-atom` and the same attribute. A MutationObserver draws a block again when its
// text changes (a gate repaints on submit) or its marks are gone, and draws blocks as they
// unlock. Glossary marks (glossary.js) may sit inside or around highlight marks; neither
// changes the text, so neither disturbs the other.
//
// Touch and iOS (the app shows this page in a WKWebView): the popover follows the selection
// through `selectionchange` (no hover, no right-click, no long-press handler of its own), sits
// below the selection so that the system's own selection menu above it stays usable, and the
// selection is captured when it settles, so a tap on the popover that clears the selection
// still acts on it. Hooks for a host page or app shell:
//   window.teacherHighlights = { slug, list(), refresh(), hidePopover(), popoverOpen() }
//   document events (CustomEvent): "teacher:highlights" {detail: {artifact, count, missing}}
//     after every load or change; "teacher:highlight-popover" {detail: {open, mode}} when the
//     popover opens, changes mode ("selection" | "note" | "existing" | "message") or closes.
//   The popover is #hl-popover (data-mode), the panel #highlights.

import { api, describeError, seg } from "./api.js";
import { byId, h, prefersReducedMotion, setChildren, setError } from "./dom.js";
import {
  MAX_NOTE_CHARS,
  MAX_QUOTE_CHARS,
  buildModel,
  checkHighlight,
  checkList,
  coveredPieces,
  inPageOrder,
  locate,
  makeAnchor,
  modelToRaw,
  noteValue,
  pickHighlight,
  rawToModel,
  widenToAtoms,
} from "./highlight_model.js";
import { clampReference, extractText, katexSource, previewText } from "./references.js";

const MARK_CLASS = "hl";
const ATOM_CLASS = "hl-atom";
const NOTE_CLASS = "has-note";
const IDS_ATTR = "data-hl-ids";
const FLASH_CLASS = "is-flash";
const FLASH_MS = 1800;
const SETTLE_MS = 250; // a selection this still is the reader's choice (touch handles move it)
const GAP_PX = 10;
const MARGIN_PX = 8;
const NARROW_QUERY = "(max-width: 600px)";

const SKIP_TAGS = new Set([
  "BUTTON", "INPUT", "TEXTAREA", "SELECT", "OPTION", "SCRIPT", "STYLE", "TEMPLATE", "NOSCRIPT",
  "SVG", "CANVAS", "IFRAME", "DIALOG", "IMG", "VIDEO", "AUDIO", "OBJECT", "EMBED",
]);
const SKIP_CLASSES = ["block-sources", "ask-button"];
const BLOCK_TAGS = new Set([
  "P", "DIV", "LI", "UL", "OL", "H1", "H2", "H3", "H4", "H5", "H6", "PRE", "BLOCKQUOTE", "TABLE",
  "THEAD", "TBODY", "TFOOT", "TR", "TD", "TH", "CAPTION", "FIGURE", "FIGCAPTION", "SECTION",
  "ARTICLE", "HEADER", "FOOTER", "ASIDE", "FIELDSET", "LEGEND", "LABEL", "DL", "DT", "DD",
  "DETAILS", "SUMMARY", "HR", "BR", "FORM",
]);
// Containers in which a text node may only be whitespace: such a run is never wrapped.
const NO_TEXT_PARENTS = new Set(["UL", "OL", "TABLE", "THEAD", "TBODY", "TFOOT", "TR", "DL"]);
const UNSUPPORTED =
  "This part of the page cannot be highlighted: highlights work on text, not on charts, " +
  "interactive examples, images or form fields.";

// -- the text model of a block ---------------------------------------------------------------

function tagOf(el) {
  return String(el.tagName || "").toUpperCase();
}

function skipped(el) {
  if (SKIP_TAGS.has(tagOf(el))) return true;
  if (el.hidden || el.hasAttribute("data-no-highlight")) return true;
  return SKIP_CLASSES.some((name) => el.classList.contains(name));
}

function atomText(el) {
  const source = katexSource(el);
  if (source) return source.display ? `$$${source.tex}$$` : `$${source.tex}$`;
  return `$${el.textContent}$`; // maths without its TeX annotation: still one unit
}

/** The pieces of a block's text (highlight_model.js), each text and atom with its `ref`. */
function piecesOf(block) {
  const pieces = [];
  const gap = () => pieces.push({ kind: "gap", text: " " });
  const walk = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        if (child.nodeValue) pieces.push({ kind: "text", text: child.nodeValue, ref: child });
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE || skipped(child)) continue;
      if (child.classList.contains("katex")) {
        pieces.push({ kind: "atom", text: atomText(child), ref: child });
        continue;
      }
      const isBlock = BLOCK_TAGS.has(tagOf(child)) || child.classList.contains("katex-display");
      if (isBlock) gap();
      walk(child);
      if (isBlock) gap();
    }
  };
  walk(block);
  return pieces;
}

function modelOf(block) {
  return buildModel(piecesOf(block));
}

function closestElement(node, selector) {
  const el = node && node.nodeType === Node.ELEMENT_NODE ? node : node && node.parentElement;
  return el ? el.closest(selector) : null;
}

/** Where a range ends on screen: its last line box (the popover goes below it). */
function rangeRect(range) {
  const rects = [...range.getClientRects()].filter((rect) => rect.width > 0 || rect.height > 0);
  return rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();
}

/** Where the DOM point (container, offset) falls in the model's raw text. */
function rawOffset(model, container, offset, isEnd) {
  const { pieces, starts } = model;
  for (let i = 0; i < pieces.length; i += 1) {
    const piece = pieces[i];
    if (piece.kind === "atom" && piece.ref.contains(container)) {
      return isEnd ? starts[i] + piece.text.length : starts[i];
    }
    if (piece.kind === "text" && piece.ref === container) {
      return starts[i] + Math.min(offset, piece.text.length);
    }
  }
  const point = document.createRange();
  point.setStart(container, offset);
  point.collapse(true);
  for (let i = 0; i < pieces.length; i += 1) {
    const piece = pieces[i];
    if (!piece.ref) continue;
    const node = piece.kind === "text" ? piece.ref : piece.ref.parentNode;
    const at = piece.kind === "text" ? 0 : Array.prototype.indexOf.call(node.childNodes, piece.ref);
    if (point.comparePoint(node, at) >= 0) return starts[i];
  }
  return model.raw.length;
}

// -- the module ------------------------------------------------------------------------------

/**
 * Mount the highlights of `artifact` (a ready study page). Options: `blocksRoot` (the element
 * whose direct children are the blocks; `#blocks` by default) and `askTutor(ref)` (attaches a
 * passage `{block_key, text, truncated}` to the tutor's composer and opens it; it must not
 * send anything by itself).
 */
export function mountHighlights(artifact, options = {}) {
  const slug = artifact.slug;
  const base = `/api/artifacts/${seg(slug)}/highlights`;
  const blocksRoot = options.blocksRoot || byId("blocks");
  const askTutor = options.askTutor || null;

  const panel = byId("highlights");
  const countEl = byId("highlights-count");
  const listEl = byId("highlights-list");
  const emptyEl = byId("highlights-empty");
  const errorEl = byId("highlights-error");
  const status = byId("highlights-status");

  const items = new Map(); // id -> highlight (as the API sends it)
  const located = new Map(); // id -> whether it is drawn on the page now
  const drawnText = new WeakMap(); // block -> the model text it was drawn on
  let loaded = false;

  const blocks = () =>
    [...blocksRoot.children].filter((el) => el.dataset && el.dataset.blockKey);
  const blockFor = (key) => blocks().find((el) => el.dataset.blockKey === key) || null;
  const marksOf = (id, root = blocksRoot) =>
    [...root.querySelectorAll(`[${IDS_ATTR}]`)].filter((el) =>
      el.getAttribute(IDS_ATTR).split(" ").includes(id)
    );

  function emit() {
    const missing = [...items.keys()].filter((id) => !located.get(id)).length;
    document.dispatchEvent(
      new CustomEvent("teacher:highlights", { detail: { artifact: slug, count: items.size, missing } })
    );
  }

  // -- drawing -------------------------------------------------------------------------------

  function unwrap(block) {
    const parents = new Set();
    for (const mark of [...block.querySelectorAll(`mark.${MARK_CLASS}`)]) {
      parents.add(mark.parentNode);
      mark.replaceWith(...mark.childNodes);
    }
    for (const parent of parents) parent.normalize();
    for (const atom of [...block.querySelectorAll(`.${ATOM_CLASS}`)]) {
      atom.classList.remove(ATOM_CLASS, NOTE_CLASS);
      atom.removeAttribute(IDS_ATTR);
    }
  }

  const hasNote = (ids) => ids.some((id) => items.get(id) && items.get(id).note);

  function draw(block) {
    unwrap(block);
    const key = block.dataset.blockKey;
    const model = modelOf(block);
    drawnText.set(block, model.text);
    const spans = new Map(); // piece index -> [{from, to, id}]
    for (const item of items.values()) {
      if (item.block_key !== key) continue;
      const at = locate(model.text, item);
      located.set(item.id, Boolean(at));
      if (!at) continue;
      const raw = modelToRaw(model, at.start, at.end);
      for (const part of coveredPieces(model, raw.start, raw.end)) {
        if (!spans.has(part.index)) spans.set(part.index, []);
        spans.get(part.index).push({ from: part.from, to: part.to, id: item.id });
      }
    }
    for (const [index, parts] of spans) {
      const piece = model.pieces[index];
      if (piece.kind === "atom") {
        const ids = [...new Set(parts.map((p) => p.id))];
        piece.ref.classList.add(ATOM_CLASS);
        piece.ref.classList.toggle(NOTE_CLASS, hasNote(ids));
        piece.ref.setAttribute(IDS_ATTR, ids.join(" "));
        continue;
      }
      const node = piece.ref;
      const text = node.nodeValue;
      const cuts = [...new Set([0, text.length, ...parts.flatMap((p) => [p.from, p.to])])].sort(
        (a, b) => a - b
      );
      const noText = NO_TEXT_PARENTS.has(tagOf(node.parentNode));
      const frag = document.createDocumentFragment();
      for (let i = 0; i + 1 < cuts.length; i += 1) {
        const [a, b] = [cuts[i], cuts[i + 1]];
        const part = text.slice(a, b);
        const ids = [...new Set(parts.filter((p) => p.from <= a && p.to >= b).map((p) => p.id))];
        if (!ids.length || (noText && !part.trim())) {
          frag.append(part);
          continue;
        }
        frag.append(
          h("mark", { class: `${MARK_CLASS}${hasNote(ids) ? ` ${NOTE_CLASS}` : ""}`, [IDS_ATTR]: ids.join(" ") }, part)
        );
      }
      node.replaceWith(frag);
    }
  }

  /** Whether `block` still shows what it was drawn with: the same text, every mark there. */
  function upToDate(block) {
    if (!drawnText.has(block) || modelOf(block).text !== drawnText.get(block)) return false;
    const key = block.dataset.blockKey;
    for (const item of items.values()) {
      if (item.block_key === key && located.get(item.id) && !marksOf(item.id, block).length) return false;
    }
    return true;
  }

  const OBSERVE = { childList: true, subtree: true, characterData: true };
  const pending = new Set();
  let frame = 0;
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      const target =
        record.target.nodeType === Node.ELEMENT_NODE ? record.target : record.target.parentElement;
      if (target === blocksRoot) {
        for (const node of record.addedNodes) {
          if (node.nodeType === Node.ELEMENT_NODE && node.dataset.blockKey) pending.add(node);
        }
      } else if (target) {
        const block = target.closest("[data-block-key]");
        if (block && block.parentElement === blocksRoot) pending.add(block);
      }
    }
    if (pending.size && !frame) frame = requestAnimationFrame(flush);
  });

  function quietly(change) {
    observer.disconnect();
    try {
      change();
    } finally {
      observer.observe(blocksRoot, OBSERVE);
    }
  }

  function flush() {
    frame = 0;
    const todo = [...pending].filter((block) => blocksRoot.contains(block));
    pending.clear();
    const stale = todo.filter((block) => !upToDate(block));
    if (!stale.length) return;
    quietly(() => stale.forEach(draw));
    paintList();
  }

  function drawAll() {
    for (const item of items.values()) located.set(item.id, false);
    quietly(() => blocks().forEach(draw));
    paintList();
  }

  function redrawBlockOf(key) {
    const block = blockFor(key);
    if (block) quietly(() => draw(block));
  }

  // -- the panel -----------------------------------------------------------------------------

  function announce(message) {
    status.textContent = message;
  }

  function sectionName(item) {
    return Number.isInteger(item.block_position) ? `section ${item.block_position}` : `section ${item.block_key}`;
  }

  function entry(item) {
    const found = Boolean(located.get(item.id));
    const li = h("li", { class: `hl-entry${found ? "" : " is-missing"}`, dataset: { highlightId: item.id } });
    const quote = h("blockquote", { class: "hl-entry-quote", title: previewText(item.quote, 400) }, `“${previewText(item.quote, 160)}”`);
    const where = h(
      "p",
      { class: "hl-entry-where muted small" },
      found ? `In ${sectionName(item)}` : null,
      found ? null : h("span", { class: "tag tag-warn" }, "Not found on the page"),
      found
        ? null
        : ` ${blockFor(item.block_key) ? "The text of its section has changed since it was highlighted." : `Its section (${item.block_key}) is not shown on this page.`}`
    );
    const note = item.note ? h("p", { class: "hl-entry-note" }, item.note) : null;
    const actions = h("div", { class: "hl-entry-actions" });
    if (found) {
      actions.append(
        h("button", { type: "button", class: "button compact", onclick: () => show(item.id) }, "Show in page")
      );
    }
    actions.append(
      h(
        "button",
        { type: "button", class: "button compact", onclick: () => editInList(li, item) },
        item.note ? "Edit note" : "Add note"
      ),
      h("button", { type: "button", class: "button compact danger quiet", onclick: () => confirmInList(li, item) }, "Remove")
    );
    const error = h("div", { class: "error hl-entry-error", role: "alert", hidden: true });
    return setChildren(li, quote, where, note, actions, error);
  }

  let listSignature = "";
  /** Draw the panel's list again, when what it shows changed (or `force`: an editor in it is
   * closed), so that a note being edited there is not lost when a block is redrawn. */
  function paintList({ force = false } = {}) {
    if (!loaded) return;
    const list = inPageOrder([...items.values()]);
    const signature = JSON.stringify(
      list.map((item) => [item.id, item.note, Boolean(located.get(item.id)), Boolean(blockFor(item.block_key))])
    );
    if (!force && signature === listSignature) return;
    listSignature = signature;
    const missing = list.filter((item) => !located.get(item.id)).length;
    panel.hidden = false;
    countEl.textContent = list.length
      ? `${list.length}${missing ? ` (${missing} not found)` : ""}`
      : "none yet";
    emptyEl.hidden = list.length > 0;
    listEl.hidden = list.length === 0;
    setChildren(listEl, list.map(entry));
    emit();
  }

  function editInList(li, item) {
    const errorBox = li.querySelector(".hl-entry-error");
    const editor = noteEditor(item.note, {
      onSave: async (text, button) => {
        button.disabled = true;
        try {
          await saveNote(item.id, text);
          announce("Note saved.");
        } catch (err) {
          setError(errorBox, `Could not save the note: ${describeError(err)}`);
          button.disabled = false;
        }
      },
      onCancel: () => paintList({ force: true }),
    });
    li.querySelector(".hl-entry-actions").replaceWith(editor.el);
    const oldNote = li.querySelector(".hl-entry-note");
    if (oldNote) oldNote.remove();
    editor.focus();
  }

  function confirmInList(li, item) {
    const errorBox = li.querySelector(".hl-entry-error");
    const doRemove = async (button) => {
      button.disabled = true;
      try {
        await remove(item.id);
        announce("Highlight removed.");
      } catch (err) {
        setError(errorBox, `Could not remove the highlight: ${describeError(err)}`);
        button.disabled = false;
      }
    };
    if (!item.note) {
      doRemove(li.querySelector(".hl-entry-actions .danger"));
      return;
    }
    const confirmBox = h(
      "div",
      { class: "hl-entry-actions hl-confirm", role: "group", "aria-label": "Remove this highlight" },
      h("span", {}, "Remove this highlight and its note?"),
      h("button", { type: "button", class: "button compact danger", onclick: (e) => doRemove(e.currentTarget) }, "Remove"),
      h("button", { type: "button", class: "button compact", onclick: () => paintList({ force: true }) }, "Cancel")
    );
    li.querySelector(".hl-entry-actions").replaceWith(confirmBox);
    confirmBox.querySelector("button").focus();
  }

  const flashTimers = new WeakMap();
  function show(id) {
    const marks = marksOf(id);
    if (!marks.length) {
      setError(errorEl, "This highlight is not on the page any more.");
      return;
    }
    const first = marks[0];
    first.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "center" });
    first.setAttribute("tabindex", "-1");
    first.focus({ preventScroll: true });
    for (const mark of marks) {
      mark.classList.remove(FLASH_CLASS);
      void mark.offsetWidth; // restart the animation
      mark.classList.add(FLASH_CLASS);
      clearTimeout(flashTimers.get(mark));
      flashTimers.set(mark, setTimeout(() => mark.classList.remove(FLASH_CLASS), FLASH_MS));
    }
  }

  // -- talking to the server -----------------------------------------------------------------

  function keep(item) {
    const checked = checkHighlight(item);
    if (checked.artifact !== slug) {
      throw new Error(`Unexpected response from the server: a highlight of “${checked.artifact}”.`);
    }
    items.set(checked.id, checked);
    return checked;
  }

  async function create(anchor, note) {
    const made = keep(await api.post(base, { ...anchor, note }));
    redrawBlockOf(made.block_key);
    paintList();
    announce(made.note ? "Highlight with a note saved." : "Highlight saved.");
    return made;
  }

  async function saveNote(id, text) {
    const saved = keep(await api.patch(`${base}/${seg(id)}`, { note: noteValue(text) }));
    redrawBlockOf(saved.block_key); // the note mark style
    paintList();
    return saved;
  }

  async function remove(id) {
    const item = items.get(id);
    await api.delete(`${base}/${seg(id)}`);
    items.delete(id);
    located.delete(id);
    if (item) redrawBlockOf(item.block_key);
    paintList();
  }

  async function load() {
    try {
      const list = checkList(await api.get(base), slug);
      items.clear();
      list.forEach(keep);
      loaded = true;
      listSignature = "";
      setError(errorEl, null);
      drawAll();
    } catch (err) {
      loaded = false;
      panel.hidden = false;
      panel.open = true;
      countEl.textContent = "could not load";
      setError(errorEl, `Could not load your highlights: ${describeError(err)}`);
    }
  }

  // -- the popover ---------------------------------------------------------------------------

  const popover = h("div", {
    id: "hl-popover",
    class: "hl-popover",
    role: "dialog",
    "aria-label": "Highlight",
    hidden: true,
  });
  document.body.append(popover);
  const narrow = window.matchMedia(NARROW_QUERY);
  let mode = "";
  let placeAt = null; // () => DOMRect of what the popover belongs to
  let pressing = false; // a pointer is down on the popover (touch may clear the selection first)
  let pointerDown = false;
  let timer = 0;

  function setMode(next) {
    const changed = next !== mode;
    mode = next;
    popover.dataset.mode = next;
    popover.hidden = !next;
    if (changed) {
      document.dispatchEvent(
        new CustomEvent("teacher:highlight-popover", { detail: { open: Boolean(next), mode: next || null } })
      );
    }
  }

  function hide() {
    clearTimeout(timer);
    placeAt = null;
    pressing = false;
    setChildren(popover);
    setMode("");
  }

  function place() {
    if (popover.hidden || !placeAt) return;
    const vv = window.visualViewport;
    const viewTop = vv ? vv.offsetTop : 0;
    const viewLeft = vv ? vv.offsetLeft : 0;
    const viewWidth = vv ? vv.width : window.innerWidth;
    const viewHeight = vv ? vv.height : window.innerHeight;
    const box = popover.getBoundingClientRect();
    if (mode === "note" && narrow.matches) {
      // a phone's keyboard covers the lower half: the editor goes to the top of what is seen
      popover.style.left = `${Math.round(viewLeft + MARGIN_PX)}px`;
      popover.style.top = `${Math.round(viewTop + MARGIN_PX)}px`;
      return;
    }
    const rect = placeAt();
    let top = rect.bottom + GAP_PX;
    if (top + box.height > viewTop + viewHeight - MARGIN_PX && rect.top - GAP_PX - box.height >= viewTop + MARGIN_PX) {
      top = rect.top - GAP_PX - box.height;
    }
    top = Math.max(viewTop + MARGIN_PX, Math.min(top, viewTop + viewHeight - box.height - MARGIN_PX));
    let left = rect.left + rect.width / 2 - box.width / 2;
    left = Math.max(viewLeft + MARGIN_PX, Math.min(left, viewLeft + viewWidth - box.width - MARGIN_PX));
    popover.style.left = `${Math.round(left)}px`;
    popover.style.top = `${Math.round(top)}px`;
  }

  function open(next, children, rectOf) {
    setChildren(popover, children);
    placeAt = rectOf;
    setMode(next);
    place();
  }

  function popoverError(message) {
    let box = popover.querySelector(".hl-popover-error");
    if (!box) {
      box = h("p", { class: "hl-popover-error error-inline", role: "alert" });
      popover.append(box);
    }
    box.textContent = message;
    place();
  }

  function actionButton(label, onClick, extra = "") {
    const button = h("button", { type: "button", class: `button compact ${extra}`.trim() }, label);
    button.addEventListener("click", () => onClick(button));
    return button;
  }

  function noteEditor(initial, { onSave, onCancel }) {
    const id = `hl-note-${Math.random().toString(36).slice(2)}`;
    const area = h("textarea", { id, class: "hl-note-input", rows: "3", maxlength: String(MAX_NOTE_CHARS) });
    area.value = initial || "";
    const save = actionButton("Save note", (button) => onSave(area.value, button), "primary");
    const cancel = actionButton("Cancel", () => onCancel());
    area.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onCancel();
      } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        save.click();
      }
    });
    const el = h(
      "div",
      { class: "hl-note-editor" },
      h("label", { for: id, class: "hl-note-label" }, "Note"),
      area,
      h("div", { class: "hl-popover-actions" }, save, cancel)
    );
    return { el, focus: () => area.focus({ preventScroll: true }) };
  }

  /** The current selection inside the blocks, as {block, anchor, ref, range, message}; null
   * when there is none to act on. */
  function captureSelection() {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
    const active = document.activeElement;
    if (
      active &&
      (active.tagName === "TEXTAREA" ||
        (active.tagName === "INPUT" && typeof active.selectionStart === "number") ||
        active.isContentEditable)
    ) {
      return null;
    }
    const range = selection.getRangeAt(0);
    if (!range.intersectsNode(blocksRoot)) return null;
    const touched = blocks().filter((block) => range.intersectsNode(block));
    for (const [n, block] of touched.entries()) {
      const r = range.cloneRange();
      if (!block.contains(r.startContainer)) r.setStart(block, 0);
      if (!block.contains(r.endContainer)) r.setEnd(block, block.childNodes.length);
      const model = modelOf(block);
      const raw = widenToAtoms(
        model,
        rawOffset(model, r.startContainer, r.startOffset, false),
        rawOffset(model, r.endContainer, r.endOffset, true)
      );
      const anchor =
        raw.end > raw.start
          ? makeAnchor(model, rawToModel(model, raw.start), rawToModel(model, raw.end))
          : null;
      // The tutor gets the passage as the chat's "Ask about this" took it (maths as TeX).
      const startMath = closestElement(r.startContainer, ".katex");
      if (startMath && block.contains(startMath)) r.setStartBefore(startMath);
      const endMath = closestElement(r.endContainer, ".katex");
      if (endMath && block.contains(endMath)) r.setEndAfter(endMath);
      const text = r.collapsed ? "" : extractText(r.cloneContents());
      if (!anchor && !text) continue;
      let message = null;
      if (!anchor) message = UNSUPPORTED;
      else if (anchor.quote.length > MAX_QUOTE_CHARS) {
        message = `That selection is too long to highlight (it is ${anchor.quote.length} characters; at most ${MAX_QUOTE_CHARS}).`;
      } else if (touched.length > n + 1) {
        message = "The selection runs over several sections: only the part in the first one is used.";
      }
      return {
        block,
        anchor: anchor && anchor.quote.length <= MAX_QUOTE_CHARS ? { block_key: block.dataset.blockKey, ...anchor } : null,
        ref: text ? { block_key: block.dataset.blockKey, ...clampReference(text) } : null,
        range: r,
        message,
      };
    }
    return null;
  }

  function clearSelection() {
    const selection = window.getSelection();
    if (selection) selection.removeAllRanges();
  }

  function selectionPopover(found) {
    const rectOf = () => rangeRect(found.range);
    const buttons = [];
    if (found.anchor) {
      buttons.push(
        actionButton("Highlight", async (button) => {
          button.disabled = true;
          const anchor = found.anchor;
          try {
            await create(anchor, null);
            clearSelection();
            hide();
          } catch (err) {
            button.disabled = false;
            popoverError(`Could not save the highlight: ${describeError(err)}`);
          }
        }, "primary"),
        actionButton("Add note", () => {
          const anchor = found.anchor;
          clearSelection();
          const editor = noteEditor("", {
            onSave: async (text, button) => {
              button.disabled = true;
              try {
                await create(anchor, noteValue(text));
                hide();
              } catch (err) {
                button.disabled = false;
                popoverError(`Could not save the highlight: ${describeError(err)}`);
              }
            },
            onCancel: hide,
          });
          open("note", [h("p", { class: "hl-popover-quote" }, `“${previewText(anchor.quote, 120)}”`), editor.el], rectOf);
          editor.focus();
        })
      );
    }
    if (found.ref && askTutor) {
      buttons.push(
        actionButton("Ask the tutor", () => {
          const ref = found.ref;
          clearSelection();
          hide();
          askTutor(ref);
        })
      );
    }
    if (!buttons.length && !found.message) return;
    open(
      buttons.length ? "selection" : "message",
      [
        found.message ? h("p", { class: "hl-popover-message" }, found.message) : null,
        buttons.length ? h("div", { class: "hl-popover-actions" }, buttons) : null,
      ],
      rectOf
    );
  }

  function existingPopover(item, element) {
    const rectOf = () => element.getBoundingClientRect();
    const edit = () => {
      const editor = noteEditor(item.note, {
        onSave: async (text, button) => {
          button.disabled = true;
          try {
            await saveNote(item.id, text);
            announce("Note saved.");
            hide();
          } catch (err) {
            button.disabled = false;
            popoverError(`Could not save the note: ${describeError(err)}`);
          }
        },
        onCancel: hide,
      });
      open("note", [editor.el], rectOf);
      editor.focus();
    };
    const doRemove = async (button) => {
      button.disabled = true;
      try {
        await remove(item.id);
        announce("Highlight removed.");
        hide();
      } catch (err) {
        button.disabled = false;
        popoverError(`Could not remove the highlight: ${describeError(err)}`);
      }
    };
    const removeButton = actionButton("Remove", (button) => {
      if (!item.note) {
        doRemove(button);
        return;
      }
      open(
        "existing",
        [
          h("p", { class: "hl-popover-message" }, "Remove this highlight and its note?"),
          h(
            "div",
            { class: "hl-popover-actions" },
            actionButton("Remove", doRemove, "danger"),
            actionButton("Cancel", () => existingPopover(item, element))
          ),
        ],
        rectOf
      );
    }, "danger quiet");
    open(
      "existing",
      [
        item.note ? h("p", { class: "hl-popover-note" }, item.note) : null,
        h(
          "div",
          { class: "hl-popover-actions" },
          actionButton(item.note ? "Edit note" : "Add note", edit),
          removeButton,
          actionButton("Close", hide, "quiet")
        ),
      ],
      rectOf
    );
    popover.querySelector("button").focus({ preventScroll: true });
  }

  function update() {
    timer = 0;
    if (mode === "note") return; // an open note editor is not replaced by a selection
    const found = captureSelection();
    if (!found) {
      if (!pressing && (mode === "selection" || mode === "message")) hide();
      return;
    }
    selectionPopover(found);
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(update, ms);
  }

  popover.addEventListener("mousedown", (event) => {
    // keep the selection (and focus) where it is when a button is pressed with a mouse
    if (event.target instanceof Element && event.target.closest("button")) event.preventDefault();
  });
  popover.addEventListener("pointerdown", () => {
    pressing = true;
  });
  popover.addEventListener("click", () => {
    pressing = false;
  });
  document.addEventListener("pointerdown", (event) => {
    if (event.target instanceof Node && popover.contains(event.target)) return;
    pressing = false;
    pointerDown = true;
    if (mode === "existing") hide(); // a tap elsewhere closes a highlight's menu
    if (mode === "selection" || mode === "message") {
      popover.hidden = true; // a new selection may be starting; shown again when it settles
    }
  });
  const release = () => {
    if (!pointerDown) return;
    pointerDown = false;
    if (mode === "selection" || mode === "message") popover.hidden = false;
    schedule(10);
  };
  document.addEventListener("pointerup", release);
  document.addEventListener("pointercancel", release);
  document.addEventListener("selectionchange", () => {
    if (!pointerDown) schedule(SETTLE_MS);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && mode) hide();
  });
  window.addEventListener("resize", () => place());
  window.addEventListener("scroll", () => place(), { passive: true });
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", () => place());
    window.visualViewport.addEventListener("scroll", () => place());
  }

  // A tap on a highlight (with nothing selected) opens its menu; links, form controls, option
  // labels and glossary terms keep their own behaviour.
  blocksRoot.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    if (target.closest("a, button, input, textarea, select, label, .gloss-term")) return;
    const mark = target.closest(`[${IDS_ATTR}]`);
    if (!mark || !blocksRoot.contains(mark)) return;
    const item = pickHighlight(mark.getAttribute(IDS_ATTR).split(" "), items);
    if (!item) return;
    existingPopover(item, mark);
  });

  observer.observe(blocksRoot, OBSERVE);
  window.teacherHighlights = {
    slug,
    list: () => inPageOrder([...items.values()]).map((item) => ({ ...item, found: Boolean(located.get(item.id)) })),
    refresh: load,
    hidePopover: hide,
    popoverOpen: () => Boolean(mode),
  };
  load();
  return window.teacherHighlights;
}
