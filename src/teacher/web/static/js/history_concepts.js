// The concept part of the study history page (/history): "Suggested next" (GET
// /api/history/suggestions) and the standing on each concept (GET /api/history/concepts). A
// module of its own beside history.js, which draws the sessions. A failed load says so in its
// section; the rest of the page is unaffected. The standing table shows its first
// STANDING_SHOWN rows (weak first), the rest behind a "Show all" button, so the sessions below
// it stay within reach (on a phone each row is a block of its own, history.css).

import { api, describeError } from "./api.js";
import { byId, h, setChildren } from "./dom.js";
import { standingTable, suggestionsList } from "./standing.js";

const SHOWN = 5; // the rest behind "Show more" (all shown with ?suggestions=all, the library's link)
const STANDING_SHOWN = 12; // standing rows shown before "Show all"

const suggestionsEl = byId("history-suggestions");
const conceptsEl = byId("history-concepts");
const allToggle = byId("history-concepts-all");

let standing = null;
let standingAll = false; // the reader asked for every row of the standing table

async function loadSuggestions() {
  try {
    const data = await api.get("/api/history/suggestions");
    const all = new URLSearchParams(window.location.search).get("suggestions") === "all";
    setChildren(suggestionsEl, suggestionsList(data, { limit: SHOWN, expanded: all }));
  } catch (err) {
    setChildren(suggestionsEl, h("p", { class: "error" }, `Could not load the suggestions: ${describeError(err)}`));
  }
}

function drawStanding() {
  const parts = standingTable(standing, { all: allToggle.checked });
  setChildren(conceptsEl, parts);
  const rows = [...conceptsEl.querySelectorAll(".standing-table tbody tr")];
  if (rows.length <= STANDING_SHOWN) return;
  const extra = rows.slice(STANDING_SHOWN);
  const tbody = rows[0].parentElement;
  tbody.id = "history-standing-rows";
  const toggle = h("button", { type: "button", class: "button compact", "aria-controls": tbody.id });
  const show = () => {
    for (const row of extra) row.hidden = !standingAll;
    toggle.setAttribute("aria-expanded", String(standingAll));
    toggle.textContent = standingAll ? "Show fewer" : `Show all ${rows.length} concepts`;
  };
  toggle.addEventListener("click", () => {
    standingAll = !standingAll;
    show();
  });
  show();
  conceptsEl.append(h("div", { class: "suggestions-more small" }, toggle));
}

async function loadStanding() {
  try {
    standing = await api.get("/api/history/concepts");
  } catch (err) {
    setChildren(conceptsEl, h("p", { class: "error" }, `Could not load the standing per concept: ${describeError(err)}`));
    return;
  }
  drawStanding();
}

allToggle.addEventListener("change", () => {
  if (standing) drawStanding();
});

loadSuggestions();
loadStanding();
