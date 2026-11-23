// Pure helpers for the source page's "Related sources" section (no DOM, so node can test them):
// concept relations, readiness and citations between library sources.
// The data comes from GET /api/sources/{slug}/related (teacher/web/routers/source_graph.py; the
// rules are in teacher/source_graph.py). Anything the helpers do not recognise (a role, a
// standing status, a missing field) is an error, never a guess.

import { statusLabel } from "./standing_model.js";

const ROLE_WORDS = {
  introduces: "introduced",
  uses: "used",
  assumes: "assumed",
};

/** How a source treats a concept, in a word ("used"); an unknown role is an error. */
export function roleWord(role) {
  if (!ROLE_WORDS[role]) throw new Error(`unknown concept role ${role}`);
  return ROLE_WORDS[role];
}

export const sourceHref = (slug) => `/sources/${encodeURIComponent(slug)}`;
export const pageHref = (slug) => `/artifacts/${encodeURIComponent(slug)}`;
export const conceptHref = (slug) => `/graph?concept=${encodeURIComponent(slug)}`;

/** "a", "a and b", "a, b and c". */
export function listText(words) {
  if (words.length <= 1) return words.join("");
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

/** "attention (assumed) and softmax (used)" for a builds_on edge's concepts. */
export function conceptsText(concepts) {
  if (!Array.isArray(concepts) || !concepts.length) throw new Error("an edge without concepts");
  return listText(concepts.map((c) => `${c.name} (${roleWord(c.role)})`));
}

/** The shared concepts of a related edge: "attention (both introduce it)" or
 * "softmax (used here, introduced there)". */
export function sharedText(shared) {
  if (!Array.isArray(shared) || !shared.length) throw new Error("a related edge without shared concepts");
  return listText(
    shared.map((c) =>
      c.role === c.other_role
        ? `${c.name} (${roleWord(c.role)} in both)`
        : `${c.name} (${roleWord(c.role)} here, ${roleWord(c.other_role)} there)`
    )
  );
}

/** A related edge's weight (0..1) as "42% overlap". */
export function weightText(weight) {
  if (typeof weight !== "number" || !(weight > 0) || weight > 1) {
    throw new Error(`invalid related weight ${weight}`);
  }
  return `${Math.max(1, Math.round(weight * 100))}% overlap`;
}

// Why a source's citations are unknown (its `cites` is null), by the latest run's state.
const NOT_STORED_TEXT = {
  never_run: "Its references have not been extracted yet, so what it cites is not known.",
  running: "Its references are being extracted.",
  interrupted: "Its reference extraction was interrupted, so what it cites is not known: extract them again under References.",
  failed: "Its reference extraction failed, so what it cites is not known: see References.",
};

// What to add when references are stored but the latest run did not succeed.
const OLDER_RUN_TEXT = {
  running: "A new extraction of its references is running; these are from the previous one.",
  interrupted: "The latest extraction of its references was interrupted; these are from an earlier one.",
  failed: "The latest extraction of its references failed; these are from an earlier one.",
};

/**
 * The "Cites" and "Cited by" groups of GET /api/sources/{slug}/related:
 * `{cites: {items, note}, citedBy: {items, note}}`. `cites.items` is null when the source's
 * references are not stored (then `note` says why: never extracted, running, interrupted or
 * failed), never an empty list; `citedBy.note` names the other sources whose references are
 * not stored (they could cite it). Items are `{slug, title, href}`.
 */
export function citationsView(data) {
  const refs = data.references;
  if (!refs || typeof refs.stored !== "boolean") throw new Error("the relations have no reference state");
  const known = ["never_run", "running", "interrupted", "done", "failed"];
  if (!known.includes(refs.state)) throw new Error(`unknown reference extraction state ${refs.state}`);
  const item = (entry) => ({ slug: entry.source.slug, title: entry.source.title, href: sourceHref(entry.source.slug) });
  let cites;
  if (!refs.stored) {
    if (data.cites !== null) throw new Error("citations listed for a source without stored references");
    if (refs.state === "done") throw new Error("a done extraction without stored references");
    cites = { items: null, note: NOT_STORED_TEXT[refs.state] };
  } else {
    if (!Array.isArray(data.cites)) throw new Error("the relations have no cites list");
    const items = data.cites.map(item);
    const empty = items.length ? null : "It cites no other source in the library.";
    const older = refs.state === "done" ? null : OLDER_RUN_TEXT[refs.state];
    if (refs.state === "never_run") throw new Error("stored references without an extraction");
    cites = { items, note: [empty, older].filter(Boolean).join(" ") || null };
  }
  const citedByData = requireList(data, "cited_by");
  const unread = requireList(data, "references_not_extracted");
  const citedItems = citedByData.map(item);
  const notes = [];
  if (!citedItems.length) notes.push("No source in the library cites it.");
  if (unread.length) {
    notes.push(
      `${plural(unread.length, "other source")} ${unread.length === 1 ? "has" : "have"} no extracted references, so it is not known whether ${unread.length === 1 ? "it cites" : "they cite"} this source: ${listText(unread.map((s) => s.title))}.`
    );
  }
  return { cites, citedBy: { items: citedItems, note: notes.join(" ") || null } };
}

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

function requireList(data, key) {
  if (!Array.isArray(data[key])) throw new Error(`the relations have no ${key} list`);
  return data[key];
}

/** One "Read first" item: the assumed concept, its standing, and the sources introducing it
 * (each with its newest study page that is not superseded, else its newest one, or none). */
export function readFirstItem(item) {
  const introducers = item.introduced_by.map((entry) => {
    const pages = entry.study_pages;
    if (entry.has_ready_page !== pages.length > 0) {
      throw new Error(`inconsistent study pages for ${entry.source.slug}`);
    }
    const page = pages.find((p) => !p.superseded) || pages[0] || null;
    return {
      slug: entry.source.slug,
      title: entry.source.title,
      href: sourceHref(entry.source.slug),
      page: page ? { slug: page.slug, title: page.title, href: pageHref(page.slug) } : null,
    };
  });
  return {
    slug: item.concept.slug,
    name: item.concept.name,
    href: conceptHref(item.concept.slug),
    status: item.status,
    statusText: statusLabel(item.status),
    introducers,
    note: introducers.length
      ? null
      : "Nothing in the library introduces it yet: add a source that explains it.",
  };
}

/**
 * The view of GET /api/sources/{slug}/related:
 * - `{state: "not_analysed", message, notAnalysedNote}` when the source has no concept links;
 * - otherwise `{state: "alone" | "related", readFirst, readinessNote, groups, message,
 *   notAnalysedNote}`: `readFirst` the "Read first" items (readFirstItem), `readinessNote` what
 *   to say when there are none, `groups` the non-empty "Builds on", "Built on by" and "Related"
 *   lists (`{key, label, items: [{slug, title, href, text}]}`), `message` the empty-state text
 *   when there is no other source to list ("alone"), and `notAnalysedNote` how many other sources
 *   were not compared (null when none).
 * A source listed under "Builds on" or "Built on by" is not repeated under "Related" (it always
 * shares a concept, so it is always related too); the Related group then says how many were left
 * out (`omitted`). Citations are separate facts, from the source's references, not its concepts:
 * `citations` (citationsView) is given in every state, and a cited or citing source is listed
 * there even when it also appears under a concept heading.
 */
export function relatedView(data) {
  if (!data || typeof data.analysed !== "boolean") throw new Error("the relations have no analysed flag");
  const others = requireList(data, "not_analysed");
  const notAnalysedNote = others.length
    ? `${plural(others.length, "other source")} ${others.length === 1 ? "is" : "are"} not analysed yet, so ${others.length === 1 ? "it was" : "they were"} not compared: ${listText(others.map((s) => s.title))}.`
    : null;
  if (!data.analysed) {
    for (const key of ["builds_on", "built_on_by", "related", "readiness"]) {
      if (data[key] !== null) throw new Error(`an unanalysed source with ${key}`);
    }
    return {
      state: "not_analysed",
      citations: citationsView(data),
      message: "Not analysed yet: generate a study page to extract its concepts. Its relations to other sources are worked out from them.",
      notAnalysedNote,
    };
  }
  const buildsOn = requireList(data, "builds_on");
  const builtOnBy = requireList(data, "built_on_by");
  const related = requireList(data, "related");
  if (!data.readiness || !Array.isArray(data.readiness.read_first)) {
    throw new Error("the relations of an analysed source have no readiness");
  }
  const { assumed, read_first: readFirstData } = data.readiness;
  const readFirst = readFirstData.map(readFirstItem);
  let readinessNote = null;
  if (!readFirst.length) {
    readinessNote = assumed
      ? `You are in good standing on all ${plural(assumed, "concept")} it assumes.`
      : "It assumes no background concepts.";
  }

  const edgeItem = (entry, text) => ({
    slug: entry.source.slug,
    title: entry.source.title,
    href: sourceHref(entry.source.slug),
    text,
  });
  const listed = new Set([...buildsOn, ...builtOnBy].map((e) => e.source.slug));
  const relatedShown = related.filter((e) => !listed.has(e.source.slug));
  const groups = [
    {
      key: "builds_on",
      label: "Builds on",
      hint: "It uses or assumes concepts these sources introduce.",
      items: buildsOn.map((e) => edgeItem(e, `Through ${conceptsText(e.concepts)}.`)),
    },
    {
      key: "built_on_by",
      label: "Built on by",
      hint: "These sources use or assume concepts it introduces.",
      items: builtOnBy.map((e) => edgeItem(e, `Through ${conceptsText(e.concepts)}.`)),
    },
    {
      key: "related",
      label: "Related",
      hint: "These sources share concepts with it, most overlap first.",
      items: relatedShown.map((e) =>
        edgeItem(e, `${weightText(e.weight)}: shares ${sharedText(e.shared)}.`)
      ),
      omitted: related.length - relatedShown.length,
    },
  ].filter((g) => g.items.length);
  const alone = !buildsOn.length && !builtOnBy.length && !related.length;
  return {
    state: alone ? "alone" : "related",
    citations: citationsView(data),
    readFirst,
    readinessNote,
    groups,
    message: alone ? "No other analysed source shares a concept with this one yet." : null,
    notAnalysedNote,
  };
}
