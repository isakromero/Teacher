// Pure helpers for the study page's highlights (highlights.js): the text model a highlight is
// anchored in, making an anchor from a selection, finding it again, and checking what the API
// sends. No DOM globals, so node can test it (tests/web/js/highlight_test.mjs).
//
// THE TEXT MODEL. A block's text is a list of pieces in document order:
//   {kind: "text", text}   a text node's value
//   {kind: "atom", text}   an element taken as one unit: rendered maths, whose text is its TeX
//                          source ($...$ inline, $$...$$ display); a highlight covers all of it
//                          or none of it, and is never cut inside it
//   {kind: "gap", text: " "}  a boundary between block-level elements (paragraphs, list items)
// (highlights.js adds `ref`, the node or element, to text and atom pieces). The RAW text is the
// pieces' texts joined; the MODEL text is the raw text with every run of whitespace made one
// space and no whitespace at either end. Offsets (`start`, `end`) count UTF-16 code units of
// the model text. Only the model text is stored or compared, so the same block rendered again
// (with other glossary marks, other highlight marks, or another layout) gives the same text.
//
// AN ANCHOR is {block_key, quote, prefix, suffix, start, end}: the quote is model text, the
// prefix and suffix up to CONTEXT_CHARS of model text around it. To find it again
// (`locate`): every occurrence of the quote in the block's model text is a candidate; the one
// whose surroundings agree best with the prefix and suffix wins, then the one nearest the
// stored start. No occurrence: the highlight is "not found on the page" (the text changed).

export const CONTEXT_CHARS = 32;
export const MAX_QUOTE_CHARS = 4000; // the API's limits
export const MAX_NOTE_CHARS = 10000;

const WHITESPACE = /\s/;

function isHigh(code) {
  return code >= 0xd800 && code <= 0xdbff;
}
function isLow(code) {
  return code >= 0xdc00 && code <= 0xdfff;
}

/** `index` moved off the middle of a surrogate pair: towards the start of the string when
 * `towardsStart`, else past the pair. */
export function noSplit(text, index, towardsStart) {
  if (index <= 0 || index >= text.length) return Math.max(0, Math.min(index, text.length));
  if (isLow(text.charCodeAt(index)) && isHigh(text.charCodeAt(index - 1))) {
    return towardsStart ? index - 1 : index + 1;
  }
  return index;
}

/**
 * The model of a block from its pieces: `{text, raw, starts, map}`. `raw` is the joined raw
 * text, `starts[i]` where piece i starts in it, `map[k]` the raw index of model character k
 * (increasing).
 */
export function buildModel(pieces) {
  const starts = [];
  let raw = "";
  for (const piece of pieces) {
    if (!piece || typeof piece.text !== "string") throw new Error("a text piece needs its text");
    starts.push(raw.length);
    raw += piece.text;
  }
  let text = "";
  const map = [];
  let pendingSpace = -1; // raw index of the first whitespace of a run not yet written
  for (let r = 0; r < raw.length; r += 1) {
    const char = raw[r];
    if (WHITESPACE.test(char)) {
      if (pendingSpace < 0) pendingSpace = r;
      continue;
    }
    if (pendingSpace >= 0 && text.length) {
      text += " ";
      map.push(pendingSpace);
    }
    pendingSpace = -1;
    text += char;
    map.push(r);
  }
  return { pieces, text, raw, starts, map };
}

/** The model offset of raw offset `raw`: how many model characters come before it. */
export function rawToModel(model, raw) {
  const { map } = model;
  let lo = 0;
  let hi = map.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (map[mid] < raw) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** The raw range [start, end) of the model range [start, end) (end > start). */
export function modelToRaw(model, start, end) {
  if (!(start >= 0 && end > start && end <= model.map.length)) {
    throw new Error(`no such range in the block's text: ${start}..${end}`);
  }
  return { start: model.map[start], end: model.map[end - 1] + 1 };
}

/**
 * The pieces a raw range covers: `[{index, from, to}]` in order, `from`/`to` offsets in the
 * piece's text. Gaps are left out; an atom touched at all is covered whole.
 */
export function coveredPieces(model, rawStart, rawEnd) {
  const out = [];
  model.pieces.forEach((piece, index) => {
    const start = model.starts[index];
    const end = start + piece.text.length;
    if (piece.kind === "gap" || end <= rawStart || start >= rawEnd || start === end) return;
    if (piece.kind === "atom") {
      out.push({ index, from: 0, to: piece.text.length });
      return;
    }
    out.push({ index, from: Math.max(rawStart, start) - start, to: Math.min(rawEnd, end) - start });
  });
  return out;
}

/** A raw range widened so that it does not end or start inside an atom. */
export function widenToAtoms(model, rawStart, rawEnd) {
  let start = rawStart;
  let end = rawEnd;
  model.pieces.forEach((piece, index) => {
    if (piece.kind !== "atom") return;
    const a = model.starts[index];
    const b = a + piece.text.length;
    if (a < start && start < b) start = a;
    if (a < end && end < b) end = b;
  });
  return { start, end };
}

/**
 * The anchor of the model range [start, end) of `model` (whitespace at its ends dropped), or
 * null when it holds no text. `{quote, prefix, suffix, start, end}`; the caller adds block_key
 * and checks the quote's length.
 */
export function makeAnchor(model, start, end) {
  const text = model.text;
  let s = Math.max(0, Math.min(start, text.length));
  let e = Math.max(s, Math.min(end, text.length));
  while (s < e && WHITESPACE.test(text[s])) s += 1;
  while (e > s && WHITESPACE.test(text[e - 1])) e -= 1;
  s = noSplit(text, s, true);
  e = noSplit(text, e, false);
  if (s >= e) return null;
  const before = noSplit(text, Math.max(0, s - CONTEXT_CHARS), false);
  const after = noSplit(text, Math.min(text.length, e + CONTEXT_CHARS), true);
  return {
    quote: text.slice(s, e),
    prefix: text.slice(before, s),
    suffix: text.slice(e, after),
    start: s,
    end: e,
  };
}

function commonSuffix(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n += 1;
  return n;
}

function commonPrefix(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[n] === b[n]) n += 1;
  return n;
}

/**
 * Where `anchor` is in the model text `text` now: `{start, end}`, or null when the quote does
 * not occur in it (see the module comment for how one of several occurrences is chosen).
 */
export function locate(text, anchor) {
  const quote = anchor && typeof anchor.quote === "string" ? anchor.quote : "";
  if (!quote) return null;
  const prefix = anchor.prefix || "";
  const suffix = anchor.suffix || "";
  let best = null;
  for (let at = text.indexOf(quote); at !== -1; at = text.indexOf(quote, at + 1)) {
    const end = at + quote.length;
    const score =
      commonSuffix(text.slice(Math.max(0, at - prefix.length), at), prefix) +
      commonPrefix(text.slice(end, end + suffix.length), suffix);
    const distance = Math.abs(at - (Number.isInteger(anchor.start) ? anchor.start : 0));
    if (!best || score > best.score || (score === best.score && distance < best.distance)) {
      best = { start: at, end, score, distance };
    }
  }
  return best ? { start: best.start, end: best.end } : null;
}

/** The note to send for what the reader typed: null for a blank one. */
export function noteValue(text) {
  const value = String(text ?? "").trim();
  return value ? value : null;
}

const FIELDS = {
  id: "string",
  artifact: "string",
  block_key: "string",
  quote: "string",
  prefix: "string",
  suffix: "string",
  start: "number",
  end: "number",
  created_at: "string",
  updated_at: "string",
};

/** `item` if it is a highlight as the API sends it; an Error saying what is wrong otherwise. */
export function checkHighlight(item) {
  if (!item || typeof item !== "object") throw new Error("a highlight from the server is not an object");
  for (const [key, type] of Object.entries(FIELDS)) {
    if (typeof item[key] !== type) {
      throw new Error(`a highlight from the server has no ${key} (${type})`);
    }
  }
  if (!(item.note === null || typeof item.note === "string")) {
    throw new Error("a highlight from the server has a note that is not text");
  }
  if (!(item.block_position === null || Number.isInteger(item.block_position))) {
    throw new Error("a highlight from the server has a bad block_position");
  }
  return item;
}

/** The list GET /api/artifacts/{slug}/highlights sends, checked. */
export function checkList(data, slug) {
  if (!data || !Array.isArray(data.highlights)) {
    throw new Error("Unexpected response from the server: the highlights list is missing.");
  }
  if (data.artifact !== slug) {
    throw new Error(`Unexpected response from the server: highlights of “${data.artifact}”, not “${slug}”.`);
  }
  return data.highlights.map(checkHighlight);
}

/**
 * Of the highlights covering one place (`ids`), the one a tap means: the shortest quote (the
 * innermost when they nest), then the newest.
 */
export function pickHighlight(ids, byId) {
  let best = null;
  for (const id of ids) {
    const item = byId.get(id);
    if (!item) continue;
    if (
      !best ||
      item.quote.length < best.quote.length ||
      (item.quote.length === best.quote.length && item.created_at > best.created_at)
    ) {
      best = item;
    }
  }
  return best;
}

/** Highlights in page order: by block position (unknown blocks last), start, creation. */
export function inPageOrder(list) {
  const position = (h) => (Number.isInteger(h.block_position) ? h.block_position : Infinity);
  return [...list].sort(
    (a, b) =>
      position(a) - position(b) ||
      a.start - b.start ||
      (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0) ||
      (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
  );
}
