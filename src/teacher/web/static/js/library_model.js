// Pure helpers for the library page (no DOM, so node can test them): grouping study pages and
// labs under their sources, and the filter box. Data: GET /api/sources and GET /api/artifacts.

/** Lower case, accents removed: "Bézier" -> "bezier". */
export function fold(text) {
  return String(text ?? "")
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .toLowerCase();
}

/** The words of a filter (letters and digits), folded. */
export function filterWords(text) {
  return fold(text).match(/[\p{L}\p{N}]+/gu) || [];
}

const newest = (a, b) =>
  String(b.added_at || b.created_at || "").localeCompare(String(a.added_at || a.created_at || ""));

/**
 * The sources newest first, each with its study pages and labs (newest first):
 * `[{source, artifacts}]`. An artifact linked to several sources is listed under each.
 */
export function groupBySource(sources, artifacts) {
  const bySource = new Map();
  for (const artifact of artifacts) {
    for (const s of artifact.sources || []) {
      if (!bySource.has(s.slug)) bySource.set(s.slug, []);
      bySource.get(s.slug).push(artifact);
    }
  }
  return [...sources].sort(newest).map((source) => ({
    source,
    artifacts: [...(bySource.get(source.slug) || [])].sort(newest),
  }));
}

/** The text a filter looks in for one group: the source's title, authors, date and
 * identifiers, and the titles of its study pages and labs. */
export function groupText(group) {
  const s = group.source;
  return fold(
    [
      s.title,
      ...(s.creators || []),
      s.originated_on,
      ...(s.identifiers || []).map((i) => `${i.scheme} ${i.value}`),
      ...group.artifacts.map((a) => a.title),
    ]
      .filter(Boolean)
      .join(" \n ")
  );
}

/** Whether every filter word starts a word of the group's text (an empty filter keeps all). */
export function groupMatches(group, words) {
  if (!words.length) return true;
  const text = groupText(group);
  const have = new Set(text.match(/[\p{L}\p{N}]+/gu) || []);
  return words.every((w) => [...have].some((h) => h.startsWith(w)));
}

/** "2 sources, 5 study pages and 1 lab" style count. */
export function libraryCount(groups) {
  const artifacts = new Map();
  for (const g of groups) for (const a of g.artifacts) artifacts.set(a.slug, a);
  const labs = [...artifacts.values()].filter((a) => a.kind === "lab").length;
  const pages = artifacts.size - labs;
  const n = (count, one, many) => `${count} ${count === 1 ? one : many}`;
  const parts = [n(groups.length, "source", "sources"), n(pages, "study page", "study pages")];
  if (labs) parts.push(n(labs, "lab", "labs"));
  return parts.length === 3 ? `${parts[0]}, ${parts[1]} and ${parts[2]}` : `${parts[0]} and ${parts[1]}`;
}
