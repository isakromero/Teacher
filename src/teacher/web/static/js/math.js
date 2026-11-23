// Protect maths spans from the markdown parser.
//
// marked treats `_`, `*`, `\` and `|` as markdown syntax, which would mangle LaTeX such as
// `$x_i^{*} = \frac{a}{b}$`. protectMath() swaps every maths span for an opaque placeholder
// (private-use characters around an index, which markdown leaves alone) before parsing; the
// caller puts the rendered maths back in afterwards. Code spans and fenced code blocks are left
// untouched, so a `$` inside code is never treated as maths.
//
// Delimiters: $$...$$ and \[...\] (display), $...$ and \(...\) (inline). An inline `$` opens
// only when followed by a non-space, and the next `$` must be preceded by a non-space and not
// followed by a digit, otherwise the opener is plain text: "costs $5 and $10" stays text.
// `\$` is a literal dollar. Inline maths cannot span a blank line or a backtick.
//
// No DOM access here, so this module also runs under node for tests.

export const PH_OPEN = "";
export const PH_CLOSE = "";
export const PLACEHOLDER_SPLIT = /(\d+)/;

const FENCE_OPEN = /^ {0,3}(`{3,}|~{3,})/;
const BLANK_LINE = /^\n[ \t]*\n/;

function placeholder(index) {
  return `${PH_OPEN}${index}${PH_CLOSE}`;
}

/** Index just past the fenced code block opening at `start`, or -1 if none opens there. */
function skipFence(src, start) {
  const open = FENCE_OPEN.exec(src.slice(start, start + 64));
  if (!open) return -1;
  const marker = open[1];
  const lineEnd = src.indexOf("\n", start);
  if (lineEnd === -1) return src.length;
  const close = new RegExp(`^ {0,3}${marker[0]}{${marker.length},}[ \\t]*$`, "m");
  const rest = src.slice(lineEnd + 1);
  const match = close.exec(rest);
  return match ? lineEnd + 1 + match.index + match[0].length : src.length;
}

/** Index just past the code span opening at `start` (a run of backticks), or -1. */
function skipCodeSpan(src, start) {
  let run = 0;
  while (src[start + run] === "`") run += 1;
  let j = start + run;
  while (j < src.length) {
    const k = src.indexOf("`", j);
    if (k === -1) return -1;
    let len = 0;
    while (src[k + len] === "`") len += 1;
    if (len === run) return k + len;
    j = k + len;
  }
  return -1;
}

function findInlineClose(src, start) {
  if (start >= src.length || /\s/.test(src[start]) || src[start] === "$") return -1;
  for (let j = start; j < src.length; j += 1) {
    const c = src[j];
    if (c === "\\") {
      j += 1; // an escaped character (e.g. \$ or \{) never closes
      continue;
    }
    if (c === "`") return -1; // never reach into a code span
    if (c === "\n" && BLANK_LINE.test(src.slice(j, j + 32))) return -1;
    if (c === "$") {
      // The next `$` must close; if it cannot ("$5 and $10"), this `$` was not maths.
      if (/\s/.test(src[j - 1]) || /[0-9]/.test(src[j + 1] || "")) return -1;
      return j;
    }
  }
  return -1;
}

/**
 * Replace the maths spans in markdown `src` with placeholders.
 * Returns `{ text, maths }` where `maths[i] = { tex, display }` belongs to placeholder i.
 */
export function protectMath(src) {
  const maths = [];
  const put = (tex, display) => {
    maths.push({ tex, display });
    return placeholder(maths.length - 1);
  };
  let out = "";
  let i = 0;
  const n = src.length;
  while (i < n) {
    if (i === 0 || src[i - 1] === "\n") {
      const end = skipFence(src, i);
      if (end !== -1) {
        out += src.slice(i, end);
        i = end;
        continue;
      }
    }
    const c = src[i];
    if (c === "\\") {
      const next = src[i + 1];
      if (next === "(" || next === "[") {
        const close = next === "(" ? "\\)" : "\\]";
        const end = src.indexOf(close, i + 2);
        if (end !== -1) {
          out += put(src.slice(i + 2, end), next === "[");
          i = end + 2;
          continue;
        }
      }
      out += src.slice(i, i + 2); // keep other escapes (\$, \*, ...) for marked
      i += 2;
      continue;
    }
    if (c === "`") {
      const end = skipCodeSpan(src, i);
      if (end !== -1) {
        out += src.slice(i, end);
        i = end;
        continue;
      }
      let run = 0;
      while (src[i + run] === "`") run += 1;
      out += src.slice(i, i + run);
      i += run;
      continue;
    }
    if (c === "$") {
      if (src[i + 1] === "$") {
        const end = src.indexOf("$$", i + 2);
        if (end !== -1 && src.slice(i + 2, end).trim()) {
          out += put(src.slice(i + 2, end), true);
          i = end + 2;
          continue;
        }
        out += "$$";
        i += 2;
        continue;
      }
      const end = findInlineClose(src, i + 1);
      if (end !== -1) {
        out += put(src.slice(i + 1, end), false);
        i = end + 1;
        continue;
      }
    }
    out += c;
    i += 1;
  }
  return { text: out, maths };
}

/** Put the original delimited TeX source back (used where maths cannot be rendered, e.g. attributes). */
export function restoreSource(text, maths) {
  return text.replace(new RegExp(PLACEHOLDER_SPLIT.source, "g"), (whole, index) => {
    const item = maths[Number(index)];
    if (!item) return whole;
    return item.display ? `$$${item.tex}$$` : `$${item.tex}$`;
  });
}
