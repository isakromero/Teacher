// Source page: metadata, open the original file, generate a study page, list its study pages
// (with the labs made from each under it, see artifact_list.js) and the concepts it was linked to (grouped by role, each linking to the concept graph),
// delete the source (with an in-page confirmation step).

import { logEvent } from "./activity.js";
import { api, describeError, seg } from "./api.js";
import { artifactListNodes } from "./artifact_list.js";
import { byId, formatDate, h, setError, setChildren } from "./dom.js";
import { chosenModel, mountModelChoice } from "./model_choice.js";

const root = byId("source-page");
const slug = root.dataset.slug;
const base = `/api/sources/${seg(slug)}`;

// The study history: opening the original file (the browser opens it in a new tab) is logged.
const fileLink = byId("source-file-link");
const logFileOpened = () => logEvent("source_file_opened", { source: slug });
fileLink.addEventListener("click", logFileOpened);
fileLink.addEventListener("auxclick", (event) => {
  if (event.button === 1) logFileOpened(); // middle click
});

// -- metadata --------------------------------------------------------------------------------

function row(label, value) {
  if (value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length)) {
    return null;
  }
  return h("div", {}, h("dt", {}, label), h("dd", {}, value));
}

function identifierNode(identifier) {
  const text = `${identifier.scheme}:${identifier.value}`;
  if (identifier.scheme === "arxiv") {
    return h(
      "a",
      { href: `https://arxiv.org/abs/${encodeURIComponent(identifier.value)}`, target: "_blank", rel: "noopener noreferrer" },
      text
    );
  }
  return text;
}

async function loadSource() {
  const meta = byId("source-meta");
  let source;
  try {
    source = await api.get(base);
  } catch (err) {
    setChildren(meta, 
      h("div", {}, h("dt", {}, "Error"), h("dd", { class: "error-inline" }, `Could not load this source: ${describeError(err)}`))
    );
    return;
  }
  byId("source-title").textContent = source.title;
  document.title = `${source.title} - Teacher`;
  const identifiers = (source.identifiers || []).map(identifierNode);
  setChildren(meta, 
    ...[
      row("Kind", source.kind),
      row("Authors", (source.creators || []).join(", ")),
      row("Date", source.originated_on),
      row("Identifiers", identifiers.flatMap((node, i) => (i ? [", ", node] : [node]))),
      row("Link", source.origin_url ? h("a", { href: source.origin_url, target: "_blank", rel: "noopener noreferrer" }, source.origin_url) : null),
      row("File type", source.media_type),
      row("Added", formatDate(source.added_at)),
      row("Summary", source.summary),
      row("Notes", source.notes),
    ].filter(Boolean)
  );
}

// -- study pages -----------------------------------------------------------------------------

async function loadArtifacts() {
  const listEl = byId("artifact-list");
  let artifacts;
  try {
    artifacts = await api.get(`/api/artifacts?source=${encodeURIComponent(slug)}`);
  } catch (err) {
    setChildren(listEl, 
      h("p", { class: "error", role: "alert" }, `Could not load the study pages and labs: ${describeError(err)}`)
    );
    return;
  }
  if (!artifacts.length) {
    setChildren(listEl, h("p", { class: "muted" }, "No study pages yet."));
    return;
  }
  artifacts.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  setChildren(
    listEl,
    ...artifactListNodes(artifacts, {
      datePrefix: "Created ",
      empty: h("p", { class: "muted" }, "No current study page."),
    })
  );
}

// -- concepts --------------------------------------------------------------------------------

// The roles a source has towards a concept, in the order they are shown.
const CONCEPT_ROLES = [
  ["introduces", "Introduced here"],
  ["uses", "Used"],
  ["assumes", "Assumed background"],
];

function conceptItem(concept) {
  return h(
    "li",
    {},
    h("a", { href: `/graph?concept=${encodeURIComponent(concept.slug)}` }, concept.name),
    concept.reviewed ? null : h("span", { class: "tag" }, "unreviewed")
  );
}

async function loadConcepts() {
  const listEl = byId("concept-list");
  let concepts;
  try {
    concepts = await api.get(`${base}/concepts`);
  } catch (err) {
    setChildren(listEl, h("p", { class: "error", role: "alert" }, `Could not load the concepts: ${describeError(err)}`));
    return;
  }
  if (!concepts.length) {
    setChildren(listEl, h("p", { class: "muted" }, "No concepts have been extracted yet."));
    return;
  }
  const known = new Set(CONCEPT_ROLES.map(([role]) => role));
  const unknown = concepts.filter((c) => !known.has(c.role));
  if (unknown.length) {
    setChildren(listEl, h("p", { class: "error", role: "alert" }, `Unknown concept role: ${unknown[0].role}`));
    return;
  }
  setChildren(
    listEl,
    CONCEPT_ROLES.map(([role, label]) => {
      const items = concepts.filter((c) => c.role === role);
      if (!items.length) return null;
      return h(
        "section",
        { class: "concept-group", "aria-label": label },
        h("h3", {}, `${label} (${items.length})`),
        h("ul", { class: "concept-list" }, items.map(conceptItem))
      );
    })
  );
}

// -- generate --------------------------------------------------------------------------------

const generateButton = byId("generate-button");
mountModelChoice("generate-model", { errorEl: byId("generate-error") });
generateButton.addEventListener("click", async () => {
  const errorEl = byId("generate-error");
  setError(errorEl, null);
  generateButton.disabled = true;
  generateButton.textContent = "Starting...";
  try {
    const artifact = await api.post("/api/artifacts", {
      sources: [slug],
      ...chosenModel("generate-model"),
    });
    window.location.assign(`/artifacts/${seg(artifact.slug)}`);
  } catch (err) {
    setError(errorEl, `Could not start a study page: ${describeError(err)}`);
    generateButton.disabled = false;
    generateButton.textContent = "Generate study page";
  }
});

// -- delete (inline confirmation; no browser dialogs) ------------------------------------------

const deleteButton = byId("delete-button");
const confirmBox = byId("delete-confirm");
const confirmButton = byId("delete-confirm-button");
const cancelButton = byId("delete-cancel-button");
const deleteError = byId("delete-error");

function openConfirm() {
  setError(deleteError, null);
  deleteButton.hidden = true;
  confirmBox.hidden = false;
  cancelButton.focus();
}

function closeConfirm() {
  confirmBox.hidden = true;
  deleteButton.hidden = false;
  deleteButton.focus();
}

deleteButton.addEventListener("click", openConfirm);
cancelButton.addEventListener("click", closeConfirm);
confirmBox.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeConfirm();
});
confirmButton.addEventListener("click", async () => {
  setError(deleteError, null);
  confirmButton.disabled = true;
  confirmButton.textContent = "Deleting...";
  try {
    await api.delete(base);
    window.location.assign("/library");
  } catch (err) {
    confirmButton.disabled = false;
    confirmButton.textContent = "Yes, delete it";
    closeConfirm();
    setError(deleteError, `Could not delete this source: ${describeError(err)}`);
  }
});

loadSource();
loadArtifacts();
loadConcepts();
