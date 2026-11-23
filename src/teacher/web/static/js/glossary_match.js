// Pure matching of glossary terms in a string (no DOM globals, so node can test it).
//
// The rules mirror teacher/glossary.py, which decides on the server which terms occur:
// - a form (the term or one of its aliases) matches as a whole word: it may not be preceded or
//   followed by a letter, digit, combining mark or "_" when the form itself starts or ends with
//   one, so "GAN" does not match inside "ORGAN" but does in "GAN-based" or "(GAN)";
// - whitespace inside a form matches any run of whitespace;
// - acronyms and symbols match case-sensitively, other terms case-insensitively.
// On top of that, for marking a page, where matches overlap the longest wins, and:
// - a shorthand form (see `isShorthand`: "SFT", "R1-Zero", "L2") is matched at every occurrence,
//   because shorthand is what a reader forgets and it is short enough not to clutter the page;
// - the other forms of a term ("supervised fine-tuning") are matched at most once per call
//   between them: the first occurrence not covered by a longer match.

const WORD_CHAR = /[\p{L}\p{N}\p{M}_]/u;
const CASE_SENSITIVE = new Set(["acronym", "symbol"]);
// Characters with a meaning in a regular expression; in a `u` regex only these may be escaped.
const SYNTAX = /[\^$\\.*+?()[\]{}|/]/g;

export function isWordChar(char) {
  return Boolean(char) && WORD_CHAR.test(char);
}

function escapeRegExp(text) {
  return text.replace(SYNTAX, "\\$&");
}

/** The code point ending just before `index` (a surrogate pair counts as one character). */
function charBefore(text, index) {
  if (index <= 0) return "";
  const code = text.charCodeAt(index - 1);
  if (code >= 0xdc00 && code <= 0xdfff && index >= 2) return text.slice(index - 2, index);
  return text[index - 1];
}

/** The code point starting at `index`. */
function charAt(text, index) {
  if (index >= text.length) return "";
  return String.fromCodePoint(text.codePointAt(index));
}

/**
 * Whether `form` (whitespace already normalised) is shorthand: a single token that is the form
 * of an acronym or symbol, or that has two or more capital letters or a digit ("SFT", "GANs",
 * "R1-Zero", "word2vec"). "KL divergence" and "policy" are not.
 */
export function isShorthand(form, kind) {
  if (!form || form.includes(" ")) return false;
  if (CASE_SENSITIVE.has(kind)) return true;
  return (form.match(/\p{Lu}/gu) || []).length >= 2 || /\p{N}/u.test(form);
}

export function normaliseSpace(text) {
  return String(text ?? "").split(/\s+/u).filter(Boolean).join(" ");
}

/**
 * Prepare `terms` (the API's `[{term, kind, aliases, ...}]`) for `findMatches`. Each entry keeps
 * the original term object as `entry` and is identified by `key` (the term's text, unique in a
 * glossary). Blank and repeated forms are skipped.
 */
export function compileTerms(terms) {
  const compiled = [];
  for (const entry of terms || []) {
    if (!entry || typeof entry.term !== "string") continue;
    const caseSensitive = CASE_SENSITIVE.has(entry.kind);
    const seen = new Set();
    const forms = [];
    for (const raw of [entry.term, ...(Array.isArray(entry.aliases) ? entry.aliases : [])]) {
      const form = normaliseSpace(raw);
      const id = caseSensitive ? form : form.toLowerCase();
      if (!form || seen.has(id)) continue;
      seen.add(id);
      const body = form.split(" ").map(escapeRegExp).join("\\s+");
      forms.push({
        form,
        every: isShorthand(form, entry.kind),
        regex: new RegExp(body, caseSensitive ? "gu" : "giu"),
        checkStart: isWordChar(charAt(form, 0)),
        checkEnd: isWordChar(charBefore(form, form.length)),
      });
    }
    if (forms.length) compiled.push({ key: entry.term, entry, forms });
  }
  return compiled;
}

/** Every whole-word occurrence of one compiled form in `text`, as {start, end}. */
function occurrences(text, form) {
  const found = [];
  const regex = form.regex;
  regex.lastIndex = 0;
  for (;;) {
    const match = regex.exec(text);
    if (!match) break;
    const start = match.index;
    const end = start + match[0].length;
    const before = form.checkStart && isWordChar(charBefore(text, start));
    const after = form.checkEnd && isWordChar(charAt(text, end));
    if (!before && !after) found.push({ start, end });
    // Step one character on (not past the match), so an overlapping occurrence is found too.
    regex.lastIndex = start + Math.max(1, charAt(text, start).length);
  }
  return found;
}

/**
 * The glossary matches in `text`: `[{start, end, key, every}]` in text order, non-overlapping.
 * `every` is true for a shorthand form (all of its occurrences are matched) and false for the
 * one match a term's other forms get; a key in `seen` (terms whose long form is already marked
 * earlier in the same block) gets no such match, only its shorthand ones.
 *
 * The longest candidate is chosen first (ties: the earlier one), where the candidates are every
 * shorthand occurrence and, per term, its earliest other occurrence not covered by a match
 * already chosen. So "conditional GAN" wins over "GAN" where they overlap, while "GAN" is still
 * marked everywhere else.
 */
export function findMatches(text, compiled, seen = new Set()) {
  const value = String(text ?? "");
  const byStart = (a, b) => a.start - b.start || b.end - b.start - (a.end - a.start);
  const pending = new Map(); // key -> occurrences of its non-shorthand forms, in text order
  let repeating = []; // every occurrence of every shorthand form
  for (const item of compiled || []) {
    const once = [];
    for (const form of item.forms) {
      if (!form.every && seen.has(item.key)) continue;
      for (const range of occurrences(value, form)) {
        (form.every ? repeating : once).push({ ...range, key: item.key, every: form.every });
      }
    }
    if (once.length) pending.set(item.key, once.sort(byStart));
  }
  const chosen = [];
  const covered = (range) => chosen.some((c) => range.start < c.end && c.start < range.end);
  const better = (a, b) => {
    if (!b) return true;
    const la = a.end - a.start;
    const lb = b.end - b.start;
    return la > lb || (la === lb && a.start < b.start);
  };
  for (;;) {
    let best = null;
    for (const list of pending.values()) {
      while (list.length && covered(list[0])) list.shift();
      if (list.length && better(list[0], best)) best = list[0];
    }
    repeating = repeating.filter((range) => !covered(range));
    for (const range of repeating) if (better(range, best)) best = range;
    if (!best) break;
    chosen.push(best);
    if (!best.every) pending.delete(best.key);
  }
  return chosen.sort((a, b) => a.start - b.start);
}
