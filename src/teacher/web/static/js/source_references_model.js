// The citation views' logic that needs no DOM, so that tests/web/js/source_references_test.mjs
// can run it under node: what the source page says about the state of a source's reference
// extraction (never run, running, interrupted, done, failed) and which button it offers, how one
// reference and the "cited by" list read, the add page's "Cited by your library" list from the
// reading frontier (with its empty states), and the address an /add?url=... link prefills.
//
// Fail loudly: an unknown extraction status or a missing field is an Error, never a quiet guess,
// and a failed or never-run extraction is never described as "no references".

export const POLL_MS = 3000;
export const FRONTIER_SHOWN = 8;

const STATUSES = ["never_run", "running", "interrupted", "done", "failed"];

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

/** The calendar day of an ISO timestamp ("2026-11-22"); "" for none. */
export function isoDay(iso) {
  if (!iso) return "";
  const day = String(iso).slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) throw new Error(`Not a timestamp: ${iso}`);
  return day;
}

/** "/add?url=..." for an address to add. */
export function addPageUrl(url) {
  if (!url) throw new Error("An address is needed to add a work.");
  return `/add?${new URLSearchParams({ url })}`;
}

/** The address an /add page link carries: {url}, {error} for one that is not http(s), or null. */
export function prefilledUrl(search) {
  const value = new URLSearchParams(search || "").get("url");
  if (value === null) return null;
  const url = value.trim();
  if (!/^https?:\/\/\S+$/i.test(url)) {
    return { error: `The link asked to add “${url || "(nothing)"}”, which is not a web address that starts with http:// or https://.` };
  }
  return { url };
}

function storedLine(extraction, matched) {
  const count = extraction.reference_count;
  const day = isoDay(extraction.succeeded_at);
  if (extraction.found_in === "none") {
    return `No cited works were found in this source (extracted ${day}).`;
  }
  const what =
    extraction.found_in === "reference_list"
      ? `${plural(count, "reference")} from its reference list`
      : `${plural(count, "work")} cited in its text (it has no reference list)`;
  return `${what}, extracted ${day}; ${matched} in your library.`;
}

/**
 * What the source page shows for `extraction` (the API's `extraction` object) when `matched`
 * of the stored references are in the library: `tone` (busy | ok | error | idle), `text`,
 * `button` (its label, or null for none), `busy` (poll until it finishes).
 */
export function extractionView(extraction, matched = 0) {
  if (!extraction || !STATUSES.includes(extraction.status)) {
    throw new Error(`Unknown reference extraction status: ${extraction && extraction.status}`);
  }
  const unavailable = !extraction.available;
  const offer = (label) => (unavailable ? null : label);
  const why = unavailable ? ` ${extraction.unavailable_reason || "This server cannot extract references."}` : "";
  const earlier = extraction.succeeded_at ? ` Showing the earlier result: ${storedLine(extraction, matched)}` : " No references are stored for this source.";
  switch (extraction.status) {
    case "never_run":
      return { tone: "idle", text: `The references of this source have not been extracted yet.${why}`, button: offer("Extract references"), busy: false };
    case "running":
      return { tone: "busy", text: `Extracting the references (started ${isoDay(extraction.started_at)})...`, button: null, busy: true };
    case "interrupted":
      return {
        tone: "error",
        text: `The last extraction never finished (the server stopped while it ran).${earlier}${why}`,
        button: offer("Try again"),
        busy: false,
      };
    case "failed":
      return {
        tone: "error",
        text: `The last extraction failed on ${isoDay(extraction.finished_at)}: ${extraction.error}${earlier}${why}`,
        button: offer("Try again"),
        busy: false,
      };
    default: // done
      if (extraction.reference_count === null || extraction.reference_count === undefined) {
        throw new Error("A finished extraction has no reference count.");
      }
      return { tone: "ok", text: storedLine(extraction, matched), button: offer("Extract again"), busy: false };
  }
}

/** "A. Vaswani, N. Shazeer, N. Parmar et al." from a list of names ("" for none). */
export function authorsText(authors, shown = 3) {
  const names = (authors || []).filter(Boolean);
  if (names.length <= shown) return names.join(", ");
  return `${names.slice(0, shown).join(", ")} et al.`;
}

const ADD_KINDS = {
  arxiv: "the arXiv PDF",
  doi: "its DOI link",
  url: "the address in the reference",
};

const MATCH_METHODS = { doi: "by DOI", arxiv: "by arXiv id", title: "by title" };

/**
 * How one reference reads: `heading` (its title, or the reference as printed), `details`
 * (authors · year · venue), `raw` (the printed text, shown under a title), and either `match`
 * ({href, title, how}) or `add` ({href, how}) or neither.
 */
export function referenceView(ref) {
  const details = [authorsText(ref.authors), ref.year, ref.venue].filter((part) => part !== null && part !== undefined && part !== "").join(" · ");
  let match = null;
  let add = null;
  if (ref.match) {
    const how = MATCH_METHODS[ref.match.method];
    if (!how) throw new Error(`Unknown match method: ${ref.match.method}`);
    match = { href: `/sources/${encodeURIComponent(ref.match.slug)}`, title: ref.match.title, how };
  } else if (ref.add_url) {
    const how = ADD_KINDS[ref.add_url_kind];
    if (!how) throw new Error(`Unknown kind of address: ${ref.add_url_kind}`);
    add = { href: addPageUrl(ref.add_url), how };
  }
  return {
    position: ref.position,
    heading: ref.title || ref.raw_text,
    details,
    raw: ref.title ? ref.raw_text : null,
    match,
    add,
  };
}

/** How many references matched a library source. */
export function matchedCount(references) {
  return (references || []).filter((ref) => ref.match).length;
}

/** The "cited by" list: {empty: message | null, items: [{href, title, note}]}. */
export function citedByView(citedBy) {
  const items = (citedBy || []).map((source) => ({
    href: `/sources/${encodeURIComponent(source.slug)}`,
    title: source.title,
    note: source.positions.length > 1 ? `${source.positions.length} of its references` : null,
  }));
  if (items.length) return { empty: null, items };
  return { empty: "No source in your library cites this one, as far as their extracted references show.", items };
}

/**
 * The add page's "Cited by your library" list from `GET /api/references/frontier`:
 * {empty: message | null, items: [{title, details, cited, how, url}], note: string | null}.
 * Only works with an address can be added in one click; how many others there are is said.
 */
export function frontierView(frontier, shown = FRONTIER_SHOWN) {
  const coverage = frontier && frontier.coverage;
  if (!coverage || !Array.isArray(frontier.items)) throw new Error("The reading frontier reply is incomplete.");
  const extracted = coverage.done + coverage.failed + coverage.interrupted; // may hold stored references
  // Sources whose references are missing or may be out of date (a failed or interrupted run
  // keeps the references of an earlier one, if there was one).
  const pending = [
    coverage.never_run && `${plural(coverage.never_run, "source")} not extracted yet`,
    coverage.running && `${coverage.running} being extracted now`,
    coverage.failed && `${coverage.failed} whose last extraction failed`,
    coverage.interrupted && `${coverage.interrupted} whose last extraction was interrupted`,
  ].filter(Boolean);
  const pendingNote = pending.length ? ` Incomplete: ${pending.join(", ")}.` : "";
  if (!coverage.sources) {
    return { empty: "Your library is empty, so nothing is cited yet.", items: [], note: null };
  }
  if (!frontier.total) {
    const empty = extracted || coverage.running
      ? `Everything your library cites is already in it, as far as the references extracted so far show.${pendingNote}`
      : `The references of your sources have not been extracted yet, so Teacher does not know what they cite.${pendingNote}`;
    return { empty, items: [], note: null };
  }
  const addable = frontier.items.filter((item) => item.add_url);
  const items = addable.slice(0, shown).map((item) => {
    const how = ADD_KINDS[item.add_url_kind];
    if (!how) throw new Error(`Unknown kind of address: ${item.add_url_kind}`);
    return {
      title: item.title || item.raw_text,
      details: [authorsText(item.authors), item.year].filter(Boolean).join(" · "),
      cited: `Cited by ${plural(item.citing_count, "source")}: ${item.cited_by.map((s) => s.title).join("; ")}`,
      how,
      url: item.add_url,
    };
  });
  const withoutAddress = frontier.items.length - addable.length; // among the items fetched
  const notes = [];
  if (frontier.items.length < frontier.total) notes.push(`Showing the most cited of ${plural(frontier.total, "work")}.`);
  if (withoutAddress > 0) notes.push(`${plural(withoutAddress, "cited work")} with no known address ${withoutAddress === 1 ? "is" : "are"} left out.`);
  if (pendingNote) notes.push(pendingNote.trim());
  if (!items.length) {
    return { empty: `Your library cites ${plural(frontier.total, "work")} it does not hold, but none of them has an address Teacher knows.${pendingNote}`, items, note: null };
  }
  return { empty: null, items, note: notes.join(" ") || null };
}
