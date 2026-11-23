// Pure computations for the `chart` block: contract checking, ticks, number formats, domains,
// category order, bar geometry and label fitting. No DOM, so node tests can import it
// (tests/web/js/chart_test.mjs); blocks/chart.js does the drawing.

export const KINDS = ["line", "bar", "scatter"];
export const MAX_SERIES = { line: 6, bar: 6, scatter: 3 };
export const MAX_POINTS = 60;
export const MAX_CATEGORIES = 24;
export const MAX_BAR = 24; // px: bars are capped, never fill their band
export const BAR_GAP = 2; // px of surface between neighbouring bars in a group
export const MIN_BAR = 3; // px: below this the plot grows wider (and scrolls) instead
export const MIN_LABEL_BAND = 16; // px: room for one category label rotated -90 degrees
export const BAR_RADIUS = 4;
export const MINUS = "−";

function fail(message) {
  throw new Error(`chart: ${message}`);
}

function describe(value) {
  if (typeof value === "number") return String(value);
  if (value === undefined) return "nothing";
  return JSON.stringify(value);
}

// -- contract -------------------------------------------------------------------------------

/**
 * Check `block.content` against the chart contract and return the model the renderer draws:
 * `{ kind, title, xLabel, yLabel, caption, source, series: [{ name, points }], categories }`.
 * Line points come back sorted by x; `categories` (bar only) is the first-appearance order.
 * Throws an Error naming the first violation; nothing is coerced or skipped.
 */
export function checkChartContent(content) {
  if (!content || typeof content !== "object" || Array.isArray(content)) {
    fail("content must be an object");
  }
  const { kind } = content;
  if (!KINDS.includes(kind)) fail(`unknown kind ${describe(kind)} (expected line, bar or scatter)`);
  for (const key of ["title", "x_label", "y_label", "caption", "source"]) {
    if (typeof content[key] !== "string") fail(`${key} must be a string, got ${describe(content[key])}`);
  }
  const series = content.series;
  const limit = MAX_SERIES[kind];
  if (!Array.isArray(series)) fail(`series must be a list, got ${describe(series)}`);
  if (series.length < 1 || series.length > limit) {
    fail(`a ${kind} chart takes 1 to ${limit} series, got ${series.length}`);
  }
  const names = new Set();
  const checked = series.map((s, i) => {
    if (!s || typeof s !== "object") fail(`series ${i + 1} must be an object`);
    if (typeof s.name !== "string" || !s.name.trim()) fail(`series ${i + 1} needs a non-blank name`);
    if (names.has(s.name)) fail(`series name ${describe(s.name)} is used twice`);
    names.add(s.name);
    const label = `series ${describe(s.name)}`;
    if (!Array.isArray(s.points)) fail(`${label}: points must be a list`);
    if (s.points.length < 1 || s.points.length > MAX_POINTS) {
      fail(`${label}: takes 1 to ${MAX_POINTS} points, got ${s.points.length}`);
    }
    if (kind === "line" && s.points.length < 2) fail(`${label}: a line needs at least 2 points`);
    const seen = new Set();
    const points = s.points.map((p, j) => {
      const at = `${label} point ${j + 1}`;
      if (!p || typeof p !== "object") fail(`${at} must be an object`);
      if (typeof p.y !== "number" || !Number.isFinite(p.y)) {
        fail(`${at}: y must be a finite number, got ${describe(p.y)}`);
      }
      if (kind === "bar") {
        if (typeof p.x !== "string" || !p.x.trim()) {
          fail(`${at}: a bar chart's x must be a non-blank category name, got ${describe(p.x)}`);
        }
      } else if (typeof p.x !== "number" || !Number.isFinite(p.x)) {
        fail(`${at}: a ${kind} chart's x must be a finite number, got ${describe(p.x)}`);
      }
      if (kind !== "scatter") {
        if (seen.has(p.x)) fail(`${label}: x ${describe(p.x)} appears twice`);
        seen.add(p.x);
      }
      return { x: p.x, y: p.y };
    });
    if (kind === "line") points.sort((a, b) => a.x - b.x);
    return { name: s.name, points };
  });
  let categories = null;
  if (kind === "bar") {
    categories = categoryOrder(checked);
    if (categories.length > MAX_CATEGORIES) {
      fail(`a bar chart takes at most ${MAX_CATEGORIES} categories, got ${categories.length}`);
    }
  }
  return {
    kind,
    title: content.title,
    xLabel: content.x_label,
    yLabel: content.y_label,
    caption: content.caption,
    source: content.source,
    series: checked,
    categories,
  };
}

/** Category order for a bar chart: first appearance, walking the series in order. */
export function categoryOrder(series) {
  const seen = new Set();
  for (const s of series) for (const p of s.points) seen.add(p.x);
  return [...seen];
}

// -- ticks and domains ----------------------------------------------------------------------

/** Number of decimals needed to write `step` exactly (0.25 -> 2, 5 -> 0, 2e-7 -> 7). */
export function stepDecimals(step) {
  if (!(step > 0) || !Number.isFinite(step)) fail(`a tick step must be positive, got ${step}`);
  for (let d = 0; d <= 20; d++) {
    const scaled = step * 10 ** d;
    if (Math.round(scaled) >= 1 && Math.abs(scaled - Math.round(scaled)) <= 1e-6 * scaled) return d;
  }
  return 20;
}

/**
 * Choose a 1, 2 or 5 x 10^e step for [min, max] giving about `target` ticks. `inside`: ticks must
 * lie within the range (a fixed domain); otherwise the range extends to the outer ticks, and
 * the empty space that adds counts against a step.
 */
function chooseStep(min, max, target, inside) {
  const span = max - min;
  const base = Math.floor(Math.log10(span / Math.max(1, target - 1)));
  let best = null;
  for (let e = base - 1; e <= base + 1; e++) {
    for (const m of [1, 2, 5]) {
      const step = Number(`${m}e${e}`);
      const lo = inside ? Math.ceil(min / step - 1e-9) : Math.floor(min / step + 1e-9);
      const hi = inside ? Math.floor(max / step + 1e-9) : Math.ceil(max / step - 1e-9);
      const count = hi - lo + 1;
      if (count < (inside ? 1 : 2)) continue;
      const waste = inside ? 0 : 1 - span / ((hi - lo) * step);
      const score = Math.abs(count - target) + 5 * waste;
      const better = !best || score < best.score - 1e-9 || (Math.abs(score - best.score) <= 1e-9 && step > best.step);
      if (better) best = { score, step, lo, hi, m, e };
    }
  }
  if (!best) fail(`no ticks fit ${min}..${max}`);
  return best;
}

function tickValues(lo, hi, { m, e }) {
  const ticks = [];
  for (let k = lo; k <= hi; k++) {
    const value = Number(`${k * m}e${e}`); // exact decimal, no 0.30000000000000004
    ticks.push(value === 0 ? 0 : value); // no -0
  }
  return ticks;
}

function checkRange(min, max, target) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) fail(`ticks need finite bounds, got ${min}..${max}`);
  if (min > max) fail(`tick range is reversed: ${min}..${max}`);
  if (!(target >= 2)) fail(`tick target must be at least 2, got ${target}`);
}

/**
 * Clean ticks covering [min, max]: steps of 1, 2 or 5 x 10^n, about `target` of them.
 * Returns `{ min, max, step, ticks }` where min/max are the outer ticks (the extended domain).
 * A zero-width range is widened around its value (0 -> -1..1, v -> v +/- 10%).
 */
export function niceTicks(min, max, target = 5) {
  checkRange(min, max, target);
  if (min === max) {
    const pad = min === 0 ? 1 : Math.abs(min) * 0.1;
    min -= pad;
    max += pad;
  }
  const st = chooseStep(min, max, target, false);
  const ticks = tickValues(st.lo, st.hi, st);
  return { min: ticks[0], max: ticks[ticks.length - 1], step: st.step, ticks };
}

/** Clean ticks inside a fixed domain (a line chart's x axis spans the data exactly). */
export function ticksWithin(min, max, target = 5) {
  checkRange(min, max, target);
  if (min === max) fail(`a fixed domain needs width, got ${min}..${max}`);
  const st = chooseStep(min, max, target, true);
  return { min, max, step: st.step, ticks: tickValues(st.lo, st.hi, st) };
}

/**
 * Pick the most ticks (up to `target`) whose labels fit side by side along `length` px.
 * `make(count)` returns a tick set (`niceTicks`-shaped), `label(tickSet)` its label strings,
 * `measure(text)` a label's width in px.
 */
export function fitTicks({ make, label, measure, length, target, gap = 8 }) {
  let chosen = null;
  for (let count = target; count >= 2; count--) {
    const set = make(count);
    chosen = set;
    const span = set.max - set.min;
    if (set.ticks.length < 2 || span <= 0) return set;
    const spacing = (set.step / span) * length;
    const widest = Math.max(...label(set).map(measure));
    if (widest + gap <= spacing) return set;
  }
  return chosen;
}

/** y domain input: bars always include zero; line/scatter take the data's own extent. */
export function valueExtent(kind, values) {
  if (!values.length) fail("no values to scale");
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (kind === "bar") {
    lo = Math.min(lo, 0);
    hi = Math.max(hi, 0);
    if (lo === hi) hi = 1; // all zero: draw an empty 0..1 axis
  }
  return [lo, hi];
}

/** Linear map from [d0, d1] to [r0, r1]. */
export function linearScale(d0, d1, r0, r1) {
  if (!(d1 !== d0)) fail(`cannot scale a zero-width domain ${d0}..${d1}`);
  const k = (r1 - r0) / (d1 - d0);
  return (v) => r0 + (v - d0) * k;
}

// -- number formatting ----------------------------------------------------------------------

function withMinus(text) {
  return text.replace(/^-/, MINUS);
}

const COMPACT = [
  [1e12, "T"],
  [1e9, "B"],
  [1e6, "M"],
  [1e3, "K"],
];

/**
 * Formatter for one axis: every label gets the same number of decimals (from the step); an
 * axis whose largest tick reaches 10,000 switches to K/M/B/T. `grouping` adds thousands
 * separators to full numbers (off for an x axis, where 1990 is likelier a year than 1,990).
 */
export function tickFormatter(ticks, step, { grouping = true } = {}) {
  const maxAbs = Math.max(...ticks.map((t) => Math.abs(t)));
  let unit = 1;
  let suffix = "";
  if (maxAbs >= 1e4) [unit, suffix] = COMPACT.find(([u]) => maxAbs >= u * (u === 1e3 ? 10 : 1));
  const decimals = stepDecimals(step / unit);
  const nf = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
    useGrouping: grouping,
  });
  return (v) => (v === 0 ? "0" : withMinus(nf.format(v / unit) + suffix));
}

/** Short value for a label on a mark (rounded; the tooltip and table give full precision). */
export function formatValue(v) {
  const a = Math.abs(v);
  if (a === 0) return "0";
  if (a >= 1e4) {
    const [unit, suffix] = COMPACT.find(([u]) => a >= u);
    const nf = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 3 });
    return withMinus(nf.format(v / unit) + suffix);
  }
  const opts = a >= 100 ? { maximumFractionDigits: 0 } : a >= 1 ? { maximumFractionDigits: 2 } : { maximumSignificantDigits: 3 };
  return withMinus(new Intl.NumberFormat("en-US", opts).format(v));
}

/** A value at full precision (shortest round-trip form), for the tooltip and the table. */
export function formatFull(v) {
  return withMinus(String(v));
}

// -- bar geometry ---------------------------------------------------------------------------

/** Smallest band (px per category) that holds `nSeries` bars of MIN_BAR plus gaps and air. */
export function minBandWidth(nSeries) {
  return Math.max(nSeries * MIN_BAR + (nSeries - 1) * BAR_GAP + 4, MIN_LABEL_BAND);
}

/** Plot width for a bar chart: the available width, or wider (then it scrolls) if too narrow. */
export function barPlotWidth(available, nCategories, nSeries) {
  return Math.max(available, nCategories * minBandWidth(nSeries));
}

/**
 * Bars of one category band: `{ barWidth, gap, offsets }`, offsets from the band's left edge.
 * Bars are at most MAX_BAR thick with BAR_GAP between neighbours; the group is centred and at
 * least a fifth of the band (and 4px) is left as air between groups.
 */
export function barGeometry(band, nSeries) {
  if (!Number.isInteger(nSeries) || nSeries < 1) fail(`bar groups need at least one series, got ${nSeries}`);
  if (band < minBandWidth(nSeries)) fail(`a ${band}px band cannot hold ${nSeries} bars`);
  const room = Math.max(band - Math.max(4, band * 0.2), nSeries * MIN_BAR + (nSeries - 1) * BAR_GAP);
  const barWidth = Math.min(MAX_BAR, (room - (nSeries - 1) * BAR_GAP) / nSeries);
  const groupWidth = nSeries * barWidth + (nSeries - 1) * BAR_GAP;
  const start = (band - groupWidth) / 2;
  const offsets = Array.from({ length: nSeries }, (_, i) => start + i * (barWidth + BAR_GAP));
  return { barWidth, gap: BAR_GAP, offsets };
}

const r2 = (n) => Math.round(n * 100) / 100;

/**
 * SVG path for one bar from `base` (the zero line) to `tip` (both y px): rounded corners at the
 * data end, square at the baseline. Empty for a zero-height bar.
 */
export function barPath(x, width, base, tip, radius = BAR_RADIUS) {
  const height = Math.abs(base - tip);
  if (height === 0 || width <= 0) return "";
  const r = r2(Math.min(radius, width / 2, height));
  const [x0, x1, b, t] = [r2(x), r2(x + width), r2(base), r2(tip)];
  if (tip < base) {
    return `M${x0},${b}V${r2(t + r)}A${r},${r} 0 0 1 ${r2(x0 + r)},${t}H${r2(x1 - r)}A${r},${r} 0 0 1 ${x1},${r2(t + r)}V${b}Z`;
  }
  return `M${x0},${b}V${r2(t - r)}A${r},${r} 0 0 0 ${r2(x0 + r)},${t}H${r2(x1 - r)}A${r},${r} 0 0 0 ${x1},${r2(t - r)}V${b}Z`;
}

/** SVG path through `[[x, y], ...]` px points. */
export function linePath(points) {
  return points.map(([x, y], i) => `${i ? "L" : "M"}${r2(x)},${r2(y)}`).join("");
}

/** Marker radius for a line series whose points are `spacing` px apart (0: no markers). */
export function markerRadius(spacing) {
  if (spacing >= 24) return 4;
  if (spacing >= 12) return 3;
  return 0;
}

// -- labels ---------------------------------------------------------------------------------

/** `text`, or its longest prefix plus an ellipsis, that `measure`s at most `maxWidth` px. */
export function truncateToWidth(text, maxWidth, measure) {
  if (measure(text) <= maxWidth) return text;
  const chars = Array.from(text);
  let lo = 0;
  let hi = chars.length - 1;
  let best = "";
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const candidate = `${chars.slice(0, mid).join("").trimEnd()}…`;
    if (measure(candidate) <= maxWidth) {
      best = candidate;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return best;
}

/**
 * How to set category labels under bar bands. `widths`: each label's px width; `band`: px per
 * category; `left`: px from the svg's left edge to the first band. Returns `{ angle, limits,
 * height }`: angle 0 (horizontal) when every label fits its band, else -35 degrees (or -90 when
 * bands are too tight for -35 labels not to overlap); `limits` is each label's max px width
 * (truncate to it); `height` the vertical px the labels need below the axis.
 */
export function categoryLabelLayout({ widths, band, left, lineHeight, maxRotated = 140 }) {
  const widest = Math.max(0, ...widths);
  if (widest + 6 <= band) {
    return { angle: 0, limits: widths.map(() => band - 6), height: lineHeight };
  }
  const steep = band * Math.sin((35 * Math.PI) / 180) < lineHeight + 1;
  const angle = steep ? -90 : -35;
  const rad = (Math.abs(angle) * Math.PI) / 180;
  const cap = steep ? Math.min(maxRotated, 100) : maxRotated;
  const limits = widths.map((_, i) => {
    if (steep) return cap;
    const anchor = left + band * (i + 0.5); // a -35 label runs left from its anchor
    return Math.max(0, Math.min(cap, (anchor - 2) / Math.cos(rad)));
  });
  const used = Math.max(0, ...widths.map((w, i) => Math.min(w, limits[i])));
  return { angle, limits, height: used * Math.sin(rad) + lineHeight * Math.cos(rad) };
}

/** Do two `{ x0, x1, y0, y1 }` rectangles overlap (touching edges do not)? */
export function rectsOverlap(a, b) {
  return a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
}

/**
 * True when every label rect lies inside `bounds`, overlaps no other label and no obstacle
 * except its own (`obstacles[i]` belongs to `labels[i]`; extra obstacles belong to nobody).
 */
export function labelsFit(labels, obstacles, bounds) {
  for (let i = 0; i < labels.length; i++) {
    const a = labels[i];
    if (a.x0 < bounds.x0 || a.x1 > bounds.x1 || a.y0 < bounds.y0 || a.y1 > bounds.y1) return false;
    for (let j = i + 1; j < labels.length; j++) if (rectsOverlap(a, labels[j])) return false;
    for (let j = 0; j < obstacles.length; j++) if (j !== i && rectsOverlap(a, obstacles[j])) return false;
  }
  return true;
}

// -- hover ----------------------------------------------------------------------------------

/** Index of the value in `values` nearest to `v` (-1 for an empty list). */
export function nearestIndex(values, v) {
  let best = -1;
  let bestDist = Infinity;
  values.forEach((value, i) => {
    const d = Math.abs(value - v);
    if (d < bestDist) {
      best = i;
      bestDist = d;
    }
  });
  return best;
}

/** Index of the `[x, y]` point nearest to (x, y) within `radius` px, else -1. */
export function nearestPoint(points, x, y, radius) {
  let best = -1;
  let bestDist = radius * radius;
  points.forEach(([px, py], i) => {
    const d = (px - x) ** 2 + (py - y) ** 2;
    if (d <= bestDist) {
      best = i;
      bestDist = d;
    }
  });
  return best;
}
