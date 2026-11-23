// Loaded on every page (base.html): on the pages whose time is measured it sends the study
// history's heartbeat (activity.js), saying what is on screen: a study page with the block
// being read, a lab, a source page, the concept graph. Other pages (the library, the history,
// a 404) send nothing. On a study page a heartbeat also goes out once the block in view has
// changed and the scrolling has settled (SETTLE_MS), so time is split between blocks where the
// reader moved on; and on every page one goes out when the tab is hidden or the page closed, so
// the last seconds of a visit count. On a study page it also shows the time spent on it so far
// (#artifact-study-time, from GET /api/history/artifacts/{slug}).

import { api, describeError, request, seg } from "./api.js";
import { HEARTBEAT_MS, SETTLE_MS, createHeartbeat } from "./activity.js";
import { byId, h, setChildren, showPageError } from "./dom.js";
import { blockInView, formatDuration } from "./history_model.js";

const ACTIVITY_EVENTS = ["scroll", "wheel", "keydown", "pointerdown", "pointermove", "touchstart"];

function studyPageBlock() {
  const ready = document.getElementById("state-ready");
  if (!ready || ready.hidden) return null;
  const blocks = [...document.querySelectorAll("#blocks > [data-block-key]")].map((el) => {
    const rect = el.getBoundingClientRect();
    return { key: el.dataset.blockKey, top: rect.top, bottom: rect.bottom };
  });
  return blockInView(blocks, window.innerHeight);
}

/** What the heartbeat reports on this page, or null for a page that reports nothing. */
function subjectFor(root) {
  switch (root.dataset.page) {
    case "artifact":
      return () => ({ surface: "study_page", slug: root.dataset.slug, block: studyPageBlock() });
    case "lab":
      return () => ({ surface: "lab", slug: root.dataset.slug });
    case "source":
      return () => ({ surface: "source", slug: root.dataset.slug });
    case "graph":
      return () => ({ surface: "graph" });
    default:
      return null;
  }
}

async function showStudyTime(slug) {
  const el = byId("artifact-study-time");
  let totals;
  try {
    totals = await api.get(`/api/history/artifacts/${seg(slug)}`);
  } catch (err) {
    showPageError(`Could not load your study time on this page: ${describeError(err)}`);
    return;
  }
  const link = h("a", { href: `/history?artifact=${encodeURIComponent(slug)}` }, "Your history on this page");
  if (!totals.sessions) {
    setChildren(el, "No study time recorded on this page yet.");
  } else {
    const parts = [
      `You have spent ${formatDuration(totals.active_seconds)} on this page`,
      totals.lab_seconds ? ` and ${formatDuration(totals.lab_seconds)} in its labs` : "",
      `, over ${totals.sessions} ${totals.sessions === 1 ? "session" : "sessions"}`,
    ];
    if (totals.answers) {
      parts.push(`; ${totals.answers_passed} of ${totals.answers} ${totals.answers === 1 ? "answer" : "answers"} passed`);
    }
    setChildren(el, `${parts.join("")}. `, link);
  }
  el.hidden = false;
}

// The last heartbeat, when the tab is hidden or the page closed: `keepalive` lets the request
// outlive the page. A failure goes to onError (the banner) like any other while the page is
// still there (a hidden tab); once the page is gone nothing can show it. That is accepted for
// this one call only: at worst the last few seconds of the visit are not credited.
const sendFinal = (body) => request("POST", "/api/history/heartbeat", { json: body, keepalive: true });

function main() {
  const root = document.querySelector("[data-page]");
  if (!root) return;
  const subject = subjectFor(root);
  if (!subject) return;
  const heartbeat = createHeartbeat({
    subject,
    send: (body) => api.post("/api/history/heartbeat", body),
    sendFinal,
    now: () => Date.now(),
    visible: document.visibilityState === "visible",
    onError: (err) =>
      showPageError(`Time on this page is no longer being recorded in your study history: ${describeError(err)}`),
  });
  const onActivity = () => heartbeat.activity();
  for (const name of ACTIVITY_EVENTS) {
    window.addEventListener(name, onActivity, { capture: true, passive: true });
  }
  document.addEventListener("visibilitychange", () => {
    heartbeat.visibility(document.visibilityState === "visible");
  });
  window.addEventListener("pagehide", () => heartbeat.leave());
  window.addEventListener("pageshow", (event) => {
    // back from the back/forward cache: the page is shown again without a visibilitychange
    if (event.persisted) heartbeat.visibility(document.visibilityState === "visible");
  });
  if (root.dataset.page === "artifact") {
    // The block in view may change on scrolling, resizing, or blocks unlocking below a gate;
    // a heartbeat goes out once it has stayed changed for SETTLE_MS (none while scrolling on).
    let settle = null;
    const onMove = () => {
      clearTimeout(settle);
      settle = setTimeout(() => heartbeat.settled(), SETTLE_MS);
    };
    window.addEventListener("scroll", onMove, { capture: true, passive: true });
    window.addEventListener("resize", onMove, { passive: true });
    new MutationObserver(onMove).observe(byId("blocks"), { childList: true });
  }
  const timer = setInterval(() => {
    if (heartbeat.stopped) clearInterval(timer);
    else heartbeat.tick();
  }, HEARTBEAT_MS);
  heartbeat.start();
  if (root.dataset.page === "artifact") showStudyTime(root.dataset.slug);
}

main();
