// The library page (/library): every source, newest first, with its study pages and the labs
// made from each (artifact_list.js), and a filter box over titles and authors. Adding a source
// is its own page (/add, add.js).

import { api, describeError, seg } from "./api.js";
import { artifactItems } from "./artifact_list.js";
import { byId, h, setChildren } from "./dom.js";
import { filterWords, groupBySource, groupMatches, libraryCount } from "./library_model.js";

const listEl = byId("source-list");
const countEl = byId("library-count");
const filterInput = byId("library-filter");
const filterStatus = byId("library-filter-status");

let groups = [];
let cards = []; // [{group, el}], in display order

function sourceCard(source, artifacts) {
  const details = [
    source.creators && source.creators.length ? source.creators.join(", ") : null,
    source.originated_on,
    ...(source.identifiers || []).map((i) => `${i.scheme}:${i.value}`),
  ].filter(Boolean);
  return h(
    "li",
    { class: "card source-item" },
    h("p", { class: "eyebrow" }, source.kind === "paper" ? "Paper" : source.kind === "web_page" ? "Web page" : source.kind),
    h("h3", { class: "source-title" }, h("a", { href: `/sources/${seg(source.slug)}` }, source.title)),
    details.length ? h("p", { class: "source-details muted small" }, details.join(" · ")) : null,
    artifacts.length
      ? h("ul", { class: "artifact-list", "aria-label": `Study pages and labs of ${source.title}` }, artifactItems(artifacts))
      : h(
          "p",
          { class: "muted small empty-inline" },
          "No study page yet. ",
          h("a", { href: `/sources/${seg(source.slug)}` }, "Open the source"),
          " to generate one."
        )
  );
}

function applyFilter() {
  const words = filterWords(filterInput.value);
  let shown = 0;
  for (const card of cards) {
    const keep = groupMatches(card.group, words);
    card.el.hidden = !keep;
    if (keep) shown += 1;
  }
  const empty = listEl.querySelector(".filter-empty");
  if (empty) empty.remove();
  if (!words.length) {
    filterStatus.textContent = "";
    return;
  }
  filterStatus.textContent = `Showing ${shown} of ${cards.length} ${cards.length === 1 ? "source" : "sources"}.`;
  if (!shown) {
    listEl.append(
      h("p", { class: "empty-state filter-empty" }, `No source matches “${filterInput.value.trim()}”.`)
    );
  }
}

async function loadSources() {
  let sources;
  let artifacts;
  try {
    [sources, artifacts] = await Promise.all([api.get("/api/sources"), api.get("/api/artifacts")]);
  } catch (err) {
    listEl.setAttribute("aria-busy", "false");
    setChildren(listEl, h("p", { class: "error", role: "alert" }, `Could not load the library: ${describeError(err)}`));
    return;
  }
  listEl.setAttribute("aria-busy", "false");
  if (!Array.isArray(sources) || !Array.isArray(artifacts)) {
    setChildren(listEl, h("p", { class: "error", role: "alert" }, "Unexpected response from the server: the lists of sources and study pages are missing."));
    return;
  }
  if (!sources.length) {
    countEl.textContent = "Nothing here yet.";
    setChildren(
      listEl,
      h("div", { class: "empty-state" }, h("p", {}, "No sources yet. Add a paper or a web page to start."), h("a", { class: "button primary", href: "/add" }, "Add a source"))
    );
    filterInput.disabled = true;
    return;
  }
  groups = groupBySource(sources, artifacts);
  countEl.textContent = `${libraryCount(groups)}.`;
  cards = groups.map((group) => ({ group, el: sourceCard(group.source, group.artifacts) }));
  setChildren(listEl, h("ul", { class: "sources" }, cards.map((c) => c.el)));
  applyFilter();
}

filterInput.addEventListener("input", applyFilter);
filterInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && filterInput.value) {
    event.preventDefault();
    filterInput.value = "";
    applyFilter();
  }
});

loadSources();
