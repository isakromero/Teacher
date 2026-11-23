// The review page (/review[?concept=<slug>]): one due question at a time from the review queue,
// shown with the study page's own gate renderers (blocks/), with the "How sure are you?"
// control; after the one attempt a review gets, the verdict, the explanation or model answer
// and when the question comes back, then "Next". When nothing is served it says why (nothing
// yet, nothing due until a given time, today's reviews done, with "Keep going"). Below, the
// review bank of every page with questions in the queue: pages whose variants are missing say
// so and why, with a button to (re)build them. The DOM-free parts are in review_model.js.
//
// Options of a multiple-choice question come from the server already in the order to show;
// the answer's choice is a position in that order and the server maps it back, so this page
// passes the gate its block as it is (gate.js documents the context it gets).

import { api, describeError, seg } from "./api.js";
import { renderBlock } from "./blocks/index.js";
import { byId, h, prefersReducedMotion, setChildren, setError } from "./dom.js";
import {
  DEFAULT_CAP,
  bankRows,
  emptyState,
  extendCap,
  formLabel,
  itemNote,
  queueSummary,
  queueUrl,
  scheduleText,
} from "./review_model.js";

const root = byId("review-page");
const concept = root.dataset.concept || null;
const summaryEl = byId("review-summary");
const cardEl = byId("review-card");
const banksEl = byId("review-banks");
const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
let cap = DEFAULT_CAP;

/** Load the queue and show its first question; `moveTo` (after "Next" or "Keep going") brings
 * the new question into view and focuses it. */
async function loadQueue({ moveTo = false } = {}) {
  cardEl.setAttribute("aria-busy", "true");
  let queue;
  try {
    queue = await api.get(queueUrl({ tz, cap, limit: 1, concept }));
  } catch (err) {
    setChildren(cardEl, h("p", { class: "error", role: "alert" }, `Could not load the review queue: ${describeError(err)}`));
    cardEl.setAttribute("aria-busy", "false");
    return;
  }
  summaryEl.textContent = queueSummary(queue);
  const navCount = document.getElementById("nav-review-due"); // the header's badge (review_count.js)
  if (navCount) {
    navCount.textContent = queue.due ? String(queue.due) : "";
    navCount.hidden = !queue.due;
  }
  const empty = emptyState(queue);
  if (empty) {
    setChildren(
      cardEl,
      h(
        "div",
        { class: `review-empty review-empty-${empty.kind}` },
        h("p", {}, empty.text),
        empty.canExtend
          ? h("button", { type: "button", class: "button", onClick: () => { cap = extendCap(cap); loadQueue({ moveTo: true }); } }, `Keep going: ${Math.min(10, queue.due)} more`)
          : null
      )
    );
  } else {
    setChildren(cardEl, itemCard(queue.items[0]));
  }
  cardEl.setAttribute("aria-busy", "false");
  if (moveTo) {
    cardEl.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
    const first = cardEl.querySelector("input, textarea, button");
    if (first) first.focus({ preventScroll: true });
  }
}

function itemCard(item) {
  const note = itemNote(item);
  const result = h("div", { class: "review-result", hidden: true, "aria-live": "polite" });
  const wrap = h(
    "div",
    { class: "review-item" },
    h(
      "p",
      { class: "review-meta muted small" },
      "From ",
      h("a", { href: `/artifacts/${seg(item.artifact.slug)}` }, item.artifact.title),
      item.concepts.length ? ` · about ${item.concepts.map((c) => c.name).join(", ")}` : "",
      ` · ${formLabel(item)}`
    ),
    note ? h("p", { class: `review-note${item.confident_error ? " is-confident-error" : ""}` }, note) : null
  );
  const ctx = {
    slug: item.artifact.slug,
    baseUrl: `/api/artifacts/${seg(item.artifact.slug)}/`,
    askConfidence: true,
    submit: (block, response, meta) =>
      api.post(`/api/review/items/${item.id}/answers`, { presentation: item.presentation, response, ...meta }),
    loadResponses: () => Promise.reject(new Error("A review question has no earlier answers to load.")),
    onSubmitted(block, answer) {
      wrap.classList.add("is-answered");
      let text;
      try {
        text = scheduleText(answer);
      } catch (err) {
        text = err.message;
      }
      const next = h("button", { type: "button", class: "button primary", onClick: () => loadQueue({ moveTo: true }) }, "Next question");
      setChildren(result, h("p", { class: "review-schedule" }, text), h("div", { class: "review-actions" }, next));
      result.hidden = false;
      next.focus({ preventScroll: true });
    },
  };
  wrap.append(renderBlock(item.block, ctx), result);
  return wrap;
}

async function loadBanks() {
  let banks;
  try {
    banks = await api.get("/api/review/banks");
  } catch (err) {
    setChildren(banksEl, h("p", { class: "error", role: "alert" }, `Could not load the review banks: ${describeError(err)}`));
    return;
  }
  const rows = bankRows(banks);
  if (!rows.length) {
    setChildren(banksEl, h("p", { class: "muted" }, "No page has questions in the review queue yet."));
    return;
  }
  const attention = rows.filter((r) => r.needsAttention);
  const fine = rows.filter((r) => !r.needsAttention);
  setChildren(
    banksEl,
    attention.length ? h("ul", { class: "review-bank-list" }, attention.map(bankRow)) : h("p", { class: "muted" }, "Every page in the queue has its variants."),
    !banks.can_build ? h("p", { class: "notice notice-warn" }, banks.cannot_build_reason) : null,
    fine.length
      ? h(
          "details",
          { class: "review-bank-fine" },
          h("summary", {}, `${fine.length} ${fine.length === 1 ? "page" : "pages"} with variants`),
          h("ul", { class: "review-bank-list" }, fine.map(bankRow))
        )
      : null
  );
}

function bankRow(row) {
  const error = h("p", { class: "error", role: "alert", hidden: true });
  const button = row.canBuild ? h("button", { type: "button", class: "button compact" }, row.buttonLabel) : null;
  if (button) {
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "Building... (this takes a minute or two)";
      setError(error, null);
      try {
        await api.post(`/api/artifacts/${seg(row.slug)}/review-bank`);
      } catch (err) {
        setError(error, `The variants could not be built: ${describeError(err)}`);
        button.disabled = false;
        button.textContent = row.buttonLabel;
        return;
      }
      await loadBanks();
    });
  }
  return h(
    "li",
    { class: `review-bank review-bank-${row.status}` },
    h("p", { class: "review-bank-head" }, h("a", { href: `/artifacts/${seg(row.slug)}` }, row.title), h("span", { class: row.needsAttention ? "tag tag-warn" : "tag tag-ok" }, row.label)),
    h("p", { class: "small" }, row.text),
    button,
    error
  );
}

loadQueue();
loadBanks();
