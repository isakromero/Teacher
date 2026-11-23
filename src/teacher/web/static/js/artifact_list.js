// Lists of artifacts (the library, a source page, the labs of a study page). The API lists two
// kinds: study pages and labs (`kind: "lab"`, made from a study page whose slug is in
// `params.study_page`). Each kind has its own page (`/artifacts/{slug}`, `/labs/{slug}`) and its
// own wording for progress: a study page's gates are checkpoints, a lab's are its checked
// steps. In a list, a lab is shown under the study page it was made from when that page is in
// the same list, and on its own otherwise (its study page was deleted, or is not listed).

import { seg } from "./api.js";
import { formatDate, h, plural, statusBadge } from "./dom.js";

export const LAB_KIND = "lab";

export const isLab = (artifact) => artifact.kind === LAB_KIND;

/** The page an artifact is read on. */
export function artifactUrl(artifact) {
  return isLab(artifact) ? `/labs/${seg(artifact.slug)}` : `/artifacts/${seg(artifact.slug)}`;
}

/** "2 of 5 checkpoints cleared" / "1 of 2 checked steps passed", or null when there is none. */
export function progressText(artifact) {
  const p = artifact.progress;
  if (artifact.status !== "ready" || !p || !p.gates_total) return null;
  return isLab(artifact)
    ? `${p.gates_cleared} of ${plural(p.gates_total, "checked step")} passed`
    : `${p.gates_cleared} of ${plural(p.gates_total, "checkpoint")} cleared`;
}

/**
 * One artifact as a list item: link, status, "Lab" and "superseded" tags, the date (with
 * `datePrefix`, e.g. "Created ") and the progress; `children` (e.g. a nested list of labs)
 * goes at the end.
 */
export function artifactItem(artifact, { datePrefix = "", children = null } = {}) {
  const progress = progressText(artifact);
  return h(
    "li",
    {
      class: artifact.superseded_by ? "artifact-item is-superseded" : "artifact-item",
      dataset: { kind: artifact.kind },
    },
    h("a", { href: artifactUrl(artifact) }, artifact.title),
    " ",
    isLab(artifact) ? h("span", { class: "tag tag-lab" }, "Lab") : null,
    isLab(artifact) ? " " : null,
    statusBadge(artifact.status),
    artifact.superseded_by ? h("span", { class: "tag" }, "superseded") : null,
    h(
      "span",
      { class: "muted small" },
      ` ${datePrefix}${formatDate(artifact.created_at)}`,
      progress ? ` - ${progress}` : null
    ),
    children
  );
}

/**
 * `artifacts` (already in display order) as list items, each lab nested under its study page
 * when that page is in the list. Returns the `<li>` elements.
 */
export function artifactItems(artifacts, { datePrefix = "" } = {}) {
  const pages = new Set(artifacts.filter((a) => !isLab(a)).map((a) => a.slug));
  const labsOf = new Map();
  const loose = [];
  for (const artifact of artifacts) {
    if (!isLab(artifact)) continue;
    const page = artifact.params ? artifact.params.study_page : null;
    if (page && pages.has(page)) {
      if (!labsOf.has(page)) labsOf.set(page, []);
      labsOf.get(page).push(artifact);
    } else {
      loose.push(artifact);
    }
  }
  const items = [];
  for (const artifact of artifacts) {
    if (isLab(artifact)) {
      if (loose.includes(artifact)) items.push(artifactItem(artifact, { datePrefix }));
      continue;
    }
    const labs = labsOf.get(artifact.slug) || [];
    items.push(
      artifactItem(artifact, {
        datePrefix,
        children: labs.length
          ? h(
              "ul",
              { class: "artifact-list lab-sublist", "aria-label": `Labs made from ${artifact.title}` },
              labs.map((lab) => artifactItem(lab, { datePrefix }))
            )
          : null,
      })
    );
  }
  return items;
}
