// One exploration to review (#exploration-page): GET /api/explorations/{id}, polled every
// POLL_MS while it runs; then the brief, the outline by step (each concept's status, match,
// standing, existing pages and sources, its candidate sources with verdicts and reasons, and
// its errors) and what it cost. The edits go to the API and the page is drawn again from the
// reply: rename or drop a concept (PATCH .../concepts/{id}), move a step (PUT .../steps), tick
// a source (PATCH .../candidates/{id}). They are saved; nothing else happens. Enrich: the
// preview (GET .../enrichment/preview) says what will happen; only "Confirm and enrich" starts it
// (POST .../enrichments with the preview's digest); the page then polls while it runs and shows
// its progress, summary, each source's outcome and each concept's grounding. A completed
// exploration links to its roadmap; each concept has the anchor #concept-<key>, which the
// roadmap links to for a concept that needs sources. Wording and the request bodies come from
// explore_model.js.

import { api, describeError, seg } from "./api.js";
import { byId, h, setChildren, setError } from "./dom.js";
import { POLL_MS, decisionFor, detailView, moveStep, previewView } from "./explore_model.js";

const page = byId("exploration-page");
const id = Number(page.dataset.exploration);
const base = `/api/explorations/${id}`;
const meta = byId("exploration-meta");
const progressEl = byId("exploration-progress");
const errorEl = byId("exploration-error");
const failuresEl = byId("exploration-failures");
const editErrorEl = byId("exploration-edit-error");
const briefSection = byId("exploration-brief");
const briefBody = byId("exploration-brief-body");
const outlineSection = byId("exploration-outline");
const stepsEl = byId("exploration-steps");
const footer = byId("exploration-footer");
const costEl = byId("exploration-cost");
const loading = byId("exploration-loading");
const enrichSection = byId("exploration-enrich");
const enrichStatus = byId("exploration-enrich-status");
const enrichError = byId("exploration-enrich-error");
const enrichButton = byId("exploration-enrich-button");
const enrichPreview = byId("exploration-enrich-preview");
const roadmapLink = byId("exploration-roadmap-link");

let data = null;
let timer = null;
let busy = false;
let anchored = false; // scrolled to the address's #concept-<key> once

function notice(el, text) {
  el.textContent = text || "";
  el.hidden = !text;
}

async function edit(method, path, body) {
  if (busy) return;
  busy = true;
  setError(editErrorEl, null);
  try {
    render(await api[method](`${base}${path}`, body));
    closePreview(); // the plan it showed changed
  } catch (err) {
    setError(editErrorEl, `Not saved: ${describeError(err)}`);
  } finally {
    busy = false;
  }
}

function list(items) {
  return items.length ? h("ul", { class: "explore-items" }, items.map((t) => h("li", {}, t))) : h("p", { class: "muted small" }, "None.");
}

function renderBrief(brief, questions) {
  if (!brief) {
    briefSection.hidden = true;
    return;
  }
  briefSection.hidden = false;
  setChildren(
    briefBody,
    h("p", {}, brief.summary),
    h("p", { class: "small" }, h("strong", {}, "Level: "), brief.level_note),
    h("div", { class: "explore-brief-grid" },
      h("div", {}, h("h3", {}, "In scope"), list(brief.in_scope)),
      h("div", {}, h("h3", {}, "Out of scope"), list(brief.out_of_scope)),
      h("div", {}, h("h3", {}, "Taken as known"), list(brief.assumed_known))),
    questions.length
      ? h("details", { class: "explore-questions" },
          h("summary", {}, "Questions the outline answers"),
          questions.map((p) => h("div", {}, h("h3", {}, p.perspective), list(p.questions))))
      : null
  );
}

function candidateItem(view, editable) {
  const box = h("input", {
    type: "checkbox",
    checked: view.approved,
    disabled: !editable || view.ingested || view.rejected,
    onChange: (event) => edit("patch", `/candidates/${seg(view.id)}`, decisionFor(event.target.checked)),
  });
  return h(
    "li",
    { class: `discover-item explore-candidate${view.rejected ? " is-rejected" : ""}`, dataset: { candidate: String(view.id) } },
    h("div", { class: "discover-head" },
      h("label", { class: "explore-tick" }, box, h("span", { class: "visually-hidden" }, `Use ${view.title}`)),
      h("a", { class: "discover-title", href: view.url, target: "_blank", rel: "noopener noreferrer" }, view.title),
      view.score ? h("span", { class: "discover-score", title: "Overall score from triage" }, view.score) : null),
    h("span", { class: "discover-meta muted small" }, view.url),
    view.landing ? h("span", { class: "discover-meta muted small" }, view.landing) : null,
    view.details ? h("span", { class: "discover-meta muted small" }, view.details) : null,
    h("span", { class: "discover-verdict small" },
      h("span", { class: `tag tag-verdict tone-${view.verdict.tone}` }, view.verdict.label), " ", view.verdict.reason,
      view.fits ? h("span", { class: "tag tag-ok" }, "Fits") : null,
      view.followUp ? h("span", { class: "tag" }, "Follow-up search") : null),
    view.judgement ? h("p", { class: "discover-judgement small" }, view.judgement) : null,
    view.foundBy.length ? h("ul", { class: "discover-found small muted" }, view.foundBy.map((t) => h("li", {}, t))) : null,
    view.library.length
      ? h("p", { class: "discover-library small" }, "Already in your library: ",
          view.library.map((m, i) => [i ? ", " : "", h("a", { href: `/sources/${seg(m.slug)}` }, m.title)]))
      : null,
    view.alsoFor.length ? h("p", { class: "muted small" }, `Also found for: ${view.alsoFor.join(", ")}.`) : null,
    view.ingestText
      ? h("p", { class: "small" }, view.ingestText, " ",
          view.source ? h("a", { href: `/sources/${seg(view.source.slug)}` }, view.source.title) : null)
      : null,
    view.ingestError
      ? h("p", { class: "error small", role: "alert" }, `Not added: ${view.ingestError}. It stays ticked: enrich again to retry.`)
      : null,
    h("div", { class: "discover-actions" },
      view.ingested ? h("span", { class: "tag tag-ok" }, "In your library") : null,
      editable && !view.ingested
        ? h("button", {
            type: "button", class: "button quiet compact",
            onClick: () => edit("patch", `/candidates/${seg(view.id)}`, { decision: view.rejected ? "proposed" : "rejected" }),
          }, view.rejected ? "Undo reject" : "Reject")
        : null,
      view.rejected ? h("span", { class: "muted small" }, " Rejected.") : null)
  );
}

function renameForm(concept) {
  const input = h("input", { type: "text", value: concept.name, maxlength: "120", "aria-label": `New name for ${concept.name}` });
  const formEl = h("form", { class: "explore-rename", onSubmit: (event) => {
    event.preventDefault();
    edit("patch", `/concepts/${seg(concept.id)}`, { name: input.value });
  } }, input, h("button", { type: "submit", class: "button compact" }, "Save"));
  return formEl;
}

function conceptItem(concept, editable) {
  let renameSlot = h("span", {});
  const actions = editable
    ? h("div", { class: "explore-concept-actions" },
        h("button", { type: "button", class: "button quiet compact", onClick: () => {
          const replacement = renameForm(concept);
          renameSlot.replaceWith(replacement);
          renameSlot = replacement;
          replacement.querySelector("input").focus();
        } }, "Rename"),
        h("button", { type: "button", class: "button quiet compact",
          onClick: () => edit("patch", `/concepts/${seg(concept.id)}`, { dropped: !concept.dropped }) },
          concept.dropped ? "Restore" : "Drop"))
    : null;
  return h(
    "li",
    { id: `concept-${concept.key}`, class: `card explore-concept${concept.dropped ? " is-dropped" : ""}`, dataset: { concept: concept.key } },
    h("div", { class: "explore-concept-head" },
      h("h4", {}, concept.name, " ", h("span", { class: `tag tag-verdict tone-${concept.tone}` }, concept.statusLabel)),
      actions),
    renameSlot,
    concept.renamedFrom ? h("p", { class: "muted small" }, `Renamed from “${concept.renamedFrom}”; its match and sources were found for that name.`) : null,
    h("p", { class: "small" }, concept.summary),
    concept.aliases.length ? h("p", { class: "muted small" }, `Also called: ${concept.aliases.join(", ")}.`) : null,
    concept.prerequisites.length ? h("p", { class: "muted small" }, `Builds on: ${concept.prerequisites.join(", ")}.`) : null,
    concept.match ? h("p", { class: "small" }, concept.match) : null,
    concept.coverage ? h("p", { class: "muted small" }, concept.coverage) : null,
    concept.known
      ? h("div", { class: "small" },
          concept.standing ? h("p", {}, concept.standing) : null,
          h("p", {}, "In your graph: ", h("a", { href: `/graph?concept=${encodeURIComponent(concept.known.slug)}` }, concept.known.name), "."),
          concept.known.pages.length
            ? h("p", {}, "Study pages on it: ",
                concept.known.pages.map((p, i) => [i ? ", " : "", h("a", { href: `/${p.kind === "lab" ? "labs" : "artifacts"}/${seg(p.slug)}` }, p.title)]))
            : null,
          concept.known.sources.length
            ? h("p", {}, "Sources linked to it: ",
                concept.known.sources.map((s, i) => [i ? ", " : "", h("a", { href: `/sources/${seg(s.slug)}` }, s.title), ` (${s.role})`]))
            : null)
      : null,
    concept.graph ? h("p", { class: "small" }, concept.graph) : null,
    concept.grounding
      ? h("p", { class: concept.grounded ? "small" : "small notice-warn" }, concept.grounding,
          concept.groundedBy.length
            ? [" By: ", concept.groundedBy.map((s, i) => [i ? ", " : "", h("a", { href: `/sources/${seg(s.slug)}` }, s.title)]), "."]
            : null)
      : null,
    concept.alsoLinked.length
      ? h("p", { class: "muted small" }, "Also linked (does not ground it): ",
          concept.alsoLinked.map((s, i) => [i ? ", " : "", h("a", { href: `/sources/${seg(s.slug)}` }, s.title), ` (${s.role})`]), ".")
      : null,
    concept.links.length ? h("p", { class: "muted small" }, `Prerequisite links: ${concept.links.join("; ")}.`) : null,
    concept.discovery ? h("p", { class: "muted small" }, concept.discovery) : null,
    concept.error ? h("p", { class: "error small", role: "alert" }, `Source search failed: ${concept.error}`) : null,
    concept.leftOut.map((t) => h("p", { class: "muted small" }, t)),
    concept.candidates.length
      ? h("ol", { class: "discover-list" }, concept.candidates.map((c) => candidateItem(c, editable)))
      : null
  );
}

function renderSteps(view) {
  if (!view.steps.length) {
    outlineSection.hidden = true;
    return;
  }
  outlineSection.hidden = false;
  const count = view.steps.length;
  setChildren(
    stepsEl,
    view.steps.map((step, i) =>
      h("section", { class: "explore-step", dataset: { step: String(step.number) } },
        h("div", { class: "explore-step-head" },
          h("h3", {}, `Step ${step.number}: ${step.title}`),
          view.editable
            ? h("div", { class: "explore-step-actions" },
                h("button", { type: "button", class: "button quiet compact", disabled: !step.canMoveUp,
                  title: step.canMoveUp ? null : "A prerequisite must stay in an earlier step",
                  onClick: () => edit("put", "/steps", { order: moveStep(count, i, -1) }) }, "Move up"),
                h("button", { type: "button", class: "button quiet compact", disabled: !step.canMoveDown,
                  title: step.canMoveDown ? null : "A prerequisite must stay in an earlier step",
                  onClick: () => edit("put", "/steps", { order: moveStep(count, i, 1) }) }, "Move down"))
            : null),
        h("ul", { class: "explore-concepts" }, step.concepts.map((c) => conceptItem(c, view.editable)))))
  );
}

function renderEnrich(view) {
  if (view.status !== "completed" && !view.enrichment) {
    enrichSection.hidden = true;
    return;
  }
  enrichSection.hidden = false;
  const e = view.enrichment;
  enrichButton.disabled = !view.canEnrich || busy;
  enrichButton.textContent = e && e.status === "completed" ? "Enrich again..." : "Enrich...";
  if (!e) {
    setChildren(enrichStatus);
    return;
  }
  setChildren(
    enrichStatus,
    h("p", { class: e.status === "failed" ? "error" : "" }, e.progress),
    e.error ? h("p", { class: "error small", role: "alert" }, e.error) : null,
    e.failures ? h("p", { class: "notice notice-warn small", role: "alert" }, e.failures) : null,
    e.lines.length ? h("ul", { class: "explore-items small" }, e.lines.map((t) => h("li", {}, t))) : null,
    e.cost ? h("p", { class: "muted small" }, `Cost of this enrichment (from the cost ledger): ${e.cost}`) : null
  );
}

function closePreview() {
  enrichPreview.hidden = true;
  setChildren(enrichPreview);
}

async function showPreview() {
  setError(enrichError, null);
  let p;
  try {
    p = previewView(await api.get(`${base}/enrichment/preview`));
  } catch (err) {
    setError(enrichError, `The plan could not be shown: ${describeError(err)}`);
    return;
  }
  const section = (title, items) =>
    h("div", {}, h("h3", {}, title), items.length ? h("ul", { class: "explore-items small" }, items.map((t) => h("li", {}, t))) : h("p", { class: "muted small" }, "None."));
  setChildren(
    enrichPreview,
    h("p", {}, h("strong", {}, "What will happen: "), p.headline),
    p.empty ? h("p", { class: "notice notice-warn small" }, "No source is ticked: only the outline is written to your concept graph.") : null,
    section("Sources", p.sources),
    section("Concepts", p.concepts),
    section("Prerequisite links (builds on)", p.links),
    p.dropped ? h("p", { class: "muted small" }, p.dropped) : null,
    h("p", { class: "muted small" }, p.note),
    p.blocked ? h("p", { class: "error small", role: "alert" }, p.blocked) : null,
    h("p", { class: "explore-enrich-actions" },
      h("button", { type: "button", class: "button", disabled: !p.canStart, onClick: () => confirmEnrich(p.digest) }, "Confirm and enrich"),
      " ",
      h("button", { type: "button", class: "button quiet", onClick: closePreview }, "Cancel"))
  );
  enrichPreview.hidden = false;
}

async function confirmEnrich(digest) {
  if (busy) return;
  busy = true;
  setError(enrichError, null);
  try {
    const reply = await api.post(`${base}/enrichments`, { digest });
    closePreview();
    render(reply);
  } catch (err) {
    setError(enrichError, `The enrichment did not start: ${describeError(err)}`);
  } finally {
    busy = false;
  }
}

enrichButton.addEventListener("click", showPreview);

function render(reply) {
  let view;
  try {
    view = detailView(reply);
  } catch (err) {
    setError(errorEl, `The exploration could not be shown: ${err.message}`);
    return;
  }
  data = reply;
  loading.hidden = true;
  meta.textContent = `${view.level} · ${view.statusLabel}${view.approved ? ` · ${view.approved} source${view.approved === 1 ? "" : "s"} ticked` : ""}`;
  roadmapLink.hidden = view.status !== "completed";
  notice(progressEl, view.running ? view.progress : null);
  notice(errorEl, view.error ? `${view.progress} ${view.error}` : null);
  notice(failuresEl, view.failures);
  renderBrief(view.brief, view.questions);
  renderSteps(view);
  if (view.notes.length) {
    stepsEl.append(h("div", { class: "muted small" }, view.notes.map((n) => h("p", {}, n))));
  }
  renderEnrich(view);
  footer.hidden = false;
  costEl.textContent = `Cost (from the cost ledger): ${view.cost}`;
  if (view.poll) schedule();
  if (!anchored && window.location.hash) {
    anchored = true;
    const target = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
    if (target) target.scrollIntoView();
  }
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(load, POLL_MS);
}

async function load() {
  try {
    render(await api.get(base));
  } catch (err) {
    loading.hidden = true;
    setError(errorEl, `The exploration could not be loaded: ${describeError(err)}`);
    if (data && (data.status === "running" || data.enriching)) schedule();
  }
}

load();
