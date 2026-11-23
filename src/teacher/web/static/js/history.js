// The study history page: a filter (one page or paper, a date range), the time spent per page,
// and the sessions, newest first, each opening onto its timeline: time on each page (and each
// block of a study page), then the pages opened, answers with their verdict and feedback, the
// tutor conversations, lab runs and the other actions, in order.
//
// API (teacher/web/routers/history.py): GET /api/history/sessions?limit&offset&artifact|source
// &since&until, GET /api/history/sessions/{id}, GET /api/history/artifacts. The filter is kept in
// the address (?artifact=, ?source=, ?from=, ?to=), so the study page can link here, and
// #session-<id> opens that session (the home page's links). Times are shown in the reader's
// time zone.

import { api, describeError, seg } from "./api.js";
import { artifactUrl } from "./artifact_list.js";
import { byId, formatDate, h, plural, setChildren, setError } from "./dom.js";
import { answerView, blockName, dayRange, formatDuration, listItems, sessionTotals, timeOnPages } from "./history_model.js";
import { renderMarkdown } from "./markdown.js";

const PAGE_SIZE = 20;

const form = byId("history-filters");
const subjectSelect = byId("history-subject");
const fromInput = byId("history-from");
const toInput = byId("history-to");
const errorEl = byId("history-error");
const totalsEl = byId("history-totals");
const countEl = byId("history-count");
const listEl = byId("history-sessions");
const moreButton = byId("history-more");

let offset = 0;
let query = "";
let loading = 0; // the latest list load; older answers are dropped

// -- links and names ---------------------------------------------------------------------------

const timeOf = (iso) =>
  new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function artifactLink(ref, gone = "a deleted page") {
  return ref ? h("a", { href: artifactUrl(ref) }, ref.title) : h("em", {}, gone);
}

function sourceLink(ref) {
  return ref ? h("a", { href: `/sources/${seg(ref.slug)}` }, ref.title) : h("em", {}, "a deleted source");
}

function conceptLink(ref) {
  return ref
    ? h("a", { href: `/graph?concept=${encodeURIComponent(ref.slug)}` }, ref.name)
    : h("em", {}, "a deleted concept");
}

/** One sentence (as DOM parts) for a study event; an unknown kind is an error, not a guess. */
function describeEvent(e) {
  const p = e.payload || {};
  switch (e.kind) {
    case "library_opened":
      return ["Opened the library."];
    case "study_page_opened":
      return ["Opened ", artifactLink(e.artifact), "."];
    case "lab_opened":
      return ["Opened the lab ", artifactLink(e.lab, "a deleted lab"), "."];
    case "source_opened":
      return ["Opened the source ", sourceLink(e.source), "."];
    case "source_file_opened":
      return ["Opened the original file of ", sourceLink(e.source), "."];
    case "graph_opened":
      return e.concept ? ["Opened the concept graph at ", conceptLink(e.concept), "."] : ["Opened the concept graph."];
    case "gate_cleared":
      return [
        `Cleared checkpoint ${blockName(e.block, e.block_label, "(deleted)")} on `,
        artifactLink(e.artifact),
        ` (${p.state === "revealed" ? "answer revealed" : "passed"} at attempt ${p.attempt}); `,
        `${plural(p.unlocked.length, "section")} unlocked.`,
      ];
    case "glossary_opened":
      return ["Opened the glossary of ", artifactLink(e.artifact), "."];
    case "glossary_term_viewed":
      return ["Looked up ", h("strong", {}, p.term), " in the glossary of ", artifactLink(e.artifact), "."];
    case "glossary_build_requested":
      return ["Asked for the glossary of ", artifactLink(e.artifact), "."];
    case "chat_thread_started":
      return [`Started tutor conversation ${p.thread_id} on `, artifactLink(e.lab || e.artifact), "."];
    case "lab_workspace_saved":
      return ["Saved the code of ", artifactLink(e.lab, "a deleted lab"), ` (${plural(p.files, "file")}).`];
    case "lab_hint_viewed":
      return [`Opened the hint of step ${p.step + 1} of `, artifactLink(e.lab, "a deleted lab"), "."];
    case "lab_solution_viewed":
      return ["Viewed the solution of ", artifactLink(e.lab, "a deleted lab"), p.first_time ? " for the first time." : " again."];
    case "lab_exported":
      return ["Downloaded ", artifactLink(e.lab, "a deleted lab"), "."];
    case "concept_inspected":
      return ["Inspected the concept ", conceptLink(e.concept), "."];
    case "graph_searched":
      return ["Searched the concept graph for ", h("strong", {}, p.query), ` (${plural(p.results, "result")}).`];
    case "relations_requested":
      return [p.regenerate ? "Asked to regenerate the relations of " : "Asked for relations of ", conceptLink(e.concept), "."];
    case "source_added":
      return ["Added the source ", sourceLink(e.source), p.via === "url" ? " from a web address." : "."];
    case "source_updated":
      return [`Edited ${p.fields.join(", ")} of `, sourceLink(e.source), "."];
    case "source_deleted":
      return ["Deleted the source ", h("em", {}, p.title), "."];
    case "study_page_requested":
      return ["Asked for a new study page, ", artifactLink(e.artifact), "."];
    case "lab_requested":
      return ["Asked for a new lab, ", artifactLink(e.lab, "a deleted lab"), ", from ", artifactLink(e.artifact), "."];
    case "regeneration_requested":
      return [`Asked to regenerate ${p.supersedes} as `, artifactLink(e.lab || e.artifact), "."];
    case "artifact_deleted":
      return [`Deleted the ${p.kind === "lab" ? "lab" : "study page"} `, h("em", {}, p.title), "."];
    default:
      throw new Error(`The history has an event this page does not know: ${e.kind}`);
  }
}

const TYPE_WORDS = { multiple_choice: "multiple choice", blurb: "written answer" };
const typeWords = (type) => TYPE_WORDS[type] || type; // a type without words here: as stored

/** What the reader answered: the chosen option (Markdown, like the options on the page), the
 * written text as typed, or a shape the history does not know, as stored and labelled so. */
function givenAnswer(answer) {
  const view = answerView(answer);
  const body =
    view.form === "choice"
      ? h("blockquote", { class: "history-response" }, renderMarkdown(view.text, { inline: true }))
      : view.form === "text"
        ? h("blockquote", { class: "history-response" }, view.text)
        : h("pre", { class: "history-response history-response-raw" }, view.text);
  return h("div", { class: "history-given" }, h("span", { class: "muted small" }, view.lead), body);
}

/** An answer's verdict: a prediction is a guess and is not graded; a written answer can be
 * partly right, in which case the criteria still missing are listed. */
function answerVerdict(e) {
  if (e.is_question === false) return h("span", { class: "tag" }, "Prediction, not graded");
  if (e.verdict === "partial") {
    const missing = (e.missing || []).join("; ");
    return h("span", { class: "tag" }, missing ? `Partly right (missing: ${missing})` : "Partly right");
  }
  return verdict(e.passed);
}

function verdict(passed, yes = "Passed", no = "Not passed") {
  return h("span", { class: passed ? "tag tag-ok" : "tag tag-fail" }, passed ? yes : no);
}

// -- one session -------------------------------------------------------------------------------

function renderItem({ entry: e, count }) {
  const time = h("time", { class: "history-time", datetime: e.at }, timeOf(e.at));
  switch (e.type) {
    case "event":
      return h("li", { class: "history-item history-event" }, time, " ", describeEvent(e), count > 1 ? h("span", { class: "muted" }, ` (${count} times)`) : null);
    case "answer":
    case "chat_answer":
      return h(
        "li",
        { class: "history-item history-answer" },
        time,
        " ",
        e.type === "answer"
          ? [`Answered checkpoint ${blockName(e.block, e.block_label)} (${typeWords(e.block_type)}) on `, artifactLink(e.artifact)]
          : [`Answered a tutor exercise (${typeWords(e.block_type)}) on `, artifactLink(e.artifact)],
        `, attempt ${e.attempt}: `,
        answerVerdict(e),
        e.question ? h("div", { class: "history-question" }, renderMarkdown(e.question)) : null,
        givenAnswer(e.answer),
        e.feedback ? h("div", { class: "history-feedback" }, h("strong", {}, "Feedback: "), renderMarkdown(e.feedback, { inline: true })) : null
      );
    case "chat_message":
      return h(
        "li",
        { class: `history-item history-chat history-chat-${e.role}` },
        time,
        " ",
        h("strong", {}, e.role === "user" ? "You" : "Tutor"),
        e.kind === "check_understanding" ? " (check my understanding)" : null,
        ` in conversation ${e.thread_id} on `,
        artifactLink(e.artifact),
        e.references.length
          ? h("ul", { class: "history-refs" }, e.references.map((r) => h("li", {}, h("span", { class: "muted" }, `${r.block_key}: `), r.text)))
          : null,
        e.text ? renderMarkdown(e.text) : h("p", { class: "muted" }, "(reported exercise answers)")
      );
    case "lab_run":
      return h(
        "li",
        { class: "history-item history-lab" },
        time,
        " ",
        e.kind === "check" ? `Checked step ${e.step + 1} of ` : e.step !== null ? `Ran step ${e.step + 1} of ` : "Ran ",
        artifactLink(e.lab, "a deleted lab"),
        ": ",
        verdict(e.passed, e.kind === "check" ? "Passed" : "Ran", e.kind === "check" ? "Failed" : "Error"),
        e.error || e.output
          ? h(
              "details",
              { class: "history-output" },
              h("summary", {}, e.error ? "Error and output" : "Output"),
              e.error ? h("pre", {}, e.error) : null,
              e.output ? h("pre", {}, e.output) : null
            )
          : null
      );
    default:
      throw new Error(`The history has an entry this page does not know: ${e.type}`);
  }
}

function renderDetail(detail) {
  const pages = timeOnPages(detail.timeline);
  const items = listItems(detail.timeline);
  return [
    pages.length
      ? h(
          "div",
          { class: "history-pages" },
          h("h3", {}, "Time on pages"),
          h(
            "ul",
            {},
            pages.map((p) =>
              h(
                "li",
                {},
                h("strong", {}, p.name),
                p.surface === "lab" ? " (lab)" : p.surface === "source" ? " (source)" : null,
                `: ${formatDuration(p.seconds)}`,
                p.blocks.length
                  ? h(
                      "ul",
                      { class: "history-blocks muted" },
                      p.blocks.map((b) => h("li", {}, `${blockName(b.block, b.label)}: ${formatDuration(b.seconds)}`))
                    )
                  : null
              )
            )
          )
        )
      : h("p", { class: "muted" }, detail.backfilled ? "No time on pages: this session was reconstructed from earlier activity." : "No time on pages was recorded in this session."),
    h("h3", {}, "Timeline"),
    items.length ? h("ol", { class: "history-timeline" }, items.map(renderItem)) : h("p", { class: "muted" }, "Nothing else happened in this session."),
  ];
}

function sessionSummary(s) {
  const start = new Date(s.started_at);
  const end = new Date(s.last_activity_at);
  const sameDay = start.toDateString() === end.toDateString();
  const span = (end - start) / 1000;
  return h(
    "summary",
    {},
    h("span", { class: "history-when" }, formatDate(s.started_at), " to ", sameDay ? timeOf(s.last_activity_at) : formatDate(s.last_activity_at)),
    " ",
    s.backfilled ? h("span", { class: "tag" }, "from earlier activity") : null,
    h(
      "span",
      { class: "history-numbers muted" },
      ` ${formatDuration(span)} long`,
      s.backfilled ? "" : `, ${formatDuration(s.active_seconds)} on pages`,
      sessionTotals(s).map((part) => `; ${part}`)
    ),
    s.artifacts.length
      ? h("span", { class: "history-touched" }, s.artifacts.map((a, i) => [i ? ", " : "", a.title]))
      : null
  );
}

function renderSession(s) {
  const body = h("div", { class: "history-session-body" }, h("p", { class: "muted" }, "Loading..."));
  const details = h("details", { class: "history-session" }, sessionSummary(s), body);
  let loaded = false;
  details.addEventListener("toggle", async () => {
    if (!details.open || loaded) return;
    loaded = true;
    try {
      const detail = await api.get(`/api/history/sessions/${seg(String(s.id))}`);
      setChildren(body, renderDetail(detail));
    } catch (err) {
      loaded = false; // try again on the next open
      setChildren(body, h("p", { class: "error" }, `Could not load this session: ${describeError(err)}`));
    }
  });
  return h("li", { id: `session-${s.id}`, class: "history-session-item" }, details);
}

/** `#session-<id>` (the home page links to a session this way): open that session and move
 * to it; say so when it is not among the sessions listed. */
function openLinkedSession() {
  const match = /^#session-(\d+)$/.exec(window.location.hash);
  if (!match || !errorEl.hidden) return; // a failed load already says what is wrong
  const item = document.getElementById(`session-${match[1]}`);
  if (!item) {
    setError(errorEl, `Session ${match[1]} is not among the sessions listed here. Clear the filter or show older sessions to find it.`);
    return;
  }
  const details = item.querySelector("details");
  details.open = true;
  item.scrollIntoView({ block: "start" });
  details.querySelector("summary").focus({ preventScroll: true });
}

// -- the lists ---------------------------------------------------------------------------------

async function loadSessions({ append = false } = {}) {
  const ticket = ++loading;
  if (!append) offset = 0;
  moreButton.disabled = true;
  let data;
  try {
    data = await api.get(`/api/history/sessions?limit=${PAGE_SIZE}&offset=${offset}${query}`);
  } catch (err) {
    if (ticket === loading) setError(errorEl, `Could not load the sessions: ${describeError(err)}`);
    moreButton.disabled = false;
    return;
  }
  if (ticket !== loading) return;
  setError(errorEl, null);
  const items = data.sessions.map(renderSession);
  if (append) listEl.append(...items);
  else setChildren(listEl, items);
  offset += data.sessions.length;
  countEl.textContent = data.total
    ? `${plural(data.total, "session")}${offset < data.total ? `, showing the newest ${offset}` : ""}.`
    : "No sessions match.";
  moreButton.hidden = offset >= data.total;
  moreButton.disabled = false;
}

async function loadTotals() {
  let rows;
  try {
    rows = await api.get("/api/history/artifacts");
  } catch (err) {
    setChildren(totalsEl, h("p", { class: "error" }, `Could not load the time per page: ${describeError(err)}`));
    return;
  }
  const studied = rows.filter((r) => r.sessions > 0).sort((a, b) => (b.last_activity_at || "").localeCompare(a.last_activity_at || ""));
  if (!studied.length) {
    setChildren(totalsEl, h("p", { class: "muted" }, "Nothing studied yet."));
    return;
  }
  setChildren(
    totalsEl,
    h(
      "table",
      { class: "history-table" },
      h("thead", {}, h("tr", {}, ["Page", "Time on page", "In its labs", "Sessions", "Answers", "Pass rate", "Last studied"].map((t) => h("th", { scope: "col" }, t)))),
      h(
        "tbody",
        {},
        studied.map((r) =>
          h(
            "tr",
            {},
            h("th", { scope: "row" }, artifactLink(r.artifact), r.artifact.kind === "lab" ? h("span", { class: "tag tag-lab" }, "Lab") : null),
            h("td", {}, formatDuration(r.active_seconds)),
            h("td", {}, r.artifact.kind === "lab" ? "-" : formatDuration(r.lab_seconds)),
            h("td", {}, String(r.sessions)),
            h("td", {}, `${r.answers_passed} of ${r.answers} passed`),
            h("td", {}, r.pass_rate === null ? "-" : `${Math.round(r.pass_rate * 100)}%`),
            h("td", {}, formatDate(r.last_activity_at))
          )
        )
      )
    )
  );
}

async function loadSubjects(selected) {
  let sources;
  let artifacts;
  try {
    [sources, artifacts] = await Promise.all([api.get("/api/sources"), api.get("/api/artifacts")]);
  } catch (err) {
    setError(errorEl, `Could not load the pages and papers to filter by: ${describeError(err)}`);
    return;
  }
  const option = (value, label) => h("option", { value, selected: value === selected }, label);
  subjectSelect.append(
    h("optgroup", { label: "Papers and other sources" }, sources.map((s) => option(`source:${s.slug}`, s.title))),
    h("optgroup", { label: "Study pages and labs" }, artifacts.map((a) => option(`artifact:${a.slug}`, a.kind === "lab" ? `${a.title} (lab)` : a.title)))
  );
  if (selected && subjectSelect.value !== selected) {
    // Keep the filter as asked: the sessions request then says what is wrong with it.
    subjectSelect.append(option(selected, `${selected} (not found)`));
  }
}

// -- the filter --------------------------------------------------------------------------------

function readAddress() {
  const params = new URLSearchParams(window.location.search);
  const subject = params.get("artifact") ? `artifact:${params.get("artifact")}` : params.get("source") ? `source:${params.get("source")}` : "";
  fromInput.value = params.get("from") || "";
  toInput.value = params.get("to") || "";
  return subject;
}

function applyFilter() {
  const params = new URLSearchParams(); // the page's address
  const apiParams = new URLSearchParams(); // the sessions request
  const [kind, slug] = subjectSelect.value ? subjectSelect.value.split(/:(.*)/s) : [null, null];
  if (kind) {
    params.set(kind, slug);
    apiParams.set(kind, slug);
  }
  if (fromInput.value) params.set("from", fromInput.value);
  if (toInput.value) params.set("to", toInput.value);
  if (fromInput.value && toInput.value && toInput.value < fromInput.value) {
    setError(errorEl, "The “To” date is before the “From” date.");
    return false;
  }
  const { since, until } = dayRange(fromInput.value, toInput.value);
  if (since) apiParams.set("since", since);
  if (until) apiParams.set("until", until);
  query = apiParams.toString() ? `&${apiParams}` : "";
  const address = params.toString() ? `?${params}` : window.location.pathname;
  window.history.replaceState(null, "", address);
  return true;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (applyFilter()) loadSessions();
});

byId("history-clear").addEventListener("click", () => {
  subjectSelect.value = "";
  fromInput.value = "";
  toInput.value = "";
  if (applyFilter()) loadSessions();
});

moreButton.addEventListener("click", () => loadSessions({ append: true }));

async function main() {
  const selected = readAddress();
  await loadSubjects(selected);
  if (applyFilter()) await loadSessions();
  openLinkedSession();
  await loadTotals();
}

window.addEventListener("hashchange", openLinkedSession);

main();
