// The reader's standing on concepts, drawn: the "Suggested next" list (history page, library),
// the per-concept standing table (history page) and the standing section of the graph's
// inspector. Every function returns DOM built with dom.js (no HTML strings); question text is
// rendered by markdown.js, which sanitises it. Data: GET /api/history/suggestions,
// /api/history/concepts and /api/history/concepts/{slug} (teacher/web/routers/history.py).

import { formatDate, h, plural } from "./dom.js";
import { formatDuration } from "./history_model.js";
import { renderMarkdown } from "./markdown.js";
import {
  actionLinks,
  evidenceParts,
  kindLabel,
  passedText,
  questionText,
  rateText,
  standingRows,
  statusLabel,
  thresholdsText,
} from "./standing_model.js";

export function statusChip(status) {
  return h("span", { class: `standing-chip standing-${status}` }, statusLabel(status));
}

function conceptLink(concept) {
  return h("a", { href: `/graph?concept=${encodeURIComponent(concept.slug)}` }, concept.name);
}

/** One suggestion, compact: kind, concept and status on one line, the reason, what to open
 * (inline links), and the numbers behind it folded away. */
function suggestionItem(s, hidden) {
  const links = actionLinks(s.action);
  return h(
    "li",
    { class: `suggestion suggestion-${s.kind}`, hidden },
    h(
      "div",
      { class: "suggestion-head" },
      h("span", { class: `suggestion-kind kind-${s.kind}` }, kindLabel(s.kind)),
      " ",
      h("strong", { class: "suggestion-concept" }, conceptLink(s.concept)),
      " ",
      statusChip(s.status)
    ),
    h("p", { class: "suggestion-reason" }, s.reason),
    h(
      "p",
      { class: "suggestion-action" },
      s.action.text,
      links.length
        ? [
            " ",
            links.map((l, i) => [
              i ? ", " : "",
              h("a", { href: l.href }, l.text),
              l.note ? h("span", { class: "muted" }, ` (${l.note})`) : null,
            ]),
          ]
        : null
    ),
    h(
      "details",
      { class: "suggestion-numbers" },
      h("summary", { class: "muted" }, "The numbers"),
      h("p", { class: "suggestion-evidence muted" }, evidenceParts(s).join(" · ")),
      s.also.length
        ? h("p", { class: "muted" }, "Also: ", s.also.map((a, i) => [i ? " " : "", h("em", {}, `${kindLabel(a.kind)}.`), " ", a.reason]))
        : null
    )
  );
}

let listCount = 0; // ids for the lists' show-more buttons

/**
 * The "Suggested next" list for `data` (GET /api/history/suggestions): the first `limit` shown,
 * the rest behind a "Show N more" button (`expanded`: shown from the start), and, when
 * `moreHref` is given, a link to the full list elsewhere. Suggestions the response left out
 * (`total` beyond those given) are counted, and reachable only through `moreHref`.
 */
export function suggestionsList(data, { limit = Infinity, moreHref = null, expanded = false } = {}) {
  if (!data.suggestions.length) {
    return h(
      "p",
      { class: "muted" },
      "Nothing to suggest yet: suggestions come from your answers to questions on study pages whose blocks are tagged with concepts, and from the concept graph."
    );
  }
  const listId = `suggestions-${++listCount}`;
  const items = data.suggestions.map((s, i) => suggestionItem(s, i >= limit && !expanded));
  const extra = items.slice(limit);
  const notGiven = data.total - data.suggestions.length;
  const toggle = extra.length
    ? h("button", {
        type: "button",
        class: "button compact",
        "aria-controls": listId,
        "aria-expanded": String(expanded),
      })
    : null;
  if (toggle) {
    const label = () => {
      const open = toggle.getAttribute("aria-expanded") === "true";
      toggle.textContent = open ? "Show fewer" : `Show ${plural(extra.length, "more suggestion")}`;
    };
    label();
    toggle.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(open));
      for (const item of extra) item.hidden = !open;
      label();
    });
  }
  return [
    h("ol", { class: "suggestions", id: listId }, items),
    toggle || notGiven > 0 || moreHref
      ? h(
          "div",
          { class: "suggestions-more small" },
          toggle,
          notGiven > 0 ? h("span", { class: "muted" }, `${plural(notGiven, "more suggestion")} not listed here.`) : null,
          moreHref ? h("a", { href: moreHref }, "The full list in your study history") : null
        )
      : null,
    h(
      "p",
      { class: "muted small" },
      `Fill the gap: prerequisites of concepts you struggled with that are not yet in good standing. Review: weak concepts, and good ones not studied for ${data.rules.review_after_days} days. Ready next: concepts you have not studied whose prerequisites are in good standing.`
    ),
  ];
}

/** The per-concept standing table (GET /api/history/concepts), weak first. */
export function standingTable(data, { all = false } = {}) {
  const rows = standingRows(data.concepts, { all });
  if (!rows.length) {
    return h(
      "p",
      { class: "muted" },
      "No study-page block is tagged with a concept yet, so there is no standing to show. New study pages are tagged when they are made; `teacher concepts tag-blocks --all` tags the existing ones."
    );
  }
  // On phones the history page shows each row as a card, each cell under its column's name
  // (`data-label`, history.css); the explicit roles keep it a table for screen readers when its
  // display changes.
  const cell = (label, value) => h("td", { role: "cell", "data-label": label }, value);
  return [
    h("p", { class: "muted small" }, thresholdsText(data.thresholds)),
    h(
      "table",
      { class: "history-table standing-table", role: "table" },
      h(
        "thead",
        { role: "rowgroup" },
        h("tr", { role: "row" }, ["Concept", "Standing", "Answered", "First time", "Attempts", "Time on it", "Last studied"].map((t) => h("th", { scope: "col", role: "columnheader" }, t)))
      ),
      h(
        "tbody",
        { role: "rowgroup" },
        rows.map((c) =>
          h(
            "tr",
            { role: "row" },
            h("th", { scope: "row", role: "rowheader" }, conceptLink(c.concept)),
            cell("Standing", statusChip(c.status)),
            cell("Answered", `${c.questions_answered} of ${c.questions}`),
            cell("First time", c.questions_answered ? `${c.first_attempt_passed} (${rateText(c.first_attempt_pass_rate)})` : "-"),
            cell("Attempts", String(c.attempts)),
            cell("Time on it", formatDuration(c.active_seconds)),
            cell("Last studied", c.last_studied_at ? formatDate(c.last_studied_at) : "-")
          )
        )
      )
    ),
  ];
}

/** The graph inspector's standing section for one concept (GET /api/history/concepts/{slug}). */
export function standingSection(d) {
  const main = d.tested_by.filter((q) => q.role === "main");
  const other = d.tested_by.filter((q) => q.role !== "main");
  const question = (q) =>
    h(
      "li",
      {},
      h("div", { class: "relation-head" }, h("a", { href: `/artifacts/${encodeURIComponent(q.artifact.slug)}` }, q.artifact.title), q.visible ? null : h("span", { class: "badge" }, "Locked")),
      h("p", { class: "small" }, questionText(q)),
      q.visible && q.question ? h("div", { class: "standing-question small" }, renderMarkdown(q.question)) : null,
      q.rationale ? h("p", { class: "rationale" }, q.rationale) : null
    );
  return h(
    "section",
    { class: "relation-group standing-section", "aria-label": "Your standing" },
    h("h3", {}, "Your standing ", statusChip(d.status)),
    h(
      "p",
      { class: "small" },
      passedText(d),
      d.attempts ? `; ${plural(d.attempts, "attempt")} in all` : "",
      d.active_seconds ? `; ${formatDuration(d.active_seconds)} on blocks about it` : "",
      d.last_studied_at ? `; last studied ${formatDate(d.last_studied_at)}` : "",
      "."
    ),
    h("h4", {}, `Questions that test it (${main.length})`),
    main.length
      ? h("ul", { class: "relation-list" }, main.map(question))
      : h("p", { class: "muted small" }, "No question in the library tests it yet (or its pages are not tagged)."),
    other.length ? [h("h4", {}, `Questions that mention it (${other.length})`), h("ul", { class: "relation-list" }, other.map(question))] : null,
    h("p", { class: "muted small" }, thresholdsText(d.thresholds))
  );
}
