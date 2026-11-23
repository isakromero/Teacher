// The home page's three sections: "Continue studying" (GET /api/history/continue), "Suggested
// next" (GET /api/history/suggestions, the first three as compact cards) and "Recent study"
// (GET /api/history/summary in the reader's time zone). Each section loads on its own and
// shows its own error; the search box is home_search.js.

import { api, describeError } from "./api.js";
import { byId, h, plural, setChildren } from "./dom.js";
import { continueView, sessionView, suggestionLink, summaryTiles } from "./home_model.js";
import { actionLinks, kindLabel } from "./standing_model.js";
import { statusChip } from "./standing.js";

const SUGGESTIONS = 3;
const SESSIONS = 4;

function done(el, ...children) {
  el.setAttribute("aria-busy", "false");
  setChildren(el, ...children);
}

function failed(el, what, err) {
  done(el, h("p", { class: "error", role: "alert" }, `Could not load ${what}: ${describeError(err)}`));
}

// -- continue studying -------------------------------------------------------------------------

function continueCard(item) {
  const v = continueView(item);
  const meta = [v.when, v.time].filter(Boolean).join(" · ");
  return h(
    "li",
    {},
    h(
      "a",
      { class: "card card-link continue-card", href: v.href },
      h(
        "p",
        { class: "card-eyebrow" },
        h("span", { class: `badge badge-kind kind-${item.artifact.kind === "lab" ? "lab" : "study_page"}` }, v.kind),
        v.older ? h("span", { class: "badge" }, "Older version") : null,
        v.state ? h("span", { class: v.state === "Finished" ? "badge badge-ready" : "badge badge-failed" }, v.state) : null
      ),
      h("h3", { class: "card-title" }, v.title),
      h("p", { class: "card-meta" }, meta),
      v.progress
        ? h(
            "div",
            { class: "meter" },
            h("span", { class: "meter-track", "aria-hidden": "true" }, h("span", { class: "meter-fill", style: `width: ${Math.round(v.share * 100)}%` })),
            h("span", { class: "meter-label" }, v.progress)
          )
        : null
    )
  );
}

async function loadContinue() {
  const el = byId("home-continue");
  let data;
  try {
    data = await api.get("/api/history/continue?limit=3");
  } catch (err) {
    failed(el, "what you studied last", err);
    return;
  }
  if (!data.items.length) {
    done(
      el,
      h("p", { class: "empty-state" }, "You have not studied anything yet. ", h("a", { href: "/add" }, "Add a source"), " to make your first study page.")
    );
    return;
  }
  done(el, h("ol", { class: "card-list" }, data.items.map(continueCard)));
}

// -- suggested next ----------------------------------------------------------------------------

function suggestionCard(s) {
  const link = suggestionLink(s, actionLinks);
  return h(
    "li",
    { class: `card suggestion-card suggestion-${s.kind}` },
    h(
      "p",
      { class: "card-eyebrow" },
      h("span", { class: `suggestion-kind kind-${s.kind}` }, kindLabel(s.kind)),
      statusChip(s.status)
    ),
    h("h3", { class: "card-title" }, h("a", { href: `/graph?concept=${encodeURIComponent(s.concept.slug)}` }, s.concept.name)),
    h("p", { class: "card-text" }, s.reason),
    h(
      "p",
      { class: "card-action" },
      link
        ? [h("a", { class: "action-link", href: link.href }, link.text), link.note ? h("span", { class: "muted" }, ` (${link.note})`) : null]
        : h("span", { class: "muted" }, s.action.text)
    )
  );
}

async function loadSuggestions() {
  const el = byId("home-suggestions");
  let data;
  try {
    data = await api.get(`/api/history/suggestions?limit=${SUGGESTIONS}`);
  } catch (err) {
    failed(el, "the suggestions", err);
    return;
  }
  if (!data.suggestions.length) {
    done(
      el,
      h("p", { class: "empty-state" }, "Nothing to suggest yet: suggestions come from your answers on study pages whose sections are tagged with concepts.")
    );
    return;
  }
  done(
    el,
    h("ol", { class: "card-list" }, data.suggestions.map(suggestionCard)),
    data.total > data.suggestions.length
      ? h("p", { class: "muted small section-foot" }, `${plural(data.total - data.suggestions.length, "more suggestion")} in your study history.`)
      : null
  );
}

// -- recent study -------------------------------------------------------------------------------

function timeZone() {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (!zone) throw new Error("this browser does not say which time zone it is in");
  return zone;
}

async function loadRecent() {
  const el = byId("home-recent");
  let data;
  try {
    data = await api.get(`/api/history/summary?tz=${encodeURIComponent(timeZone())}&sessions=${SESSIONS}`);
  } catch (err) {
    failed(el, "your recent study", err);
    return;
  }
  const tiles = summaryTiles(data);
  done(
    el,
    h(
      "dl",
      { class: "stats" },
      tiles.map((t) => h("div", { class: "stat" }, h("dt", { class: "stat-label" }, t.label), h("dd", { class: "stat-value" }, t.value), h("dd", { class: "stat-note" }, t.note)))
    ),
    h("h3", { class: "subsection-label" }, "Last sessions"),
    data.recent_sessions.length
      ? h(
          "ol",
          { class: "session-list" },
          data.recent_sessions.map((s) => {
            const v = sessionView(s);
            return h(
              "li",
              {},
              h(
                "a",
                { class: "session-link", href: v.href },
                h("span", { class: "session-when" }, v.when),
                h("span", { class: "session-length" }, v.length),
                h("span", { class: "session-what" }, v.what)
              )
            );
          })
        )
      : h("p", { class: "empty-state" }, "No study sessions yet."),
    data.total_sessions > data.recent_sessions.length
      ? h("p", { class: "muted small section-foot" }, `${plural(data.total_sessions, "session")} in all.`)
      : null
  );
}

loadContinue();
loadSuggestions();
loadRecent();
