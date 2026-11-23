// Artifact page: one page, three states driven by GET /api/artifacts/{slug}:
// `generating` (stages, polled every 2 s), `failed` (error + Regenerate), `ready` (the reader,
// plus a Regenerate button in the header and, after the reader, the page's hands-on labs).
// The generating and failed states are shared with the lab page (generation_view.js).

import { api, describeError, seg } from "./api.js";
import { byId, setError, showPageError } from "./dom.js";
import { mountChat } from "./chat.js";
import { pollGeneration, regenerate, renderMeta, renderNotices, showFailure } from "./generation_view.js";
import { mountLabs } from "./study_labs.js";
import { mountModelChoice } from "./model_choice.js";
import { mountReader } from "./reader.js";

const root = byId("artifact-page");
const slug = root.dataset.slug;
const base = `/api/artifacts/${seg(slug)}`;
const panels = {
  generating: byId("state-generating"),
  failed: byId("state-failed"),
  ready: byId("state-ready"),
};

function showPanel(status) {
  for (const [name, el] of Object.entries(panels)) el.hidden = name !== status;
  root.dataset.status = status;
}

const NOUN = "study page";
const pageUrl = (target) => `/artifacts/${seg(target)}`;

function renderHeader(artifact) {
  byId("artifact-title").textContent = artifact.title;
  document.title = `${artifact.title} - Teacher`;
  renderMeta(byId("artifact-meta"), artifact);
  renderNotices(byId("artifact-notices"), artifact, { noun: NOUN, pageUrl });
}

// -- generating ------------------------------------------------------------------------------

async function showGenerating() {
  showPanel("generating");
  await pollGeneration(base, enterSafely);
}

// -- failed ----------------------------------------------------------------------------------

async function showFailed(artifact) {
  showPanel("failed");
  await showFailure(base, artifact, { noun: NOUN, pageUrl });
}

/** The header's "Regenerate study page" button on a ready page: asks first, since a new
 * version costs model calls. Not offered once a newer version exists (the notice links to it). */
function mountRegenerate(artifact) {
  const actions = byId("artifact-actions");
  actions.hidden = Boolean(artifact.superseded_by);
  if (artifact.superseded_by) return;
  const open = byId("regenerate-study-button");
  const panel = byId("regenerate-confirm");
  const confirmButton = byId("regenerate-study-confirm");
  const errorEl = byId("regenerate-study-error");
  const show = (visible) => {
    panel.hidden = !visible;
    open.setAttribute("aria-expanded", String(visible));
  };
  open.onclick = () => {
    show(panel.hidden);
    if (!panel.hidden) confirmButton.focus();
  };
  byId("regenerate-study-cancel").onclick = () => {
    setError(errorEl, null);
    show(false);
    open.focus();
  };
  confirmButton.onclick = () => regenerate(base, confirmButton, errorEl, "regenerate-study-model", pageUrl);
  mountModelChoice("regenerate-study-model", { selected: artifact.model, errorEl });
}

// -- ready -----------------------------------------------------------------------------------

/** Notes that stages of this page's generation left for the owner (for example questions the
 * closed-book check could still answer without the page, or figures that were dropped): a
 * ready page does not list its stages, so they are shown here, collapsed. A failure to load
 * them is shown, not swallowed. */
async function showStageNotes() {
  const runs = await api.get(`${base}/runs`);
  const noted = runs.filter((run) => run.note);
  if (!noted.length) return;
  const details = document.createElement("details");
  details.className = "notice stage-notes";
  const summary = document.createElement("summary");
  summary.textContent = `Notes from generating this page (${noted.length})`;
  const list = document.createElement("ul");
  for (const run of noted) {
    const item = document.createElement("li");
    item.textContent = `${run.stage}: ${run.note}`;
    list.append(item);
  }
  details.append(summary, list);
  byId("artifact-notices").append(details);
}

async function showReady(artifact) {
  showPanel("ready");
  mountRegenerate(artifact);
  await showStageNotes();
  await mountReader(artifact);
  mountLabs(artifact);
  mountChat(artifact);
}

// -- dispatch --------------------------------------------------------------------------------

async function enter(artifact) {
  renderHeader(artifact);
  switch (artifact.status) {
    case "generating":
      return showGenerating();
    case "failed":
      return showFailed(artifact);
    case "ready":
      return showReady(artifact);
    default:
      showPanel("none");
      setError(byId("artifact-error"), `This page cannot show artifacts with status “${artifact.status}”.`);
  }
}

async function enterSafely(artifact) {
  try {
    await enter(artifact);
  } catch (err) {
    showPageError(`Could not show this study page: ${describeError(err)}`);
  }
}

async function main() {
  let artifact;
  try {
    artifact = await api.get(base);
  } catch (err) {
    showPageError(`Could not load this study page: ${describeError(err)}`);
    return;
  }
  await enterSafely(artifact);
}

main();
