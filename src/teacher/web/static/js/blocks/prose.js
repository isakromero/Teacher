// `prose` block: { markdown, role? } rendered as markdown with maths. `role` is "explanation"
// (also when absent: the source's own account) or "critique": the one place a page goes beyond
// the source, so it is set apart under a fixed label that says so. Any other role is an error.

import { h } from "../dom.js";
import { renderMarkdown } from "../markdown.js";

export const CRITIQUE_LABEL = "Beyond the source: critique and open questions";
export const CRITIQUE_NOTE =
  "This part is not the authors' own account: it adds outside context, criticism and questions the source leaves open.";

export function renderProse(block, ctx) {
  const role = block.content.role ?? "explanation";
  const el = renderMarkdown(block.content.markdown, { baseUrl: ctx.baseUrl });
  el.classList.add("prose");
  if (role === "explanation") return el;
  if (role !== "critique") throw new Error(`unknown prose role ${JSON.stringify(role)}`);
  const labelId = `critique-${block.key}-label`;
  return h(
    "aside",
    { class: "prose-critique", "aria-labelledby": labelId },
    h("p", { class: "prose-critique-label", id: labelId }, CRITIQUE_LABEL),
    h("p", { class: "prose-critique-note" }, CRITIQUE_NOTE),
    el
  );
}
