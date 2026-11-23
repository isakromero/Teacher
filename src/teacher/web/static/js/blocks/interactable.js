// `interactable` block: { title, instructions (Markdown), html }. `html` is a widget fragment
// written by the model (markup + <style> + <script>); it runs only inside a sandboxed iframe
// (see interactable_shell.js) and is never inserted into this page's DOM.
//
// The frame reports its content height and its runtime errors by postMessage; the host accepts
// a message only when event.source is this block's frame and the data passes parseFrameMessage.
// A frame that never reports a height has failed to run at all, and says so (fail loudly).

import { h } from "../dom.js";
import { buildInteractableDocument, parseFrameMessage } from "../interactable_shell.js";
import { renderMarkdown } from "../markdown.js";

const INITIAL_HEIGHT = 240;
// After the frame's load event the shell has already posted its height; allow for slow delivery.
const AFTER_LOAD_MS = 3000;
// The load event itself never comes when the widget's script hangs the frame.
const AFTER_CREATE_MS = 8000;

function requireString(content, field) {
  const value = content ? content[field] : undefined;
  if (typeof value !== "string") throw new Error(`interactable content.${field} is not a string`);
  return value;
}

export function renderInteractable(block, ctx) {
  const title = requireString(block.content, "title");
  const instructions = requireString(block.content, "instructions");
  const srcdoc = buildInteractableDocument(requireString(block.content, "html"));
  const titleId = `interactable-${block.key}-title`;

  const notice = h("div", { class: "notice notice-error interactable-error", role: "alert", hidden: true });
  const reset = h("button", { type: "button", class: "button compact" }, "Reset");
  const frameBox = h("div", { class: "interactable-frame" });
  const instructionsEl = instructions.trim() ? renderMarkdown(instructions, { baseUrl: ctx.baseUrl }) : null;
  if (instructionsEl) instructionsEl.classList.add("interactable-instructions");

  const figure = h(
    "figure",
    { class: "interactable", "aria-labelledby": titleId },
    h(
      "figcaption",
      { class: "interactable-header" },
      h("p", { class: "interactable-label" }, "Try it"),
      h("h3", { class: "interactable-title", id: titleId }, title)
    ),
    instructionsEl,
    frameBox,
    notice,
    h("div", { class: "interactable-actions" }, reset)
  );

  let frame = null;
  let height = INITIAL_HEIGHT;
  let gotHeight = false;
  let noticeKind = null; // null | "error" | "silent"
  let timers = [];
  let wasConnected = false;

  function showNotice(kind, text) {
    noticeKind = kind;
    notice.textContent = text;
    notice.hidden = false;
  }

  function clearNotice() {
    noticeKind = null;
    notice.textContent = "";
    notice.hidden = true;
  }

  function clearTimers() {
    timers.forEach(clearTimeout);
    timers = [];
  }

  /** Stop listening once the block has been removed from the page. */
  function detachedForGood() {
    if (figure.isConnected) {
      wasConnected = true;
      return false;
    }
    if (!wasConnected) return false;
    window.removeEventListener("message", onMessage);
    clearTimers();
    return true;
  }

  function checkStarted(current) {
    if (current !== frame || gotHeight || detachedForGood()) return;
    if (!frame.isConnected) return; // not on the page (yet): the load-event check covers it later
    if (noticeKind === "error") return; // the widget's own error says more than this would
    showNotice(
      "silent",
      "This interactive did not start: its frame never reported its size, so its code probably " +
        "failed to run. Try Reset; if it still does not start, the widget is broken."
    );
  }

  function createFrame() {
    clearTimers();
    gotHeight = false;
    const current = h("iframe", {
      class: "interactable-iframe",
      sandbox: "allow-scripts",
      title,
      referrerpolicy: "no-referrer",
    });
    current.style.height = `${height}px`;
    current.srcdoc = srcdoc;
    current.addEventListener("load", () => {
      timers.push(setTimeout(() => checkStarted(current), AFTER_LOAD_MS));
    });
    timers.push(setTimeout(() => checkStarted(current), AFTER_CREATE_MS));
    return current;
  }

  function onMessage(event) {
    if (detachedForGood()) return;
    const win = frame && frame.contentWindow;
    if (!win || event.source !== win) return;
    const message = parseFrameMessage(event.data);
    if (!message) return;
    if (message.type === "height") {
      gotHeight = true;
      if (noticeKind === "silent") clearNotice();
      height = message.height;
      frame.style.height = `${height}px`;
    } else if (message.type === "error" && noticeKind !== "error") {
      showNotice("error", `This interactive reported an error: ${message.message}`);
    }
  }

  reset.addEventListener("click", () => {
    clearNotice();
    const next = createFrame();
    if (frame) frame.replaceWith(next);
    else frameBox.append(next);
    frame = next;
  });

  frame = createFrame();
  frameBox.append(frame);
  window.addEventListener("message", onMessage);
  return figure;
}
