// The "Model" select shown wherever a study page is generated or regenerated, and next to the
// concept graph's "Find relations" / "Regenerate relations". The choices come from an endpoint
// answering `{selectable, default, models: [{id, label}]}` (GET /api/generation-models for study
// pages, GET /api/graph/relate-models for relations); a generator that cannot be given a model
// offers none, and the field then stays hidden.

import { api, describeError } from "./api.js";
import { byId, h, setChildren, setError } from "./dom.js";

export const GENERATION_MODELS_URL = "/api/generation-models";
export const RELATE_MODELS_URL = "/api/graph/relate-models";

const loading = new Map(); // url -> Promise of the choices

/** The model choices at `url` (fetched once per page; a failed fetch is tried again next time). */
export function loadModelChoices(url = GENERATION_MODELS_URL) {
  if (!loading.has(url)) {
    const pending = api.get(url).catch((err) => {
      loading.delete(url); // the next mount tries again
      throw err;
    });
    loading.set(url, pending);
  }
  return loading.get(url);
}

/** The study-page model choices (GET /api/generation-models). */
export function loadGenerationModels() {
  return loadModelChoices(GENERATION_MODELS_URL);
}

/** The <option>s for `choices`, the default marked; a `wanted` model the server no longer offers
 * is listed anyway, so sending it fails visibly instead of another model being used quietly. */
function modelOptions(choices, wanted) {
  const options = choices.models.map((m) =>
    h("option", { value: m.id }, m.id === choices.default ? `${m.label} (default)` : m.label)
  );
  if (!choices.models.some((m) => m.id === wanted)) {
    options.push(h("option", { value: wanted }, `${wanted} (no longer offered)`));
  }
  return options;
}

/**
 * Fill the select `#<id>` and reveal its field `#<id>-field`, preselecting `selected` (a model
 * id) or else the default. A load failure is reported in `errorEl` and leaves the field hidden.
 */
export async function mountModelChoice(id, { selected = null, errorEl } = {}) {
  const select = byId(id);
  const field = byId(`${id}-field`);
  let choices;
  try {
    choices = await loadGenerationModels();
  } catch (err) {
    field.hidden = true;
    setError(
      errorEl,
      `Could not load the list of models: ${describeError(err)}. ` +
        "Generating now uses the model this page was last generated with, or the default."
    );
    return;
  }
  if (!choices.selectable) {
    field.hidden = true;
    return;
  }
  const wanted = selected ?? choices.default;
  setChildren(select, ...modelOptions(choices, wanted));
  select.value = wanted;
  field.hidden = false;
}

/** The request-body fields for the model chosen in `#<id>`: `{model}`, or `{}` when the field
 * is hidden (no choice offered), which leaves the model to the server. */
export function chosenModel(id) {
  const select = byId(id);
  return byId(`${id}-field`).hidden || !select.value ? {} : { model: select.value };
}

/**
 * A labelled model select built from loaded `choices` (for panels that are re-rendered, where
 * `mountModelChoice`'s fixed elements do not fit): `value` is preselected (else the default) and
 * `onChange(id)` is called when the reader picks another. Returns the field element.
 */
export function modelSelect(choices, { id, label = "Model", value = null, onChange }) {
  const wanted = value ?? choices.default;
  const select = h("select", { id, name: "model", onChange: () => onChange(select.value) }, modelOptions(choices, wanted));
  select.value = wanted;
  return h("div", { class: "field model-field" }, h("label", { for: id }, label), select);
}
