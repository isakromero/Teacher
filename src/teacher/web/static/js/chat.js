// Tutor side chat on a ready study page (artifact.js) or lab (lab.js): a side bar on wide
// screens, an overlay panel opened by a floating "Tutor" button on narrow ones (the whole screen
// on a phone, sized to window.visualViewport so that an on-screen keyboard does not cover the
// composer; the floating button tucks away while the page is scrolled down, and opening the
// panel on a touch screen focuses the message list, not the composer, so that no keyboard
// covers the conversation; Enter, which sends, is labelled "send" on that keyboard). The reader
// can chat with the tutor or ask the tutor to check their understanding; on a study page they can
// also select text in a block and attach it ("Ask about this") as a quoted reference. Replies
// stream over SSE from a POST (fetch + reader).
//
// The page supplies the panel's markup (the same ids on both pages, its own empty-state text)
// and, through mountChat's options, what differs: the layout element, the element holding the
// blocks a selection can be attached from (none on a lab, whose tutor refuses references), and
// `beforeSend`, awaited before every message (the lab saves the reader's code there, since the
// tutor reads the saved workspace). If `beforeSend` throws, the message is not sent and the
// error is shown like any other failure to send.
//
// A reply is a sequence of parts: text, and blocks the tutor showed (an exercise, a chart, an
// interactable), drawn by the same renderers as the study page. Answering an exercise is graded
// and recorded by the server; the chat then tells the tutor (an "exercise_answer" turn with no
// text) so that it can respond. If that turn cannot be sent, the answer is still recorded and
// reaches the tutor with the reader's next message.
//
// API: /api/artifacts/{slug}/chat/threads[/{id}[/messages]]. The current thread id is kept per
// artifact in localStorage (created lazily on the first send; a stored id that 404s is dropped).
// Every failure is shown in the message list with its reason.
//
// On wide screens the side bar's left edge is a resize handle: drag it, or focus it and use the
// arrow keys (Home: widest, End: narrowest); a double click returns to the default width. The
// width is kept in localStorage for all pages and is limited so that the side bar is at least
// MIN_WIDTH_PX wide and the page beside it keeps MIN_PAGE_PX.

import { ApiError, api, describeError, detailText, seg } from "./api.js";
import { renderBlock } from "./blocks/index.js";
import { refreshThreadCost, renderThreadCost, replyCost } from "./cost.js";
import { byId, h, prefersReducedMotion, setChildren } from "./dom.js";
import { renderMarkdown } from "./markdown.js";
import { mountScrollHints } from "./scroll_hints.js";
import {
  MAX_MESSAGE_CHARS,
  MAX_REFERENCE_CHARS,
  addReference,
  clampReference,
  extractText,
  previewText,
} from "./references.js";
import { createSSEParser } from "./sse.js";

const WIDE_QUERY = "(min-width: 1100px)"; // keep in step with app.css
const COLLAPSED_KEY = "teacher.tutor.collapsed";
const WIDTH_KEY = "teacher.tutor.width"; // the side bar's width in px, when the reader set one
const MIN_WIDTH_PX = 320; // of the side bar
const MIN_PAGE_PX = 384; // what the page next to the side bar always keeps
const WIDTH_STEP_PX = 24; // per arrow key press on the resize handle
const threadKey = (slug) => `teacher.tutor.thread.${slug}`;
const RENDER_MS = 80; // re-render the streaming markdown at most this often
const FLASH_MS = 1800;
const PINNED_SLACK_PX = 48;
const TUCK_SLACK_PX = 8; // scrolling less than this does not move the floating button
const TUCK_AFTER_PX = 160; // the button stays out near the top of the page...
const UNTUCK_NEAR_END_PX = 120; // ...and near its end
const STALL_MS = 1500; // a streaming reply this quiet is shown as still being worked on
const STREAM_EVENTS = new Set(["user_message", "delta", "block", "done", "error"]);
const BLOCK_NAMES = {
  multiple_choice: "a question",
  blurb: "a question",
  prediction: "a prediction",
  chart: "a chart",
  interactable: "an interactive example",
};

// localStorage can be missing or throw (private windows, blocked site data): never rely on it.
const storage = {
  get(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      /* the choice just is not remembered */
    }
  },
  remove(key) {
    try {
      window.localStorage.removeItem(key);
    } catch {
      /* nothing stored */
    }
  },
};

function parseId(value) {
  return value && /^\d+$/.test(value) ? Number(value) : null;
}

function closestElement(node, selector) {
  const el = node && node.nodeType === Node.ELEMENT_NODE ? node : node && node.parentElement;
  return el ? el.closest(selector) : null;
}

// -- selection -> reference ------------------------------------------------------------------

/**
 * The current selection as a reference to one block, or null. A selection spanning several
 * blocks is attributed to the first block it covers text in and trimmed to that block.
 */
function captureSelection(blocksRoot, askButton) {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return null;
  // Text being selected inside a text field (a gate's answer box, the composer) is not study
  // material. Radio buttons can be focused while the reader selects an option's label: fine.
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

  for (const block of blocksRoot.querySelectorAll(":scope > [data-block-key]")) {
    if (!range.intersectsNode(block)) continue;
    const r = range.cloneRange();
    if (!block.contains(r.startContainer)) r.setStart(block, 0);
    if (!block.contains(r.endContainer)) r.setEnd(block, block.childNodes.length);
    if (askButton.parentNode === block && r.intersectsNode(askButton)) r.setEndBefore(askButton);
    // A boundary inside rendered maths takes the whole formula (its TeX is the useful part).
    const startMath = closestElement(r.startContainer, ".katex");
    if (startMath && block.contains(startMath)) r.setStartBefore(startMath);
    const endMath = closestElement(r.endContainer, ".katex");
    if (endMath && block.contains(endMath)) r.setEndAfter(endMath);
    if (r.collapsed) continue;

    const text = extractText(r.cloneContents());
    if (!text) continue;
    const clamped = clampReference(text);
    const rects = [...r.getClientRects()].filter((rect) => rect.width > 0 || rect.height > 0);
    const rect = rects.length ? rects[rects.length - 1] : r.getBoundingClientRect();
    return {
      block,
      rect,
      ref: { block_key: block.dataset.blockKey, text: clamped.text, truncated: clamped.truncated },
    };
  }
  return null;
}

/** The floating "Ask about this" button that follows a selection in the study material. */
function setupSelectionButton(blocksRoot, onAsk) {
  const button = h("button", { type: "button", class: "ask-button", hidden: true }, "Ask about this");
  let captured = null;
  let pointerDown = false;
  let pressing = false; // pointer is down on the button itself (touch may clear the selection first)
  let timer = 0;

  function hide() {
    captured = null;
    button.hidden = true;
  }

  function place(block, rect) {
    if (button.parentNode !== block) block.append(button);
    button.hidden = false;
    const box = block.getBoundingClientRect();
    const width = button.offsetWidth;
    let left = rect.right - box.left - width;
    left = Math.max(0, Math.min(left, box.width - width));
    button.style.left = `${Math.round(left)}px`;
    button.style.top = `${Math.round(rect.bottom - box.top + 8)}px`;
  }

  function update() {
    timer = 0;
    const found = captureSelection(blocksRoot, button);
    if (!found) {
      if (!pressing) hide();
      return;
    }
    captured = found.ref;
    place(found.block, found.rect);
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(update, ms);
  }

  // Keep the selection (and focus) where it is when the button is pressed with a mouse.
  button.addEventListener("mousedown", (event) => event.preventDefault());
  button.addEventListener("pointerdown", () => {
    pressing = true;
  });
  button.addEventListener("click", () => {
    pressing = false;
    const ref = captured;
    hide();
    if (!ref) return;
    const selection = window.getSelection();
    if (selection) selection.removeAllRanges();
    onAsk(ref);
  });

  document.addEventListener("pointerdown", (event) => {
    if (event.target === button) return;
    pressing = false;
    pointerDown = true;
    button.hidden = true; // a new selection may be starting; shown again on pointerup
  });
  const release = () => {
    if (!pointerDown) return;
    pointerDown = false;
    schedule(10);
  };
  document.addEventListener("pointerup", release);
  document.addEventListener("pointercancel", release);
  // Keyboard selection, touch handles, select-all: all end up here.
  document.addEventListener("selectionchange", () => {
    if (!pointerDown) schedule(200);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !button.hidden) hide();
  });
  window.addEventListener("resize", () => {
    if (!button.hidden) schedule(50);
  });
}

// -- the chat --------------------------------------------------------------------------------

/**
 * Mount the chat for `artifact`. Options: `layout` (the element holding the page and the panel;
 * `#artifact-layout` by default), `blocksRoot` (the blocks a selection can be attached from;
 * `#blocks` by default, null to turn "Ask about this" off), `beforeSend` (async, awaited before
 * each message is sent).
 */
export function mountChat(artifact, options = {}) {
  const slug = artifact.slug;
  const base = `/api/artifacts/${seg(slug)}/chat`;
  const assetBase = `/api/artifacts/${seg(slug)}/`;

  const layout = options.layout || byId("artifact-layout");
  const panel = byId("tutor");
  const list = byId("tutor-messages");
  const emptyState = byId("tutor-empty");
  const status = byId("tutor-status");
  const form = byId("tutor-composer");
  const input = byId("tutor-input");
  const refsEl = byId("tutor-refs");
  const hint = byId("tutor-hint");
  const sendButton = byId("tutor-send");
  const checkButton = byId("tutor-check");
  const newButton = byId("tutor-new");
  const toggleButton = byId("tutor-toggle");
  const openButton = byId("tutor-open");
  const blocksRoot = options.blocksRoot === undefined ? byId("blocks") : options.blocksRoot;
  const beforeSend = options.beforeSend || null;

  const wide = window.matchMedia(WIDE_QUERY);
  const touch = window.matchMedia("(pointer: coarse)");
  let collapsed = storage.get(COLLAPSED_KEY) === "1";
  let overlayOpen = false;
  let threadId = parseId(storage.get(threadKey(slug)));
  let refs = []; // [{block_key, text, truncated}]
  let busy = false;
  let pinned = true; // keep the list scrolled to the bottom while the reader has not scrolled up

  // -- layout --------------------------------------------------------------------------------

  const isWide = () => wide.matches;
  const isOpen = () => (isWide() ? !collapsed : overlayOpen);

  let offsetFrame = 0;
  function updateOffset() {
    offsetFrame = 0;
    // The side bar is sticky below the header: its height is the viewport minus whatever of
    // the header is still visible, so the composer is never pushed off screen.
    const top = Math.max(0, layout.getBoundingClientRect().top);
    layout.style.setProperty("--tutor-offset", `${Math.round(top)}px`);
  }
  function queueOffset() {
    if (!offsetFrame) offsetFrame = requestAnimationFrame(updateOffset);
  }

  function paintLayout() {
    const open = isOpen();
    layout.dataset.tutor = open ? "open" : "closed";
    panel.dataset.mode = isWide() ? "side" : "overlay";
    panel.hidden = !open;
    openButton.hidden = open;
    openButton.setAttribute("aria-expanded", String(open));
    toggleButton.textContent = isWide() ? "Hide" : "Close";
    toggleButton.setAttribute("aria-label", isWide() ? "Hide the tutor side bar" : "Close the tutor");
    openButton.classList.remove("is-tucked");
    fitOverlay();
    if (open) {
      updateOffset();
      autoGrow();
      stickToBottom(); // a thread loaded while the panel was closed was never scrolled
    }
  }

  // The overlay spans the visual viewport (app.css reads these properties), which shrinks and
  // moves when an on-screen keyboard opens. Without the API the stylesheet's 100dvh applies.
  const viewport = window.visualViewport || null;
  function fitOverlay() {
    if (!viewport || isWide() || panel.hidden) {
      panel.style.removeProperty("--tutor-vv-top");
      panel.style.removeProperty("--tutor-vv-height");
      return;
    }
    panel.style.setProperty("--tutor-vv-top", `${Math.round(viewport.offsetTop)}px`);
    panel.style.setProperty("--tutor-vv-height", `${Math.round(viewport.height)}px`);
    stickToBottom();
  }

  // Narrow screens: the floating button tucks away while the page scrolls down (it would sit on
  // the text being read) and comes back on scrolling up, near the top and near the end.
  let lastScrollY = window.scrollY;
  function tuckOpenButton() {
    const y = window.scrollY;
    const dy = y - lastScrollY;
    if (Math.abs(dy) < TUCK_SLACK_PX) return;
    lastScrollY = y;
    const nearEnd =
      y + window.innerHeight >= document.documentElement.scrollHeight - UNTUCK_NEAR_END_PX;
    openButton.classList.toggle("is-tucked", !isWide() && dy > 0 && y > TUCK_AFTER_PX && !nearEnd);
  }

  function setOpen(open, { focus = true } = {}) {
    if (isWide()) {
      collapsed = !open;
      storage.set(COLLAPSED_KEY, collapsed ? "1" : "0");
    } else {
      overlayOpen = open;
    }
    paintLayout();
    if (!focus) return;
    // On a touch screen focusing the composer would raise the on-screen keyboard over half of
    // the conversation the reader opened the panel to see: the message list takes focus instead.
    const target = open ? (touch.matches && !isWide() ? list : input) : openButton;
    target.focus({ preventScroll: true });
  }

  // -- small UI pieces -----------------------------------------------------------------------

  function announce(message) {
    status.textContent = message;
  }

  function setHint(message, kind = "error") {
    hint.textContent = message || "";
    hint.className = `tutor-hint is-${kind}`;
    hint.hidden = !message;
  }

  function setBusy(value) {
    busy = value;
    sendButton.disabled = value;
    checkButton.disabled = value;
    newButton.disabled = value;
    form.setAttribute("aria-busy", value ? "true" : "false");
  }

  function autoGrow() {
    if (!input.getClientRects().length) {
      input.style.height = ""; // not laid out (panel closed): sized again when it opens
      return;
    }
    input.style.height = "auto";
    const borders = input.offsetHeight - input.clientHeight;
    input.style.height = `${input.scrollHeight + borders}px`;
  }

  function stickToBottom(force = false) {
    if (force) pinned = true;
    if (pinned) list.scrollTop = list.scrollHeight;
  }

  function paintEmpty() {
    emptyState.hidden = Boolean(list.querySelector(".tutor-msg"));
  }

  function errorBox(title, detail, advice) {
    return h(
      "div",
      { class: "tutor-error", role: "alert" },
      h("p", { class: "tutor-error-title" }, title),
      h("p", { class: "tutor-error-detail" }, detail),
      advice ? h("p", { class: "tutor-error-advice" }, advice) : null
    );
  }

  // -- references in the composer ------------------------------------------------------------

  function paintRefs() {
    setChildren(
      refsEl,
      refs.map((ref, index) =>
        h(
          "li",
          { class: "tutor-ref", title: previewText(ref.text, 400) },
          h("span", { class: "tutor-ref-text" }, `“${previewText(ref.text)}”`),
          ref.truncated ? h("span", { class: "tag" }, "shortened") : null,
          h(
            "button",
            {
              type: "button",
              class: "tutor-ref-remove",
              "aria-label": `Remove the passage “${previewText(ref.text, 40)}”`,
              onclick: () => {
                refs = refs.filter((_, k) => k !== index);
                paintRefs();
                setHint(null);
                input.focus();
              },
            },
            "Remove"
          )
        )
      )
    );
    refsEl.hidden = refs.length === 0;
  }

  function attach(ref) {
    const result = addReference(refs, ref);
    if (result.error) {
      setHint(result.error);
    } else {
      refs = result.list;
      paintRefs();
      setHint(
        ref.truncated
          ? `The selection was long, so only its first ${MAX_REFERENCE_CHARS} characters are attached.`
          : null,
        "info"
      );
    }
    if (!isOpen()) setOpen(true, { focus: false });
    input.focus({ preventScroll: true });
  }

  /** Put an unsent message back, keeping anything typed or attached in the meantime. */
  function restoreComposer(text, oldRefs) {
    if (text) input.value = input.value.trim() ? `${text}\n\n${input.value}` : text;
    let merged = oldRefs;
    let dropped = 0;
    for (const ref of refs) {
      const result = addReference(merged, ref);
      if (result.error && !result.error.includes("already")) dropped += 1;
      merged = result.list;
    }
    refs = merged;
    paintRefs();
    autoGrow();
    if (dropped) {
      setHint(`${dropped} newly attached passage(s) did not fit next to the restored ones and were removed.`);
    }
  }

  // -- messages ------------------------------------------------------------------------------

  function quote(ref) {
    return h(
      "button",
      {
        type: "button",
        class: "tutor-quote",
        dataset: { blockKey: ref.block_key },
        title: "Show this passage in the study page",
      },
      h("span", { class: "visually-hidden" }, "Quoted passage, show it in the page: "),
      h("span", { class: "tutor-quote-text" }, ref.text)
    );
  }

  function userMessage(msg) {
    const references = Array.isArray(msg.references) ? msg.references : [];
    const text = typeof msg.text === "string" ? msg.text : "";
    const check = msg.kind === "check_understanding";
    const answers = Array.isArray(msg.answers) ? msg.answers : [];
    // A turn that only reports an answer: before the server confirms it, or with no text.
    const answerOnly = msg.kind === "exercise_answer" || (answers.length > 0 && !text.trim());
    let answerNote = null;
    if (answers.length) {
      const last = answers[answers.length - 1];
      answerNote = `Answered the tutor’s question: ${last.passed ? "correct" : "not yet correct"}`;
    } else if (answerOnly) {
      answerNote = "Answered the tutor’s question";
    }
    return h(
      "div",
      { class: `tutor-msg tutor-msg-user${check ? " is-check" : ""}${answerOnly ? " is-answer" : ""}` },
      h("span", { class: "visually-hidden" }, "You: "),
      references.length ? h("div", { class: "tutor-quotes" }, references.map(quote)) : null,
      answerNote ? h("p", { class: "tutor-msg-note" }, answerNote) : null,
      check
        ? h(
            "p",
            { class: "tutor-msg-note" },
            text.trim() ? "Check my understanding:" : "Asked the tutor to check my understanding"
          )
        : null,
      text.trim() ? h("div", { class: "tutor-msg-text" }, text) : null
    );
  }

  function paintMarkdown(body, text) {
    try {
      setChildren(body, renderMarkdown(text, { baseUrl: assetBase }));
    } catch (err) {
      setChildren(
        body,
        h("p", { class: "error-inline" }, `Could not display this reply as formatted text: ${err.message}`),
        h("pre", { class: "tutor-raw" }, text)
      );
    }
  }

  // The reader context the block renderers need (see reader.js), for blocks in the chat.
  const blockCtx = {
    baseUrl: assetBase,
    askConfidence: false, // the tutor's exercises do not ask how sure the reader is
    submit: (block, response) => api.post(`${base}/blocks/${seg(String(block.id))}/responses`, { response }),
    loadResponses: (block) => api.get(`${base}/blocks/${seg(String(block.id))}/responses`),
    onSubmitted() {
      // Tell the tutor. While a reply is streaming this is skipped: the answer is recorded and
      // goes with the reader's next message.
      if (!busy) send("exercise_answer");
    },
  };

  function textPart(text) {
    const el = h("div", { class: "tutor-part tutor-part-text" });
    paintMarkdown(el, text);
    return el;
  }

  function blockPart(block) {
    return h("div", { class: "tutor-part tutor-part-block" }, renderBlock(block, blockCtx));
  }

  /** A stored assistant message: its parts in order (text only, for a message without parts). */
  function assistantMessage(msg) {
    const parts = Array.isArray(msg.parts) && msg.parts.length
      ? msg.parts
      : [{ type: "text", text: String(msg.text ?? "") }];
    const body = h("div", { class: "tutor-msg-body" });
    for (const part of parts) {
      if (part && part.type === "block" && part.block) body.append(blockPart(part.block));
      else if (part && part.type === "text") body.append(textPart(String(part.text ?? "")));
      else body.append(errorBox("Could not show part of this reply", `Unknown part type: ${part && part.type}.`));
    }
    return h(
      "div",
      { class: "tutor-msg tutor-msg-assistant" },
      h("span", { class: "visually-hidden" }, "Tutor: "),
      body,
      replyCost(msg)
    );
  }

  /** A reply while it streams: text parts as they grow, and a placeholder where each block
   *  will be (the block itself arrives with the stored message). */
  function streamingMessage() {
    const body = h("div", { class: "tutor-msg-body" });
    const working = h("p", { class: "tutor-thinking tutor-working", hidden: true }, "Tutor is working…");
    const el = h(
      "div",
      { class: "tutor-msg tutor-msg-assistant is-streaming" },
      h("span", { class: "visually-hidden" }, "Tutor: "),
      body,
      working
    );
    let current = null; // the text part being written
    let text = "";
    let stall = 0;
    const quiet = () => {
      clearTimeout(stall);
      working.hidden = true;
      stall = setTimeout(() => {
        working.hidden = false;
        stickToBottom();
      }, STALL_MS);
    };
    quiet();
    return {
      el,
      addText(chunk) {
        quiet();
        if (!current) {
          current = h("div", { class: "tutor-part tutor-part-text" });
          body.append(current);
          text = "";
        }
        text += chunk;
      },
      paint() {
        if (current) paintMarkdown(current, text.trim());
      },
      addBlock(type) {
        quiet();
        this.paint();
        current = null;
        body.append(
          h(
            "p",
            { class: "tutor-part tutor-block-pending muted" },
            `The tutor added ${BLOCK_NAMES[type] || "a block"}. It appears when the reply is complete.`
          )
        );
      },
      stop() {
        clearTimeout(stall);
      },
    };
  }

  function messageElement(msg) {
    if (msg && msg.role === "user") return userMessage(msg);
    if (msg && msg.role === "assistant") return assistantMessage(msg);
    return errorBox("Could not show a message", `Unknown message role: ${msg && msg.role}.`);
  }

  // -- threads -------------------------------------------------------------------------------

  function forgetThread() {
    threadId = null;
    storage.remove(threadKey(slug));
  }

  async function ensureThread() {
    if (threadId !== null) return threadId;
    let thread;
    try {
      thread = await api.post(`${base}/threads`, {});
    } catch (err) {
      if (err instanceof ApiError) {
        throw new ApiError(`Could not start a conversation: ${err.message}`, { status: err.status });
      }
      throw err;
    }
    if (!thread || !Number.isInteger(thread.id)) {
      throw new ApiError("Unexpected response from the server: the new conversation has no id.");
    }
    threadId = thread.id;
    storage.set(threadKey(slug), String(thread.id));
    return threadId;
  }

  async function loadThread() {
    if (threadId === null) {
      paintEmpty();
      return;
    }
    setBusy(true);
    const loading = h("p", { class: "tutor-loading muted" }, "Loading the conversation…");
    list.append(loading);
    try {
      const thread = await api.get(`${base}/threads/${seg(threadId)}`);
      if (!thread || !Array.isArray(thread.messages)) {
        throw new ApiError("Unexpected response from the server: the conversation has no messages list.");
      }
      loading.remove();
      for (const msg of thread.messages) list.append(messageElement(msg));
      renderThreadCost(panel, thread);
    } catch (err) {
      loading.remove();
      if (err instanceof ApiError && err.status === 404) {
        forgetThread();
        list.append(
          h(
            "p",
            { class: "tutor-notice" },
            "Your previous conversation on this page could not be found, ",
            "so a new one will start with your next message."
          )
        );
      } else {
        list.append(
          errorBox(
            "Could not load the conversation",
            describeError(err),
            "Reload the page to try again. A message you send now continues the same conversation."
          )
        );
      }
    } finally {
      setBusy(false);
      paintEmpty();
      stickToBottom(true);
    }
  }

  // -- sending and streaming -----------------------------------------------------------------

  async function postAndStream(id, body, on) {
    const url = `${base}/threads/${seg(id)}/messages`;
    let response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
      });
    } catch (err) {
      throw new ApiError(
        `Could not reach the server (${err.message}). Check that it is still running, then try again.`
      );
    }
    if (!response.ok) {
      let text = "";
      try {
        text = await response.text();
      } catch {
        text = "";
      }
      let data = null;
      try {
        data = JSON.parse(text);
      } catch {
        data = null;
      }
      const fallback = text && !data ? text.slice(0, 300) : response.statusText || "request failed";
      throw new ApiError(detailText(data, fallback), { status: response.status });
    }
    const type = response.headers.get("content-type") || "";
    if (!type.startsWith("text/event-stream")) {
      throw new ApiError(
        `Unexpected response from the server: expected an event stream, got “${type || "no content type"}”.`,
        { status: response.status }
      );
    }
    if (!response.body) throw new ApiError("This browser cannot read a streamed response.");

    const parser = createSSEParser();
    const handle = (evt) => {
      if (!STREAM_EVENTS.has(evt.event)) return false; // unknown events are ignored
      let data;
      try {
        data = JSON.parse(evt.data);
      } catch {
        throw new ApiError(`The server sent an unreadable “${evt.event}” event.`);
      }
      switch (evt.event) {
        case "user_message":
          on.userMessage(data);
          return false;
        case "delta":
          if (!data || typeof data.text !== "string") {
            throw new ApiError("The server sent a piece of the reply without text.");
          }
          on.delta(data.text);
          return false;
        case "block":
          on.block(data && typeof data.type === "string" ? data.type : "");
          return false;
        case "done":
          on.done(data && data.message);
          return true;
        default: // "error"
          throw new ApiError(
            data && typeof data.detail === "string" && data.detail
              ? data.detail
              : "The reply failed and the server gave no reason."
          );
      }
    };

    const reader = response.body.getReader();
    try {
      for (;;) {
        let step;
        try {
          step = await reader.read();
        } catch (err) {
          throw new ApiError(`The connection broke while the tutor was replying (${err.message}).`);
        }
        if (step.done) break;
        for (const evt of parser.push(step.value)) if (handle(evt)) return;
      }
      const { events, incomplete } = parser.end();
      for (const evt of events) if (handle(evt)) return;
      throw new ApiError(
        incomplete
          ? "The connection closed in the middle of the reply."
          : "The connection closed before the tutor finished replying."
      );
    } finally {
      reader.cancel().catch(() => {});
    }
  }

  async function send(kind) {
    if (busy) return;
    // An "exercise_answer" turn has no text of its own and leaves the composer alone: the
    // reader may be in the middle of typing something else.
    const answering = kind === "exercise_answer";
    const text = answering ? "" : input.value.trim();
    if (kind === "message" && !text) {
      setHint(
        refs.length
          ? "Type your question about the attached passage, or use “Check my understanding”."
          : "Type a message first, or use “Check my understanding” to have the tutor ask you a question."
      );
      input.focus();
      return;
    }
    if (text.length > MAX_MESSAGE_CHARS) {
      setHint(`Your message is ${text.length} characters long; the limit is ${MAX_MESSAGE_CHARS}.`);
      input.focus();
      return;
    }

    const sentRefs = answering ? [] : refs;
    const body = {
      kind,
      text,
      references: sentRefs.map((r) => ({ block_key: r.block_key, text: r.text })),
    };
    if (!answering) {
      setHint(null);
      input.value = "";
      autoGrow();
      refs = [];
      paintRefs();
    }
    setBusy(true);

    let pending = userMessage(body);
    pending.classList.add("is-pending");
    list.append(pending);
    const thinking = h("p", { class: "tutor-thinking" }, "Tutor is thinking…");
    list.append(thinking);
    paintEmpty();
    stickToBottom(true);
    announce(answering ? "Answer sent to the tutor. The tutor is thinking." : "Message sent. The tutor is thinking.");

    let userStored = false;
    let usedThread = null;
    let reply = null;
    let timer = 0;
    const paintReply = () => {
      timer = 0;
      if (!reply) return;
      reply.paint();
      stickToBottom();
    };
    const ensureReply = () => {
      if (reply) return;
      thinking.remove();
      reply = streamingMessage();
      list.append(reply.el);
    };

    try {
      if (beforeSend) await beforeSend();
      usedThread = await ensureThread();
      await postAndStream(usedThread, body, {
        userMessage(msg) {
          userStored = true;
          const stored = userMessage(msg || body);
          pending.replaceWith(stored);
          pending = stored;
        },
        delta(chunk) {
          ensureReply();
          reply.addText(chunk);
          if (!timer) timer = setTimeout(paintReply, RENDER_MS);
        },
        block(type) {
          clearTimeout(timer);
          timer = 0;
          ensureReply();
          reply.addBlock(type);
          stickToBottom();
        },
        done(msg) {
          clearTimeout(timer);
          ensureReply();
          reply.stop();
          if (!msg || msg.role !== "assistant") {
            throw new ApiError("Unexpected response from the server: the finished reply has no message.");
          }
          // The stored message replaces the streamed one: it has the blocks themselves.
          const stored = assistantMessage(msg);
          reply.el.replaceWith(stored);
          reply = null;
          pending.classList.remove("is-pending");
          stickToBottom();
          announce("The tutor replied.");
        },
      });
    } catch (err) {
      clearTimeout(timer);
      thinking.remove();
      if (reply) {
        reply.stop();
        reply.el.remove();
      }
      if (!userStored) {
        pending.remove();
        if (answering) {
          list.append(
            errorBox(
              "The tutor was not told about your answer",
              describeError(err),
              "Your answer is saved and graded. The tutor will see it with your next message."
            )
          );
        } else {
          restoreComposer(text, sentRefs);
          let advice = "Your message is back in the box below.";
          if (err instanceof ApiError && err.status === 404 && usedThread !== null) {
            forgetThread();
            advice += " The conversation was not found, so sending again starts a new one.";
          }
          list.append(errorBox("Your message was not sent", describeError(err), advice));
        }
      } else {
        pending.classList.remove("is-pending");
        list.append(
          errorBox(
            "The tutor’s reply failed",
            describeError(err),
            answering
              ? "Your answer was saved. Send a message to continue."
              : "Your message was saved. Send another message to continue."
          )
        );
      }
      announce("");
      stickToBottom(true);
    } finally {
      setBusy(false);
      paintEmpty();
      if (userStored) refreshThreadCost(panel, base, usedThread); // a failed reply costs too
    }
  }

  function startNewConversation() {
    if (busy) return;
    forgetThread();
    renderThreadCost(panel, null);
    setChildren(list, emptyState);
    paintEmpty();
    setHint(null);
    pinned = true;
    announce("Started a new conversation.");
    input.focus();
  }

  // -- quotes -> blocks ----------------------------------------------------------------------

  const flashTimers = new WeakMap();
  function showBlock(key) {
    const el = key && blocksRoot ? blocksRoot.querySelector(`[data-block-key="${CSS.escape(key)}"]`) : null;
    if (!el) {
      setHint(`The section this passage came from (${key}) is not shown on this page.`);
      return;
    }
    if (!isWide() && overlayOpen) setOpen(false, { focus: false });
    const tall = el.getBoundingClientRect().height > window.innerHeight * 0.8;
    el.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: tall ? "start" : "center" });
    el.focus({ preventScroll: true });
    el.classList.remove("is-flash");
    void el.offsetWidth; // restart the animation
    el.classList.add("is-flash");
    clearTimeout(flashTimers.get(el));
    flashTimers.set(el, setTimeout(() => el.classList.remove("is-flash"), FLASH_MS));
  }

  // -- wiring --------------------------------------------------------------------------------

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    send("message");
  });
  checkButton.addEventListener("click", () => send("check_understanding"));
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (!busy) send("message");
  });
  input.addEventListener("input", () => {
    autoGrow();
    if (!hint.hidden && hint.classList.contains("is-error")) setHint(null);
  });
  input.enterKeyHint = "send"; // Enter sends (above): an on-screen keyboard says so on the key
  newButton.addEventListener("click", startNewConversation);
  toggleButton.addEventListener("click", () => setOpen(false));
  openButton.addEventListener("click", () => setOpen(true));
  panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !isWide()) {
      event.stopPropagation();
      setOpen(false);
    }
  });
  list.addEventListener(
    "scroll",
    () => {
      pinned = list.scrollHeight - list.scrollTop - list.clientHeight < PINNED_SLACK_PX;
    },
    { passive: true }
  );
  list.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest(".tutor-quote") : null;
    if (target) showBlock(target.dataset.blockKey);
  });
  // -- resizing the side bar ------------------------------------------------------------------

  // The reader's chosen width in px, or null for the stylesheet's default (--tutor-w).
  let chosenWidth = parseId(storage.get(WIDTH_KEY));
  const resizer = h("div", {
    class: "tutor-resizer",
    role: "separator",
    tabindex: "0",
    "aria-orientation": "vertical",
    "aria-label": "Width of the tutor side bar",
    title: "Drag to resize; double-click to reset",
  });
  panel.append(resizer);

  const maxWidth = () =>
    Math.max(MIN_WIDTH_PX, Math.round(layout.getBoundingClientRect().width) - MIN_PAGE_PX);
  const clampWidth = (px) => Math.min(maxWidth(), Math.max(MIN_WIDTH_PX, Math.round(px)));

  function paintWidth() {
    if (chosenWidth === null) layout.style.removeProperty("--tutor-w");
    else layout.style.setProperty("--tutor-w", `${clampWidth(chosenWidth)}px`);
    if (!isWide() || panel.hidden) return;
    resizer.setAttribute("aria-valuemin", String(MIN_WIDTH_PX));
    resizer.setAttribute("aria-valuemax", String(maxWidth()));
    resizer.setAttribute("aria-valuenow", String(Math.round(panel.getBoundingClientRect().width)));
  }
  function setWidth(px) {
    chosenWidth = px === null ? null : clampWidth(px);
    if (chosenWidth === null) storage.remove(WIDTH_KEY);
    else storage.set(WIDTH_KEY, String(chosenWidth));
    paintWidth();
  }

  resizer.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !isWide()) return;
    event.preventDefault();
    resizer.setPointerCapture(event.pointerId);
    layout.classList.add("is-resizing-tutor");
    const move = (e) => setWidth(layout.getBoundingClientRect().right - e.clientX);
    const stop = () => {
      layout.classList.remove("is-resizing-tutor");
      resizer.removeEventListener("pointermove", move);
      resizer.removeEventListener("pointerup", stop);
      resizer.removeEventListener("pointercancel", stop);
    };
    resizer.addEventListener("pointermove", move);
    resizer.addEventListener("pointerup", stop);
    resizer.addEventListener("pointercancel", stop);
  });
  resizer.addEventListener("dblclick", () => setWidth(null));
  resizer.addEventListener("keydown", (event) => {
    const now = panel.getBoundingClientRect().width;
    // The handle is the side bar's left edge: left makes the side bar wider.
    if (event.key === "ArrowLeft") setWidth(now + WIDTH_STEP_PX);
    else if (event.key === "ArrowRight") setWidth(now - WIDTH_STEP_PX);
    else if (event.key === "Home") setWidth(maxWidth());
    else if (event.key === "End") setWidth(MIN_WIDTH_PX);
    else return;
    event.preventDefault();
  });

  wide.addEventListener("change", () => {
    overlayOpen = false;
    paintLayout();
    paintWidth();
  });
  window.addEventListener("scroll", queueOffset, { passive: true });
  window.addEventListener("scroll", tuckOpenButton, { passive: true });
  if (viewport) {
    viewport.addEventListener("resize", fitOverlay);
    viewport.addEventListener("scroll", fitOverlay);
  }
  window.addEventListener("resize", () => {
    queueOffset();
    paintWidth();
  });

  if (blocksRoot) setupSelectionButton(blocksRoot, attach);
  mountScrollHints(list);

  layout.classList.add("has-tutor");
  paintRefs();
  setHint(null);
  paintLayout();
  paintWidth();
  autoGrow();
  loadThread();
}
