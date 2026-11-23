// `chart` block: { kind: line|bar|scatter, title, x_label, y_label, series: [{ name, points:
// [{ x, y }] }], caption (markdown), source }. Hand-written SVG, laid out at the container's real
// width (re-laid out on resize), with a hover/focus tooltip and a "Show data" table twin.
// The arithmetic lives in ../chart_scale.js; this module only builds DOM.

import { h, setChildren } from "../dom.js";
import { renderMarkdown } from "../markdown.js";
import {
  barGeometry,
  barPath,
  barPlotWidth,
  categoryLabelLayout,
  checkChartContent,
  fitTicks,
  formatFull,
  formatValue,
  labelsFit,
  linearScale,
  linePath,
  markerRadius,
  nearestIndex,
  nearestPoint,
  niceTicks,
  tickFormatter,
  ticksWithin,
  truncateToWidth,
  valueExtent,
} from "../chart_scale.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const KIND_NAMES = { line: "Line chart", bar: "Bar chart", scatter: "Scatter plot" };
const KEY_SHAPE = { line: "line", bar: "bar", scatter: "dot" };
const TICK_GAP = 8; // px between tick labels and the plot
const EDGE_PAD = 8; // px inside the plot so edge markers are not cut
const SCATTER_HIT = 32; // px: nearest point within this radius
const SCATTER_R = 4;

let chartCount = 0;
let measureContext = null;

function s(tag, attrs = {}, text) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) el.setAttribute(key, String(value));
  }
  if (text !== undefined) el.textContent = text;
  return el;
}

function seriesColour(index) {
  return `--c: var(--series-${index + 1})`;
}

function key(shape, index) {
  return h("span", { class: `chart-key chart-key-${shape}`, style: seriesColour(index), "aria-hidden": "true" });
}

/** Text measurer for the svg's own font (its CSS size, which no viewBox scaling changes). */
function measurerFor(el) {
  const style = getComputedStyle(el);
  measureContext ??= document.createElement("canvas").getContext("2d");
  const font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
  const fontSize = parseFloat(style.fontSize);
  return {
    // +1px: canvas ignores tabular-nums, which can widen digits slightly.
    measure(text) {
      measureContext.font = font;
      return measureContext.measureText(text).width + 1;
    },
    lineHeight: Math.ceil(fontSize * 1.25),
    fontSize,
  };
}

function ariaLabel(model) {
  const parts = [`${KIND_NAMES[model.kind]}${model.title ? `: ${model.title}` : ""}.`];
  if (model.yLabel) parts.push(`Vertical axis: ${model.yLabel}.`);
  if (model.xLabel) parts.push(`Horizontal axis: ${model.xLabel}.`);
  const names = model.series.map((x) => x.name);
  parts.push(`${names.length === 1 ? "Series" : `${names.length} series`}: ${names.join(", ")}.`);
  parts.push("The values are in the data table below the chart.");
  return parts.join(" ");
}

// -- data table ---------------------------------------------------------------------------

function dataTable(model) {
  const num = (v) => h("td", { class: "num" }, formatFull(v));
  let head;
  let rows;
  if (model.kind === "bar") {
    head = [model.xLabel || "Category", ...model.series.map((x) => x.name)];
    rows = model.categories.map((category) =>
      h(
        "tr",
        {},
        h("th", { scope: "row" }, category),
        model.series.map((x) => {
          const point = x.points.find((p) => p.x === category);
          return point ? num(point.y) : h("td", { class: "num chart-missing" }, h("span", { "aria-label": "no value" }, "–"));
        })
      )
    );
  } else {
    head = ["Series", model.xLabel || "x", model.yLabel || "y"];
    rows = model.series.flatMap((x) =>
      x.points.map((p) => h("tr", {}, h("th", { scope: "row" }, x.name), num(p.x), num(p.y)))
    );
  }
  return h(
    "details",
    { class: "chart-data" },
    h("summary", {}, "Show data"),
    h(
      "div",
      { class: "chart-table-wrap" },
      h(
        "table",
        { class: "chart-table" },
        model.title ? h("caption", { class: "chart-table-caption" }, model.title) : null,
        h("thead", {}, h("tr", {}, head.map((text, i) => h("th", { scope: "col", class: i ? "num" : null }, text)))),
        h("tbody", {}, rows)
      )
    )
  );
}

// -- layout and drawing -------------------------------------------------------------------

/** Lay the chart out at `width` px into `svg`; returns the hover items and their picker. */
function draw(model, svg, width, text) {
  svg.replaceChildren();
  const { measure, lineHeight } = text;
  const kind = model.kind;
  const plotHeight = width < 480 ? 200 : 260;
  const allY = model.series.flatMap((x) => x.points.map((p) => p.y));

  // y axis
  const [yLo, yHi] = valueExtent(kind, allY);
  const yTicks = niceTicks(yLo, yHi, 5);
  const formatY = tickFormatter(yTicks.ticks, yTicks.step);
  const yLabels = yTicks.ticks.map(formatY);
  const left = Math.ceil(Math.max(...yLabels.map(measure))) + TICK_GAP;

  const layers = {
    grid: s("g", { class: "chart-grid-layer" }),
    marks: s("g", { class: "chart-marks" }),
    labels: s("g", { class: "chart-labels" }),
    active: s("g", { class: "chart-active" }),
  };
  const result =
    kind === "bar"
      ? drawBars(model, { width, left, plotHeight, yTicks, measure, lineHeight, layers })
      : drawXY(model, { width, left, plotHeight, yTicks, measure, lineHeight, layers });

  // gridlines and y tick labels (y only)
  const { top, plotWidth, yScale } = result;
  yTicks.ticks.forEach((t, i) => {
    const y = Math.round(yScale(t)) + 0.5;
    const baseline = kind === "bar" && t === 0;
    layers.grid.append(
      s("line", { class: baseline ? "chart-baseline" : "chart-grid", x1: left, x2: left + plotWidth, y1: y, y2: y }),
      s("text", { class: "chart-tick", x: left - TICK_GAP, y, dy: "0.35em", "text-anchor": "end" }, yLabels[i])
    );
  });
  if (kind === "bar" && !yTicks.ticks.includes(0)) fail("bar axis lost its zero line");

  svg.setAttribute("width", result.svgWidth);
  svg.setAttribute("height", result.svgHeight);
  svg.append(layers.grid, layers.marks, layers.labels, layers.active);
  return { ...result, top, active: layers.active };
}

function fail(message) {
  throw new Error(`chart: ${message}`);
}

function drawBars(model, { width, left, plotHeight, yTicks, measure, lineHeight, layers }) {
  const nSeries = model.series.length;
  const categories = model.categories;
  const right = 4;
  const plotWidth = barPlotWidth(width - left - right, categories.length, nSeries);
  const band = plotWidth / categories.length;
  const geom = barGeometry(band, nSeries);

  // category labels: horizontal when they fit, else rotated and truncated
  const widths = categories.map(measure);
  const cat = categoryLabelLayout({ widths, band, left, lineHeight });
  const catLabels = categories.map((c, i) => truncateToWidth(c, cat.limits[i], measure));

  const bars = [];
  model.series.forEach((x, j) => {
    for (const p of x.points) bars.push({ series: j, category: categories.indexOf(p.x), value: p.y });
  });
  const hasNegative = bars.some((b) => b.value < 0);

  // Try with room for value labels at the bar tips; drop them if any would collide.
  const attempt = (withValues) => {
    const pad = withValues ? lineHeight + 2 : 0;
    const top = Math.ceil(lineHeight / 2) + 2;
    const yScale = linearScale(yTicks.min, yTicks.max, top + plotHeight - (withValues && hasNegative ? pad : 0), top + pad);
    const zero = yScale(0);
    const placed = bars.map((b) => {
      const x = left + b.category * band + geom.offsets[b.series];
      return { ...b, x, tip: yScale(b.value), base: zero };
    });
    if (!withValues) return { top, yScale, placed, values: null };
    const values = placed.map((b) => {
      const label = formatValue(b.value);
      const w = measure(label);
      const cx = b.x + geom.barWidth / 2;
      const y0 = b.value < 0 ? b.tip + 3 : b.tip - 3 - lineHeight;
      return { label, cx, y: b.value < 0 ? b.tip + 3 : b.tip - 3, rect: { x0: cx - w / 2 - 1, x1: cx + w / 2 + 1, y0, y1: y0 + lineHeight } };
    });
    const obstacles = placed.map((b) => ({
      x0: b.x,
      x1: b.x + geom.barWidth,
      y0: Math.min(b.tip, b.base),
      y1: Math.max(b.tip, b.base),
    }));
    const bounds = { x0: 0, x1: left + plotWidth + right, y0: 0, y1: top + plotHeight };
    const fits = labelsFit(values.map((v) => v.rect), obstacles, bounds);
    return fits ? { top, yScale, placed, values } : null;
  };
  const laid = attempt(true) || attempt(false);
  const { top, yScale, placed, values } = laid;
  const plotBottom = top + plotHeight;

  for (const b of placed) {
    const d = barPath(b.x, geom.barWidth, b.base, b.tip);
    if (d) layers.marks.append(s("path", { class: "chart-bar", d, style: seriesColour(b.series) }));
  }
  if (values) {
    for (const v of values) {
      layers.labels.append(
        s("text", { class: "chart-value", x: v.cx, y: v.y, dy: v.y > yScale(0) ? "0.8em" : null, "text-anchor": "middle" }, v.label)
      );
    }
  }

  const labelTop = plotBottom + 6;
  categories.forEach((c, i) => {
    const cx = left + band * (i + 0.5);
    let el;
    if (cat.angle === 0) {
      el = s("text", { class: "chart-tick chart-category", x: cx, y: labelTop, dy: "0.8em", "text-anchor": "middle" }, catLabels[i]);
    } else {
      el = s(
        "text",
        {
          class: "chart-tick chart-category",
          x: 0,
          y: 0,
          dy: "0.35em",
          "text-anchor": "end",
          transform: `translate(${cx + 2},${labelTop + 2}) rotate(${cat.angle})`,
        },
        catLabels[i]
      );
    }
    layers.labels.append(el);
  });

  // hover items: one per bar, hit area the bar's slot over the whole plot height
  const items = placed.map((b) => ({
    px: b.x + geom.barWidth / 2,
    py: Math.min(b.tip, b.base),
    bar: b,
    header: categories[b.category],
    rows: [{ key: b.series, label: model.series[b.series].name, value: formatFull(b.value) }],
  }));
  const order = items.map((_, i) => i).sort((a, b) => items[a].bar.category - items[b].bar.category || items[a].bar.series - items[b].bar.series);
  const sorted = order.map((i) => items[i]);
  const half = (geom.barWidth + geom.gap) / 2;
  return {
    top,
    plotWidth,
    yScale,
    svgWidth: left + plotWidth + right,
    svgHeight: Math.ceil(labelTop + 2 + cat.height + 6),
    items: sorted,
    pick(x, y) {
      if (y < top - 8 || y > plotBottom + 8) return -1;
      return sorted.findIndex((it) => Math.abs(x - it.px) <= half);
    },
    highlight(layer, item) {
      const b = item.bar;
      const d = barPath(b.x, geom.barWidth, b.base, b.tip);
      if (d) layer.append(s("path", { class: "chart-bar-active", d }));
      else layer.append(s("line", { class: "chart-bar-active", x1: b.x, x2: b.x + geom.barWidth, y1: b.base, y2: b.base }));
    },
  };
}

function drawXY(model, { width, left, plotHeight, yTicks, measure, lineHeight, layers }) {
  const kind = model.kind;
  const allX = model.series.flatMap((x) => x.points.map((p) => p.x));
  const xMin = Math.min(...allX);
  const xMax = Math.max(...allX);
  const top = Math.ceil(lineHeight / 2) + 2;
  const plotBottom = top + plotHeight;

  const xTicksFor = (length) =>
    fitTicks({
      make: (count) => (kind === "line" ? ticksWithin(xMin, xMax, count) : niceTicks(xMin, xMax, count)),
      label: (set) => set.ticks.map(tickFormatter(set.ticks, set.step, { grouping: false })),
      measure,
      length,
      target: Math.max(2, Math.min(8, Math.floor(length / 60))),
    });
  // Fit once, then make room on the right for half the last label and fit again.
  let right = 8;
  let xTicks = xTicksFor(width - left - right - 2 * EDGE_PAD);
  const formatX = () => tickFormatter(xTicks.ticks, xTicks.step, { grouping: false });
  const lastLabel = xTicks.ticks.length ? formatX()(xTicks.ticks[xTicks.ticks.length - 1]) : "";
  right = Math.max(8, Math.ceil(measure(lastLabel) / 2) - EDGE_PAD + 2);
  xTicks = xTicksFor(width - left - right - 2 * EDGE_PAD);
  const fmtX = formatX();
  const plotWidth = width - left - right;

  const xScale = linearScale(xTicks.min, xTicks.max, left + EDGE_PAD, left + plotWidth - EDGE_PAD);
  const yScale = linearScale(yTicks.min, yTicks.max, plotBottom, top);

  for (const t of xTicks.ticks) {
    layers.labels.append(
      s("text", { class: "chart-tick", x: xScale(t), y: plotBottom + 8, dy: "0.8em", "text-anchor": "middle" }, fmtX(t))
    );
  }

  const shapes = model.series.map((x, j) => {
    const pts = x.points.map((p) => [xScale(p.x), yScale(p.y)]);
    const g = s("g", { style: seriesColour(j) });
    let r = SCATTER_R;
    if (kind === "line") {
      g.append(s("path", { class: "chart-line", d: linePath(pts) }));
      r = markerRadius((plotWidth - 2 * EDGE_PAD) / (pts.length - 1));
    }
    if (r > 0) for (const [cx, cy] of pts) g.append(s("circle", { class: "chart-dot", cx, cy, r: r + 1 }));
    layers.marks.append(g);
    return { pts, r };
  });

  let items;
  let pick;
  if (kind === "line") {
    const xs = [...new Set(allX)].sort((a, b) => a - b);
    items = xs.map((xv) => {
      const rows = [];
      const marks = [];
      model.series.forEach((x, j) => {
        const k = x.points.findIndex((p) => p.x === xv);
        if (k >= 0) {
          rows.push({ key: j, label: x.name, value: formatFull(x.points[k].y) });
          marks.push({ series: j, at: shapes[j].pts[k] });
        }
      });
      return {
        px: xScale(xv),
        py: Math.min(...marks.map((m) => m.at[1])),
        header: `${model.xLabel || "x"}: ${formatFull(xv)}`,
        rows,
        marks,
      };
    });
    const pxs = items.map((it) => it.px);
    pick = (x, y) => (y < top - 16 || y > plotBottom + 16 ? -1 : nearestIndex(pxs, x));
  } else {
    items = [];
    model.series.forEach((x, j) => {
      x.points.forEach((p, k) => {
        const at = shapes[j].pts[k];
        items.push({
          px: at[0],
          py: at[1],
          header: x.name,
          headerKey: j,
          rows: [
            { key: null, label: model.xLabel || "x", value: formatFull(p.x) },
            { key: null, label: model.yLabel || "y", value: formatFull(p.y) },
          ],
          marks: [{ series: j, at }],
        });
      });
    });
    items.sort((a, b) => a.px - b.px || a.py - b.py);
    const pts = items.map((it) => [it.px, it.py]);
    pick = (x, y) => nearestPoint(pts, x, y, SCATTER_HIT);
  }

  return {
    top,
    plotWidth,
    yScale,
    svgWidth: width,
    svgHeight: Math.ceil(plotBottom + 8 + lineHeight + 6),
    items,
    pick,
    highlight(layer, item) {
      if (kind === "line") {
        layer.append(s("line", { class: "chart-crosshair", x1: item.px, x2: item.px, y1: top, y2: plotBottom }));
      }
      for (const m of item.marks) {
        const r = kind === "line" ? 4 : SCATTER_R + 1;
        layer.append(s("circle", { class: "chart-dot chart-dot-active", style: seriesColour(m.series), cx: m.at[0], cy: m.at[1], r: r + 1 }));
      }
    },
  };
}

// -- renderer -----------------------------------------------------------------------------

export function renderChart(block, ctx) {
  const model = checkChartContent(block.content);
  const id = `chart-${++chartCount}`;
  const shape = KEY_SHAPE[model.kind];

  const svg = s("svg", { class: "chart-svg", role: "img", "aria-label": ariaLabel(model) });
  const tooltip = h("div", { class: "chart-tooltip", hidden: true, "aria-live": "polite" });
  const scroller = h("div", { class: "chart-scroll" }, svg);
  const plot = h(
    "div",
    {
      class: "chart-plot",
      tabindex: "0",
      role: "group",
      "aria-label": `${model.title || KIND_NAMES[model.kind]}: use the arrow keys to read values`,
    },
    scroller,
    tooltip
  );

  const figure = h(
    "figure",
    { class: `chart chart-${model.kind}` },
    model.title ? h("figcaption", { class: "chart-title", id: `${id}-title` }, model.title) : null,
    model.series.length >= 2
      ? h(
          "ul",
          { class: "chart-legend", "aria-label": "Legend" },
          model.series.map((x, j) => h("li", {}, key(shape, j), h("span", {}, x.name)))
        )
      : null,
    model.yLabel ? h("div", { class: "chart-axis-label chart-y-label" }, model.yLabel) : null,
    plot,
    model.xLabel ? h("div", { class: "chart-axis-label chart-x-label" }, model.xLabel) : null,
    model.caption.trim() ? withClass(renderMarkdown(model.caption, { baseUrl: ctx && ctx.baseUrl }), "chart-caption") : null,
    model.source.trim() ? h("p", { class: "chart-source" }, `Data: ${model.source}`) : null,
    dataTable(model)
  );

  let layout = null; // the current draw() result
  let activeIndex = -1;
  let lastWidth = 0;

  function hide() {
    activeIndex = -1;
    tooltip.hidden = true;
    if (layout) layout.active.replaceChildren();
    document.removeEventListener("pointerdown", onOutside, true);
  }

  function onOutside(event) {
    if (!plot.contains(event.target)) hide();
  }

  function show(index) {
    if (!layout || index < 0 || index >= layout.items.length) return hide();
    const item = layout.items[index];
    activeIndex = index;
    layout.active.replaceChildren();
    layout.highlight(layout.active, item);

    setChildren(
      tooltip,
      h(
        "div",
        { class: "chart-tooltip-head" },
        item.headerKey !== undefined ? key(shape, item.headerKey) : null,
        h("span", {}, item.header)
      ),
      item.rows.map((row) =>
        h(
          "div",
          { class: "chart-tooltip-row" },
          row.key !== null ? key("line", row.key) : null,
          h("strong", { class: "chart-tooltip-value" }, row.value),
          h("span", { class: "chart-tooltip-label" }, row.label)
        )
      )
    );
    tooltip.hidden = false;

    // Place beside the mark, flipped and clamped so it stays inside the plot wrapper.
    const plotRect = plot.getBoundingClientRect();
    const svgRect = svg.getBoundingClientRect();
    const mx = svgRect.left - plotRect.left + item.px;
    const my = svgRect.top - plotRect.top + item.py;
    const tw = tooltip.offsetWidth;
    const th = tooltip.offsetHeight;
    let x = mx + 12;
    if (x + tw > plot.clientWidth) x = mx - 12 - tw;
    x = Math.max(0, Math.min(x, plot.clientWidth - tw));
    let y = my - th - 10;
    if (y < 0) y = my + 14;
    tooltip.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
    document.addEventListener("pointerdown", onOutside, true);
  }

  function fromPointer(event) {
    if (!layout) return;
    const r = svg.getBoundingClientRect();
    const index = layout.pick(event.clientX - r.left, event.clientY - r.top);
    if (index !== activeIndex) (index < 0 ? hide : show)(index);
  }

  plot.addEventListener("pointermove", fromPointer);
  plot.addEventListener("pointerdown", fromPointer);
  plot.addEventListener("pointerleave", (event) => {
    if (event.pointerType !== "touch") hide();
  });
  plot.addEventListener("focus", () => {
    // Keyboard focus shows the first value; a click's own pointerdown picks the mark instead.
    if (activeIndex < 0 && plot.matches(":focus-visible")) show(0);
  });
  plot.addEventListener("blur", hide);
  plot.addEventListener("keydown", (event) => {
    if (!layout || !layout.items.length) return;
    const last = layout.items.length - 1;
    const moves = {
      ArrowRight: activeIndex + 1,
      ArrowDown: activeIndex + 1,
      ArrowLeft: activeIndex - 1,
      ArrowUp: activeIndex - 1,
      Home: 0,
      End: last,
    };
    if (event.key === "Escape") {
      hide();
      return;
    }
    if (!(event.key in moves)) return;
    event.preventDefault();
    show(Math.max(0, Math.min(last, moves[event.key])));
  });

  // The renderer runs before the figure is attached: lay out when a real width is reported.
  // Drawing changes the scroller's height, so it is done in the next frame, not inside the
  // observer's callback: a size change made there is reported by the browser as a
  // "ResizeObserver loop" error.
  let pending = 0;
  const observer = new ResizeObserver((entries) => {
    const width = Math.floor(entries[entries.length - 1].contentRect.width);
    if (width <= 0 || width === lastWidth) return;
    lastWidth = width;
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(() => redraw(width));
  });
  const redraw = (width) => {
    hide();
    try {
      layout = draw(model, svg, width, measurerFor(svg));
    } catch (err) {
      // Layout runs after renderBlock returned, outside the registry's error handling: show it here.
      console.error(err);
      observer.disconnect();
      layout = null;
      plot.replaceWith(
        h("div", { class: "notice notice-error", role: "alert" }, `Could not draw this chart: ${err.message}`)
      );
    }
  };
  observer.observe(scroller);

  return figure;
}

function withClass(el, name) {
  el.classList.add(name);
  return el;
}
