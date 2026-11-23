// The study page's "Hands-on lab" section (status `ready`, after the reader): the labs made
// from this page (GET /api/artifacts/{slug}/labs: the current versions, newest first, each with
// its status and progress) and "Create a lab" with the model picker
// (POST /api/artifacts/{slug}/labs -> 202, the new lab, which is then opened: its page shows
// the generation's progress). A failure to list or to create is shown in the section.

import { api, describeError, seg } from "./api.js";
import { artifactItem, artifactUrl } from "./artifact_list.js";
import { byId, h, setChildren, setError } from "./dom.js";
import { chosenModel, mountModelChoice } from "./model_choice.js";

const MODEL_ID = "create-lab-model";

export function mountLabs(artifact) {
  const base = `/api/artifacts/${seg(artifact.slug)}`;
  const section = byId("study-labs");
  const listEl = byId("study-lab-list");
  const button = byId("create-lab-button");
  const errorEl = byId("create-lab-error");

  async function loadLabs() {
    setChildren(listEl, h("p", { class: "muted" }, "Loading the labs of this page..."));
    let labs;
    try {
      labs = await api.get(`${base}/labs`);
    } catch (err) {
      setChildren(listEl, h("p", { class: "error", role: "alert" }, `Could not load the labs: ${describeError(err)}`));
      return;
    }
    if (!Array.isArray(labs)) {
      setChildren(listEl, h("p", { class: "error", role: "alert" }, "Unexpected response from the server: no list of labs."));
      return;
    }
    button.textContent = labs.length ? "Create another lab" : "Create a lab";
    setChildren(
      listEl,
      labs.length
        ? h("ul", { class: "artifact-list" }, labs.map((lab) => artifactItem(lab, { datePrefix: "Created " })))
        : h("p", { class: "muted" }, "No lab has been made from this page yet.")
    );
  }

  async function create() {
    const label = button.textContent;
    setError(errorEl, null);
    button.disabled = true;
    button.textContent = "Starting...";
    try {
      const lab = await api.post(`${base}/labs`, chosenModel(MODEL_ID));
      window.location.assign(artifactUrl(lab));
    } catch (err) {
      setError(errorEl, `Could not create a lab: ${describeError(err)}`);
      button.disabled = false;
      button.textContent = label;
    }
  }

  button.onclick = create;
  section.hidden = false;
  mountModelChoice(MODEL_ID, { errorEl });
  loadLabs();
}
