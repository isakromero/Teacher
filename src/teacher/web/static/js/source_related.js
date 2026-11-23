// Source page: the "Related sources" section (#related-sources-section). Loads
// GET /api/sources/{slug}/related and shows what to read first, what the source cites and is
// cited by (library sources only; the References section lists every reference), what it builds
// on, what builds on it and what is related to it, each naming the concepts behind it; or that
// the source is not analysed yet, or that nothing else relates to it through concepts. The view logic is in
// source_related_model.js.

import { api, describeError, seg } from "./api.js";
import { byId, h, setChildren } from "./dom.js";
import { relatedView } from "./source_related_model.js";

const root = byId("source-page");
const slug = root.dataset.slug;
const container = byId("related-sources");

function readFirstNode(item) {
  return h(
    "li",
    { class: "related-item" },
    h("a", { href: item.href }, item.name),
    h("span", { class: `tag related-status related-status-${item.status}` }, item.statusText),
    item.introducers.length
      ? h(
          "p",
          { class: "related-text muted small" },
          "Introduced by ",
          item.introducers.flatMap((source, i) => [
            i ? (i === item.introducers.length - 1 ? " and " : ", ") : null,
            h("a", { href: source.href }, source.title),
            source.page
              ? [" (study page: ", h("a", { href: source.page.href }, source.page.title), ")"]
              : " (no study page yet)",
          ]),
          "."
        )
      : h("p", { class: "related-text muted small" }, item.note)
  );
}

function groupNode(id, label, hint, items, extra = null) {
  const headingId = `related-${id}-heading`;
  return h(
    "section",
    { class: "concept-group related-group", "aria-labelledby": headingId },
    h("h3", { id: headingId }, label, items ? ` (${items.length})` : null),
    hint ? h("p", { class: "muted small related-hint" }, hint) : null,
    extra
  );
}

function edgeList(items) {
  return h(
    "ul",
    { class: "related-list" },
    items.map((item) =>
      h(
        "li",
        { class: "related-item" },
        h("a", { href: item.href }, item.title),
        h("p", { class: "related-text muted small" }, item.text)
      )
    )
  );
}

// "Cites" and "Cited by": always shown, with the list (when known) and what to say about it.
function citationGroups(citations) {
  const group = (id, label, hint, { items, note }) =>
    groupNode(id, label, hint, items && items.length ? items : null, [
      items && items.length
        ? h("ul", { class: "related-list" }, items.map((i) => h("li", { class: "related-item" }, h("a", { href: i.href }, i.title))))
        : null,
      note ? h("p", { class: "muted small related-note" }, note) : null,
    ]);
  return [
    group("cites", "Cites", "Library sources its references point to.", citations.cites),
    group("cited-by", "Cited by", "Library sources whose references point to it.", citations.citedBy),
  ];
}

function render(view) {
  const notAnalysed = view.notAnalysedNote ? h("p", { class: "muted small" }, view.notAnalysedNote) : null;
  const citations = citationGroups(view.citations);
  if (view.state === "not_analysed") {
    return [...citations, h("p", { class: "empty-state" }, view.message), notAnalysed];
  }
  const readFirst = groupNode(
    "read-first",
    "Read first",
    view.readFirst.length ? "Background it assumes that you are not in good standing on yet." : null,
    view.readFirst.length ? view.readFirst : null,
    view.readFirst.length
      ? h("ul", { class: "related-list" }, view.readFirst.map(readFirstNode))
      : h("p", { class: "muted" }, view.readinessNote)
  );
  const groups = view.groups.map((g) =>
    groupNode(g.key.replace(/_/g, "-"), g.label, g.hint, g.items, [
      edgeList(g.items),
      g.omitted
        ? h("p", { class: "muted small" }, `${g.omitted} more ${g.omitted === 1 ? "is" : "are"} listed above.`)
        : null,
    ])
  );
  return [
    readFirst,
    ...citations,
    ...groups,
    view.message ? h("p", { class: "empty-state related-alone" }, view.message) : null,
    notAnalysed,
  ];
}

async function loadRelated() {
  let data;
  try {
    data = await api.get(`/api/sources/${seg(slug)}/related`);
  } catch (err) {
    setChildren(container, h("p", { class: "error", role: "alert" }, `Could not load the related sources: ${describeError(err)}`));
    return;
  }
  let view;
  try {
    view = relatedView(data);
  } catch (err) {
    setChildren(container, h("p", { class: "error", role: "alert" }, `Could not show the related sources: ${err.message}`));
    return;
  }
  setChildren(container, render(view));
}

loadRelated();
