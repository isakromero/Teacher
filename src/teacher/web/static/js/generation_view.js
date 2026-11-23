// The parts of a generated page that the study page (artifact.js) and the lab page (lab.js)
// share: the header's meta line and version notices, the list of generation stages, polling
// while the artifact is `generating`, the failed panel and Regenerate.
//
// Both pages name their elements the same way (`#generating-stages`, `#generating-error`,
// `#failed-summary`, `#failed-stages`, `#regenerate-button`, `#regenerate-error`,
// `#regenerate-model`); what differs is passed in: `noun` ("study page", "lab") for the
// wording and `pageUrl(slug)` for where an artifact is opened.

import { api, describeError, seg } from "./api.js";
import { byId, formatDate, h, setChildren, setError, statusBadge } from "./dom.js";
import { chosenModel, mountModelChoice } from "./model_choice.js";

export const POLL_MS = 2000;

/** The header's meta line: status, the sources, when it was made and with which model. */
export function renderMeta(el, artifact) {
  const sources = artifact.sources || [];
  // replaceChildren stringifies arrays and null, so build one flat list of nodes and strings.
  const metaParts = [statusBadge(artifact.status), " "];
  if (sources.length) {
    metaParts.push("Based on ");
    sources.forEach((s, i) => {
      if (i) metaParts.push(", ");
      metaParts.push(h("a", { href: `/sources/${seg(s.slug)}` }, s.title));
    });
    metaParts.push(". ");
  }
  metaParts.push(`Created ${formatDate(artifact.created_at)}.`);
  if (artifact.model) metaParts.push(" Model: ", h("code", {}, artifact.model), ".");
  setChildren(el, ...metaParts);
}

/** Links to a newer version of this artifact, and to the one it replaces. */
export function renderNotices(el, artifact, { noun, pageUrl }) {
  el.replaceChildren();
  if (artifact.superseded_by) {
    el.append(
      h(
        "p",
        { class: "notice" },
        `A newer version of this ${noun} exists: `,
        h("a", { href: pageUrl(artifact.superseded_by) }, "open it"),
        "."
      )
    );
  }
  if (artifact.supersedes) {
    el.append(
      h(
        "p",
        { class: "muted small" },
        "Replaces ",
        h("a", { href: pageUrl(artifact.supersedes) }, "an earlier version"),
        "."
      )
    );
  }
}

function duration(run) {
  if (!run.finished_at) return null;
  const ms = new Date(run.finished_at) - new Date(run.started_at);
  if (!Number.isFinite(ms) || ms < 0) return null;
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function renderStages(listEl, runs) {
  listEl.replaceChildren();
  if (!runs.length) {
    listEl.append(h("li", { class: "stage muted" }, "Waiting for the first stage to start..."));
    return;
  }
  for (const run of runs) {
    const time = duration(run);
    listEl.append(
      h(
        "li",
        { class: `stage stage-${run.status}` },
        h("span", { class: "stage-dot", "aria-hidden": "true" }),
        h("span", { class: "stage-name" }, run.stage),
        statusBadge(run.status),
        time ? h("span", { class: "stage-time muted" }, time) : null,
        // A stage that succeeded may carry a note the owner should see (for example questions
        // that could still be answered without the page after the last revision).
        run.note ? h("span", { class: "stage-note muted small" }, `Note: ${run.note}`) : null
      )
    );
  }
}

/**
 * Poll `base` (GET /api/artifacts/{slug}) and its runs every POLL_MS, drawing the stages in
 * `#generating-stages`, until the artifact is no longer `generating`; then `onDone(artifact)`.
 * A failed check is shown in `#generating-error` and retried.
 */
export async function pollGeneration(base, onDone) {
  const listEl = byId("generating-stages");
  const errorEl = byId("generating-error");
  let failures = 0;

  const tick = async () => {
    let artifact;
    let runs;
    try {
      [artifact, runs] = await Promise.all([api.get(base), api.get(`${base}/runs`)]);
    } catch (err) {
      failures += 1;
      setError(
        errorEl,
        `Could not check progress: ${describeError(err)}. Retrying every ${POLL_MS / 1000} s` +
          (failures > 1 ? ` (${failures} failed checks in a row).` : ".")
      );
      setTimeout(tick, POLL_MS);
      return;
    }
    setError(errorEl, null);
    failures = 0;
    renderStages(listEl, runs);
    if (artifact.status === "generating") {
      setTimeout(tick, POLL_MS);
      return;
    }
    await onDone(artifact);
  };
  await tick();
}

/**
 * Fill the failed panel: the stage that failed and its error (`#failed-summary`), every stage
 * (`#failed-stages`), and Regenerate with the model picker (disabled once a newer version
 * exists).
 */
export async function showFailure(base, artifact, { noun, pageUrl }) {
  const summary = byId("failed-summary");
  let runs = [];
  let loadError = null;
  try {
    runs = await api.get(`${base}/runs`);
  } catch (err) {
    loadError = err;
  }
  renderStages(byId("failed-stages"), runs);
  const failed = [...runs].reverse().find((r) => r.status === "failed");
  if (loadError) {
    setChildren(summary,
      h("p", { class: "error", role: "alert" }, `Could not load the stages: ${describeError(loadError)}`)
    );
  } else if (failed) {
    setChildren(summary,
      h("p", {}, "Failed during the ", h("strong", {}, failed.stage), " stage:"),
      h("pre", { class: "error-text" }, failed.error || "No error message was recorded.")
    );
  } else {
    setChildren(summary,
      h(
        "p",
        {},
        "Generation stopped before it finished, without a recorded error ",
        "(for example because the server was restarted while it was running)."
      )
    );
  }

  const button = byId("regenerate-button");
  const errorEl = byId("regenerate-error");
  button.disabled = Boolean(artifact.superseded_by);
  if (artifact.superseded_by) {
    setError(errorEl, null);
    summary.append(
      h("p", { class: "muted" }, `This ${noun} has already been regenerated; see the newer version above.`)
    );
  }
  button.onclick = () => regenerate(base, button, errorEl, "regenerate-model", pageUrl);
  if (!artifact.superseded_by) {
    await mountModelChoice("regenerate-model", { selected: artifact.model, errorEl });
  }
}

/** Start a new version of the artifact at `base`, with the model chosen in `#<modelId>`, and
 * go to it; on failure, say why and re-enable. */
export async function regenerate(base, button, errorEl, modelId, pageUrl) {
  const label = button.textContent;
  setError(errorEl, null);
  button.disabled = true;
  button.textContent = "Starting...";
  try {
    const created = await api.post(`${base}/regenerate`, chosenModel(modelId));
    window.location.assign(pageUrl(created.slug));
  } catch (err) {
    setError(errorEl, `Could not regenerate: ${describeError(err)}`);
    button.disabled = false;
    button.textContent = label;
  }
}
