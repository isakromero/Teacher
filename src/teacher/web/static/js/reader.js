// The reader (artifact status `ready`): a single column of the visible blocks, a progress bar
// (gates cleared / total), a "N more sections locked" footer and a completion message. The
// glossary (glossary.js) marks terms in the blocks and is refreshed whenever a gate is cleared
// or blocks unlock, since the API lists only terms in what the reader can see. Wide content
// that scrolls sideways in its own box gets an edge hint (scroll_hints.js).

import { api, seg } from "./api.js";
import { renderBlock } from "./blocks/index.js";
import { byId, h, plural, prefersReducedMotion, showPageError } from "./dom.js";
import { mountGlossary } from "./glossary.js";
import { mountScrollHints } from "./scroll_hints.js";

const CLEARED = new Set(["passed", "revealed"]);

export async function mountReader(artifact) {
  const slug = artifact.slug;
  const base = `/api/artifacts/${seg(slug)}`;
  const data = await api.get(`${base}/blocks`);
  if (!data || !Array.isArray(data.blocks)) {
    throw new Error("Unexpected response from the server: the blocks list is missing.");
  }

  const list = byId("blocks");
  const footer = byId("reader-footer");
  const progress = byId("reader-progress");
  const gatesTotal = artifact.progress ? artifact.progress.gates_total : 0;
  const gateStates = new Map(); // key -> state, for every visible gate
  const rendered = new Set();
  let lockedBlocks = data.locked_blocks;
  let complete = data.complete;
  let glossary = null;

  function paintProgress() {
    if (!gatesTotal) {
      progress.hidden = true;
      return;
    }
    const cleared = [...gateStates.values()].filter((s) => CLEARED.has(s)).length;
    const fill = progress.querySelector(".progress-fill");
    const track = progress.querySelector(".progress-track");
    fill.style.width = `${Math.round((100 * cleared) / gatesTotal)}%`;
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-label", "Checkpoints cleared");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", String(gatesTotal));
    track.setAttribute("aria-valuenow", String(cleared));
    progress.querySelector(".progress-label").textContent =
      `${cleared} of ${plural(gatesTotal, "checkpoint")} cleared`;
    progress.hidden = false;
  }

  function paintFooter() {
    footer.replaceChildren();
    if (lockedBlocks > 0) {
      footer.append(
        h(
          "p",
          { class: "locked-note" },
          `${lockedBlocks} more ${lockedBlocks === 1 ? "section" : "sections"} locked. `,
          "Answer the checkpoint above to continue."
        )
      );
    } else if (complete) {
      footer.append(
        h(
          "div",
          { class: "complete-note" },
          h("p", { class: "gate-subhead" }, "You have finished this study page."),
          h("p", {}, h("a", { href: "/library" }, "Back to the library"))
        )
      );
    }
  }

  function add(block) {
    if (rendered.has(block.key)) return null;
    rendered.add(block.key);
    if (block.is_gate) gateStates.set(block.key, block.state || "open");
    const el = renderBlock(block, ctx);
    list.append(el);
    return el;
  }

  const ctx = {
    slug,
    baseUrl: `${base}/`,
    // Study-page questions ask how sure the reader is (gate.js documents this context).
    askConfidence: true,
    submit: (block, response, meta) =>
      api.post(`${base}/blocks/${seg(block.key)}/responses`, { response, ...meta }),
    loadResponses: (block) => api.get(`${base}/blocks/${seg(block.key)}/responses`),
    onSubmitted(block, result) {
      gateStates.set(block.key, result.state);
      if (typeof result.locked_blocks === "number") lockedBlocks = result.locked_blocks;
      if (typeof result.complete === "boolean") complete = result.complete;
      const added = (result.unlocked || []).map(add).filter(Boolean);
      paintProgress();
      paintFooter();
      if (glossary && (added.length || result.revealed)) glossary.refresh();
      if (added.length) {
        added[0].focus({ preventScroll: true });
        added[0].scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
      }
    },
  };

  list.replaceChildren();
  if (!data.blocks.length) {
    list.append(h("p", { class: "muted" }, "This study page has no content."));
  }
  data.blocks.forEach(add);
  paintProgress();
  paintFooter();
  glossary = mountGlossary(artifact);
  mountScrollHints(list);
  moveToLinkedBlock(list);
}

/** A link to one block (`#block-<key>`, as the home page's search gives) moves to it once the
 * blocks are drawn; a block that is not among them is said so, not ignored. */
function moveToLinkedBlock(list) {
  const hash = window.location.hash;
  if (!hash.startsWith("#block-")) return;
  const id = decodeURIComponent(hash.slice(1));
  const target = document.getElementById(id);
  if (!target || !list.contains(target)) {
    showPageError(`The section this link points to (${id.slice("block-".length)}) is not on the part of the page you can see.`);
    return;
  }
  target.scrollIntoView({ block: "start" });
  target.focus({ preventScroll: true });
}
