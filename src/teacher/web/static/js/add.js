// The add-a-source page (/add): a PDF from this computer or a web address, its details detected
// for the reader to check, then optionally a study page started from it. Moved here from the
// library page unchanged; the element ids are the same.

import { api, describeError, seg } from "./api.js";
import { byId, h, plural, setError, setChildren } from "./dom.js";
import { chosenModel, mountModelChoice } from "./model_choice.js";
import { prefilledUrl } from "./source_references_model.js";

const form = byId("upload-form");
const submitButton = byId("upload-submit");
const statusText = byId("upload-status");
const errorEl = byId("upload-error");
mountModelChoice("upload-model", { errorEl });
// The model only matters when a study page is generated after the upload.
byId("upload-generate").addEventListener("change", (event) => {
  byId("upload-model").disabled = !event.target.checked;
});

// -- upload ----------------------------------------------------------------------------------
//
// Flow, PDF: the reader picks a PDF -> it is posted to /api/sources/inspect, which detects the
// details -> the form appears pre-filled (or empty, with the error, if detection failed) ->
// "Add to library" posts the same File object, kept in memory, to /api/sources.
//
// Flow, web address: the reader types an address and presses Fetch -> it is posted to
// /api/sources/inspect-url, which fetches it and detects the details -> the form appears
// pre-filled -> "Add to library" posts the address and the details to /api/sources/from-url,
// which fetches it again and stores it. An address that cannot be fetched or read cannot be
// added: the error is shown and the form stays closed.

const fileInput = byId("upload-file");
const detectStatus = byId("upload-detect-status");
const detectError = byId("upload-detect-error");
const duplicateEl = byId("upload-duplicate");
const detailsEl = byId("upload-details");
const detectedNote = byId("upload-detected-note");
const fields = {
  title: byId("upload-title"),
  authors: byId("upload-authors"),
  arxiv: byId("upload-arxiv"),
  year: byId("upload-year"),
  doi: byId("upload-doi"),
  summary: byId("upload-summary"),
};

const urlInput = byId("upload-url");
const urlFetch = byId("upload-url-fetch");
const modeInputs = { file: byId("upload-mode-file"), url: byId("upload-mode-url") };

let mode = "file"; // "file" | "url": which of the two the reader is adding
let chosenFile = null; // the File being added; posted twice (inspect, then upload)
let chosenUrl = null; // the fetched web address being added (after redirects)
const hasSource = () => (mode === "file" ? Boolean(chosenFile) : Boolean(chosenUrl));
let detection = 0; // id of the latest detection; older replies are ignored
let uploading = false;

function setFields(values = {}) {
  for (const [name, input] of Object.entries(fields)) {
    const value = values[name];
    input.value = value === null || value === undefined ? "" : String(value);
  }
}

function setDetecting(busy) {
  detectStatus.textContent = !busy ? "" : mode === "url" ? "Fetching the address..." : "Reading the paper...";
  detectStatus.classList.toggle("is-busy", busy);
  form.setAttribute("aria-busy", busy ? "true" : "false");
  submitButton.disabled = busy || !hasSource();
  urlFetch.disabled = busy;
}

function clearUpload() {
  detection += 1; // forget any detection still running
  chosenFile = null;
  chosenUrl = null;
  setDetecting(false);
  setError(detectError, null);
  setError(errorEl, null);
  duplicateEl.hidden = true;
  duplicateEl.replaceChildren();
  detailsEl.hidden = true;
  setFields();
  submitButton.disabled = true;
}

function showDetails(note) {
  setChildren(detectedNote, note);
  detailsEl.hidden = false;
  submitButton.disabled = false;
  // Move to the first field, unless the reader has gone elsewhere while the paper was read.
  const active = document.activeElement;
  if (!active || active === document.body || [fileInput, urlInput, urlFetch].includes(active)) {
    fields.title.focus();
  }
}

function showDuplicate(slug, what) {
  submitButton.disabled = true;
  setChildren(
    duplicateEl,
    "This is already in your library: ",
    h("a", { href: `/sources/${seg(slug)}` }, "open it"),
    `. ${what}`
  );
  duplicateEl.hidden = false;
}

// `skip`: fields that are not expected at all (a web page has no arXiv id or DOI to miss).
function missingNote(values, skip = []) {
  const missing = Object.keys(FIELD_LABELS).filter(
    (name) => !skip.includes(name) && !String(values[name] ?? "").trim()
  );
  return missing.length ? ` Not found: ${missing.map((name) => FIELD_LABELS[name]).join(", ")}.` : null;
}

const FIELD_LABELS = {
  title: "title",
  authors: "authors",
  year: "year",
  arxiv: "arXiv id",
  doi: "DOI",
  summary: "summary",
};

function detectedValues(found) {
  return {
    title: found.title,
    authors: (found.authors || []).join(", "),
    year: found.year,
    arxiv: found.arxiv_id,
    doi: found.doi,
    summary: found.summary,
  };
}

async function detect(file) {
  const id = ++detection;
  chosenFile = file;
  setError(detectError, null);
  setError(errorEl, null);
  duplicateEl.hidden = true;
  duplicateEl.replaceChildren();
  detailsEl.hidden = true;
  setFields();
  setDetecting(true);

  const data = new FormData();
  data.append("file", file);
  let found;
  try {
    found = await api.postForm("/api/sources/inspect", data);
  } catch (err) {
    if (id !== detection) return;
    setDetecting(false);
    setError(detectError, `The details could not be detected automatically: ${describeError(err)}`);
    showDetails("Fill in the details by hand.");
    return;
  }
  if (id !== detection) return;
  setDetecting(false);

  if (found.duplicate_of) {
    showDuplicate(found.duplicate_of, "Choose a different file to add another paper.");
    return;
  }

  const values = detectedValues(found);
  setFields(values);
  showDetails([
    "These details were detected automatically from the first pages of the PDF. " +
      "Check them and correct anything that is wrong before adding the paper.",
    missingNote(values),
  ]);
}

async function detectUrl() {
  if (uploading) return;
  const url = urlInput.value.trim();
  const id = ++detection;
  chosenUrl = null;
  setError(detectError, null);
  setError(errorEl, null);
  duplicateEl.hidden = true;
  duplicateEl.replaceChildren();
  detailsEl.hidden = true;
  setFields();
  if (!/^https?:\/\/\S+$/i.test(url)) {
    submitButton.disabled = true;
    setError(detectError, "Enter a web address that starts with http:// or https://.");
    return;
  }
  setDetecting(true);

  let found;
  try {
    found = await api.post("/api/sources/inspect-url", { url });
  } catch (err) {
    if (id !== detection) return;
    setDetecting(false);
    setError(detectError, `This address cannot be added: ${describeError(err)}`);
    return;
  }
  if (id !== detection) return;

  if (found.duplicate_of) {
    setDetecting(false);
    showDuplicate(found.duplicate_of, "Enter a different address to add something else.");
    return;
  }
  chosenUrl = found.url;
  setDetecting(false);
  const values = detectedValues(found);
  setFields(values);
  if (found.detection_error) {
    setError(detectError, `The details could not be detected automatically: ${found.detection_error}`);
    showDetails("The PDF was fetched. Fill in the details by hand.");
    return;
  }
  showDetails([
    found.kind === "paper"
      ? "This address is a PDF. These details were detected automatically from its first pages. "
      : `Teacher read ${plural(found.words, "word")} of text from this page. These details come from the page itself. `,
    "Check them and correct anything that is wrong before adding it.",
    missingNote(values, found.kind === "paper" ? [] : ["arxiv", "doi"]),
  ]);
}

urlFetch.addEventListener("click", detectUrl);
urlInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  event.preventDefault(); // Enter fetches the address; it must not submit the form
  detectUrl();
});
urlInput.addEventListener("input", () => {
  // The details belong to the address that was fetched: a changed address must be fetched again.
  if (chosenUrl || !detailsEl.hidden) clearUpload();
});

function setMode(next) {
  if (uploading) return;
  mode = next;
  modeInputs[mode].checked = true;
  byId("upload-file-field").hidden = mode !== "file";
  byId("upload-url-field").hidden = mode !== "url";
  fileInput.value = "";
  clearUpload();
}
for (const [name, input] of Object.entries(modeInputs)) {
  input.addEventListener("change", () => input.checked && setMode(name));
}

// A web address filled in from elsewhere: an /add?url=... link (a reference's "Add to library")
// or the "Cited by your library" list (add_frontier.js, through the `teacher:prefill-url`
// event). It only fills the field; the reader presses Fetch.
function prefillUrl(url) {
  if (uploading) return;
  setMode("url");
  urlInput.value = url;
  urlFetch.focus();
}
document.addEventListener("teacher:prefill-url", (event) => prefillUrl(event.detail.url));
const linked = prefilledUrl(window.location.search);
if (linked && linked.error) {
  setMode("url");
  setError(detectError, linked.error);
} else if (linked) {
  prefillUrl(linked.url);
}

fileInput.addEventListener("change", () => {
  if (uploading) return;
  const file = fileInput.files[0];
  if (!file) {
    clearUpload();
    return;
  }
  detect(file);
});

// The details as typed. Throws an Error listing what is missing or malformed.
function buildForm() {
  const file = chosenFile;
  const title = fields.title.value.trim();
  const authors = fields.authors.value;
  const arxiv = fields.arxiv.value.trim().replace(/^arxiv:\s*/i, "");
  const year = fields.year.value.trim();
  const doi = fields.doi.value.trim().replace(/^(https?:\/\/(dx\.)?doi\.org\/|doi:\s*)/i, "");
  const summary = fields.summary.value.trim();

  const problems = [];
  if (mode === "file" && !file) problems.push("Choose a PDF file.");
  if (mode === "url" && !chosenUrl) problems.push("Fetch a web address first.");
  if (!title) problems.push("Enter a title.");
  if (year && !/^\d{4}$/.test(year)) problems.push("Year must be four digits, like 2017.");
  if (problems.length) throw new Error(problems.join(" "));

  const creators = authors.split(",").map((a) => a.trim()).filter(Boolean);
  if (mode === "url") {
    return {
      url: chosenUrl,
      title,
      creators,
      identifiers: [arxiv && `arxiv:${arxiv}`, doi && `doi:${doi}`].filter(Boolean),
      originated_on: year || null,
      summary: summary || null,
    };
  }

  const data = new FormData();
  data.append("file", file);
  data.append("kind", "paper");
  data.append("title", title);
  for (const name of creators) data.append("creators", name);
  if (arxiv) data.append("identifiers", `arxiv:${arxiv}`);
  if (doi) data.append("identifiers", `doi:${doi}`);
  if (year) data.append("originated_on", year);
  if (summary) data.append("summary", summary);
  return data;
}

function setBusy(busy, message = "") {
  uploading = busy;
  submitButton.disabled = busy;
  fileInput.disabled = busy;
  urlInput.disabled = busy;
  urlFetch.disabled = busy;
  for (const input of Object.values(modeInputs)) input.disabled = busy;
  statusText.textContent = message;
  form.setAttribute("aria-busy", busy ? "true" : "false");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (uploading || submitButton.disabled) return; // e.g. Enter in a field while detecting
  setError(errorEl, null);
  let data;
  try {
    data = buildForm();
  } catch (err) {
    setError(errorEl, err.message);
    return;
  }
  const generate = byId("upload-generate").checked;

  const fromUrl = mode === "url";
  setBusy(true, fromUrl ? "Fetching and saving..." : "Uploading...");
  let source;
  try {
    source = fromUrl
      ? await api.post("/api/sources/from-url", data)
      : await api.postForm("/api/sources", data);
  } catch (err) {
    setBusy(false);
    setError(errorEl, `${fromUrl ? "Adding the address failed" : "Upload failed"}: ${describeError(err)}`);
    return;
  }

  if (!generate) {
    window.location.assign(`/sources/${seg(source.slug)}`);
    return;
  }
  setBusy(true, "Added. Starting the study page...");
  try {
    const artifact = await api.post("/api/artifacts", {
      sources: [source.slug],
      ...chosenModel("upload-model"),
    });
    window.location.assign(`/artifacts/${seg(artifact.slug)}`);
  } catch (err) {
    setBusy(false);
    form.reset();
    setMode("file"); // form.reset() went back to the first choice; this also clears the form
    setError(
      errorEl,
      h(
        "span",
        {},
        `The source was added, but the study page could not be started: ${describeError(err)}. `,
        h("a", { href: `/sources/${seg(source.slug)}` }, "Open the source"),
        " to try again."
      )
    );
  }
});
