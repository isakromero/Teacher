// The concept part of the study history page (/history): "Suggested next" (GET
// /api/history/suggestions) and the standing on each concept (GET /api/history/concepts). A
// module of its own beside history.js, which draws the sessions. A failed load says so in its
// section; the rest of the page is unaffected.

import { api, describeError } from "./api.js";
import { byId, h, setChildren } from "./dom.js";
import { standingTable, suggestionsList } from "./standing.js";

const SHOWN = 5; // the rest behind "Show more" (all shown with ?suggestions=all, the library's link)

const suggestionsEl = byId("history-suggestions");
const conceptsEl = byId("history-concepts");
const allToggle = byId("history-concepts-all");

let standing = null;

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
  setChildren(conceptsEl, standingTable(standing, { all: allToggle.checked }));
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
