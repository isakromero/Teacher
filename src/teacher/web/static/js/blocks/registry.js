// Block type -> renderer registry.
//
// A renderer is `(block, ctx) => HTMLElement`, where `block` is one entry of
// GET /api/artifacts/{slug}/blocks and `ctx` is the reader context (see reader.js).
// To support a new block type, write a renderer and register it in blocks/index.js.

import { h } from "../dom.js";

const renderers = new Map();

export function registerBlock(type, renderer) {
  if (renderers.has(type)) throw new Error(`block type ${type} is already registered`);
  renderers.set(type, renderer);
}

function unsupported(block) {
  return h(
    "div",
    { class: "notice notice-warn", role: "note" },
    h("p", {}, h("strong", {}, `Unsupported block type: ${block.type}`)),
    block.is_gate
      ? h(
          "p",
          {},
          "This is a checkpoint this page cannot display yet, so the sections after it stay locked."
        )
      : null
  );
}

function segmentNote(block) {
  const segments = block.segments || [];
  if (!segments.length) return null;
  const parts = segments.map((s) => (s.title ? `${s.label} (${s.title})` : s.label));
  return h("p", { class: "block-sources" }, `From the source: ${parts.join("; ")}`);
}

/** Render one block, wrapped in an element identifying it; never throws. */
export function renderBlock(block, ctx) {
  const renderer = renderers.get(block.type);
  let body;
  try {
    body = renderer ? renderer(block, ctx) : unsupported(block);
  } catch (err) {
    console.error(err);
    body = h(
      "div",
      { class: "notice notice-error", role: "alert" },
      `Could not display block ${block.key} (${block.type}): ${err.message}`
    );
  }
  return h(
    "article",
    {
      class: `block block-${block.type}`,
      id: `block-${block.key}`,
      tabindex: "-1",
      // data-block-key: the tutor chat attributes selected text to the block it comes from.
      dataset: { key: block.key, blockKey: block.key, type: block.type },
    },
    body,
    segmentNote(block)
  );
}
