// Run with `node chart_test.mjs`: the chart block's pure helpers (js/chart_scale.js) - contract
// checking, ticks, number formats, category order, bar geometry and label fitting.

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const S = await import(new URL("js/chart_scale.js", STATIC));

// -- ticks ------------------------------------------------------------------------------------

function assertClean(set, { min, max }) {
  const { ticks, step } = set;
  assert.ok(ticks.length >= 2, JSON.stringify(set));
  assert.ok(ticks[0] <= min && ticks[ticks.length - 1] >= max, `ticks ${ticks} do not cover ${min}..${max}`);
  const exponent = Math.floor(Math.log10(step));
  const mantissa = Math.round(step / 10 ** exponent);
  assert.ok([1, 2, 5].includes(mantissa), `step ${step} is not 1/2/5 x 10^n`);
  for (let i = 1; i < ticks.length; i++) {
    assert.ok(Math.abs(ticks[i] - ticks[i - 1] - step) <= step * 1e-9, `uneven ticks ${ticks}`);
  }
  // Exact decimals: every tick prints with at most the step's decimals.
  const decimals = S.stepDecimals(step);
  for (const t of ticks) assert.equal(Number(t.toFixed(decimals)), t, `tick ${t} is not exact`);
}

{
  const typical = S.niceTicks(0, 97, 5);
  assertClean(typical, { min: 0, max: 97 });
  assert.deepEqual(typical.ticks, [0, 20, 40, 60, 80, 100]);

  const negative = S.niceTicks(-6.8, 7.1, 5);
  assertClean(negative, { min: -6.8, max: 7.1 });
  assert.ok(negative.ticks.includes(0));
  assert.ok(negative.ticks.length >= 4 && negative.ticks.length <= 8, `${negative.ticks}`);

  const tiny = S.niceTicks(0.00089, 0.00119, 5);
  assertClean(tiny, { min: 0.00089, max: 0.00119 });
  assert.deepEqual(tiny.ticks, [0.0008, 0.0009, 0.001, 0.0011, 0.0012]);

  const huge = S.niceTicks(1.2e12, 9.7e12, 5);
  assertClean(huge, { min: 1.2e12, max: 9.7e12 });
  assert.equal(huge.step, 2e12);

  // 0.1 + 0.2 style drift never leaks into tick values.
  assert.deepEqual(S.niceTicks(0.1, 0.7, 6).ticks, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]);

  // Zero-width ranges are widened, not divided by zero.
  const zero = S.niceTicks(0, 0, 5);
  assertClean(zero, { min: -1, max: 1 });
  const flat = S.niceTicks(42, 42, 5);
  assertClean(flat, { min: 42, max: 42 });
  assert.ok(flat.min < 42 && flat.max > 42);
  const flatNegative = S.niceTicks(-3, -3, 5);
  assert.ok(flatNegative.min < -3 && flatNegative.max > -3);

  // No -0 among ticks.
  assert.ok(S.niceTicks(-1, 1, 4).ticks.every((t) => !Object.is(t, -0)));

  // Ticks inside a fixed (line x) domain.
  assert.deepEqual(S.ticksWithin(1990, 2019, 3).ticks, [1990, 2000, 2010]);
  assert.deepEqual(S.ticksWithin(1, 8, 7).ticks, [1, 2, 3, 4, 5, 6, 7, 8]);

  assert.throws(() => S.niceTicks(NaN, 1), /finite/);
  assert.throws(() => S.niceTicks(2, 1), /reversed/);

  // Fitting tick labels to a pixel length: fewer ticks when labels would collide.
  const measure = (text) => text.length * 7;
  const make = (count) => S.niceTicks(0, 1000, count);
  const label = (set) => set.ticks.map(S.tickFormatter(set.ticks, set.step));
  const wide = S.fitTicks({ make, label, measure, length: 600, target: 8 });
  const narrow = S.fitTicks({ make, label, measure, length: 120, target: 8 });
  assert.ok(narrow.ticks.length < wide.ticks.length, `${narrow.ticks} vs ${wide.ticks}`);
  const spacing = (narrow.step / (narrow.max - narrow.min)) * 120;
  assert.ok(Math.max(...label(narrow).map(measure)) + 8 <= spacing);
}

// -- domains ----------------------------------------------------------------------------------

{
  assert.deepEqual(S.valueExtent("bar", [3, 9]), [0, 9]); // bars include zero
  assert.deepEqual(S.valueExtent("bar", [-4, -1]), [-4, 0]);
  assert.deepEqual(S.valueExtent("bar", [0, 0]), [0, 1]);
  assert.deepEqual(S.valueExtent("line", [3, 9]), [3, 9]); // lines do not
  const scale = S.linearScale(0, 10, 200, 0);
  assert.equal(scale(0), 200);
  assert.equal(scale(5), 100);
  assert.throws(() => S.linearScale(1, 1, 0, 10), /zero-width/);
}

// -- formatting -------------------------------------------------------------------------------

{
  const fmt = (ticks, opts) => {
    const step = ticks[1] - ticks[0];
    return ticks.map(S.tickFormatter(ticks, step, opts));
  };
  assert.deepEqual(fmt([0, 2000, 4000, 6000, 8000]), ["0", "2,000", "4,000", "6,000", "8,000"]);
  assert.deepEqual(fmt([0, 10000, 20000]), ["0", "10K", "20K"]);
  assert.deepEqual(fmt([0, 5000, 10000, 15000]), ["0", "5K", "10K", "15K"]);
  assert.deepEqual(fmt([0, 500000, 1000000, 1500000]), ["0", "0.5M", "1.0M", "1.5M"]);
  assert.deepEqual(fmt([0, 2e9, 4e9]), ["0", "2B", "4B"]);
  assert.deepEqual(fmt([0, 0.5, 1, 1.5]), ["0", "0.5", "1.0", "1.5"]);
  assert.deepEqual(fmt([0.0008, 0.0009, 0.001]), ["0.0008", "0.0009", "0.0010"]);
  assert.deepEqual(fmt([-10, -5, 0, 5]), ["−10", "−5", "0", "5"]);
  // x axes: no grouping (years), still compact from 10,000.
  assert.deepEqual(fmt([1990, 2000, 2010], { grouping: false }), ["1990", "2000", "2010"]);
  assert.deepEqual(fmt([0, 20000, 40000], { grouping: false }), ["0", "20K", "40K"]);

  assert.equal(S.formatValue(6.5), "6.5");
  assert.equal(S.formatValue(4.25), "4.25");
  assert.equal(S.formatValue(1234.5), "1,235");
  assert.equal(S.formatValue(12345), "12.3K");
  assert.equal(S.formatValue(2100000), "2.1M");
  assert.equal(S.formatValue(0.0012345), "0.00123");
  assert.equal(S.formatValue(-3.2), "−3.2");
  assert.equal(S.formatValue(0), "0");
  assert.equal(S.formatFull(0.0009293651004379268), "0.0009293651004379268");
  assert.equal(S.formatFull(-1250000.5), "−1250000.5");

  assert.equal(S.stepDecimals(5), 0);
  assert.equal(S.stepDecimals(0.5), 1);
  assert.equal(S.stepDecimals(0.0002), 4);
  assert.equal(S.stepDecimals(2e-7), 7);
}

// -- contract ---------------------------------------------------------------------------------

const base = { title: "T", x_label: "X", y_label: "Y", caption: "", source: "" };
const bar = (series) => ({ ...base, kind: "bar", series });
const line = (series) => ({ ...base, kind: "line", series });
const scatter = (series) => ({ ...base, kind: "scatter", series });

{
  // Category order: first appearance across series; a missing category stays missing.
  const model = S.checkChartContent(
    bar([
      { name: "A", points: [{ x: "Jan", y: 1 }, { x: "Mar", y: 3 }] },
      { name: "B", points: [{ x: "Feb", y: 2 }, { x: "Jan", y: 4 }, { x: "Apr", y: -1 }] },
    ])
  );
  assert.deepEqual(model.categories, ["Jan", "Mar", "Feb", "Apr"]);
  assert.equal(model.series[0].points.length, 2); // no zero filled in for Feb/Apr
  assert.equal(model.xLabel, "X");

  // Lines come back sorted by x; the input is not mutated.
  const input = line([{ name: "L", points: [{ x: 3, y: 1 }, { x: 1, y: 2 }, { x: 2, y: 3 }] }]);
  const sorted = S.checkChartContent(input);
  assert.deepEqual(sorted.series[0].points.map((p) => p.x), [1, 2, 3]);
  assert.equal(input.series[0].points[0].x, 3);

  // Scatter allows repeated x and a single point.
  S.checkChartContent(scatter([{ name: "S", points: [{ x: 1, y: 1 }, { x: 1, y: 2 }] }]));
  S.checkChartContent(scatter([{ name: "S", points: [{ x: 3, y: 42 }] }]));

  const bad = [
    [null, /object/],
    [{ ...base, kind: "pie", series: [] }, /unknown kind/],
    [{ ...line([{ name: "L", points: [{ x: 1, y: 1 }, { x: 2, y: 2 }] }]), title: 3 }, /title must be a string/],
    [line([{ name: "L", points: [{ x: "a", y: 1 }, { x: 2, y: 2 }] }]), /x must be a finite number, got "a"/],
    [line([{ name: "L", points: [{ x: 1, y: 1 }] }]), /at least 2 points/],
    [line([{ name: "L", points: [{ x: 1, y: 1 }, { x: 1, y: 2 }] }]), /appears twice/],
    [line([{ name: "L", points: [{ x: 1, y: NaN }, { x: 2, y: 2 }] }]), /y must be a finite number/],
    [line([{ name: "L", points: [{ x: 1, y: "3" }, { x: 2, y: 2 }] }]), /y must be a finite number/],
    [bar([{ name: "B", points: [{ x: 1, y: 1 }] }]), /non-blank category/],
    [bar([{ name: "B", points: [{ x: "  ", y: 1 }] }]), /non-blank category/],
    [bar([{ name: "B", points: [{ x: "a", y: 1 }, { x: "a", y: 2 }] }]), /appears twice/],
    [bar([{ name: "B", points: [{ x: "a", y: 1 }] }, { name: "B", points: [{ x: "a", y: 1 }] }]), /used twice/],
    [bar([]), /1 to 6 series/],
    [bar(Array.from({ length: 7 }, (_, i) => ({ name: `s${i}`, points: [{ x: "a", y: 1 }] }))), /1 to 6 series/],
    [scatter(Array.from({ length: 4 }, (_, i) => ({ name: `s${i}`, points: [{ x: 1, y: 1 }] }))), /1 to 3 series/],
    [bar([{ name: "B", points: Array.from({ length: 25 }, (_, i) => ({ x: `c${i}`, y: i })) }]), /at most 24 categories/],
    [bar([{ name: "B", points: [] }]), /1 to 60 points/],
    [scatter([{ name: "S", points: Array.from({ length: 61 }, (_, i) => ({ x: i, y: i })) }]), /1 to 60 points/],
    [bar([{ name: "", points: [{ x: "a", y: 1 }] }]), /non-blank name/],
  ];
  for (const [content, message] of bad) assert.throws(() => S.checkChartContent(content), message);
}

// -- bar geometry -----------------------------------------------------------------------------

{
  for (const nSeries of [1, 2, 3, 4, 5, 6]) {
    for (const band of [S.minBandWidth(nSeries), 20, 33.3, 48, 64, 100, 250, 600]) {
      if (band < S.minBandWidth(nSeries)) continue;
      const g = S.barGeometry(band, nSeries);
      assert.ok(g.barWidth <= S.MAX_BAR, `bar ${g.barWidth}px > 24px`);
      assert.ok(g.barWidth >= S.MIN_BAR - 1e-9, `bar ${g.barWidth}px too thin`);
      assert.equal(g.offsets.length, nSeries);
      assert.ok(g.offsets[0] >= 0, "group starts outside its band");
      assert.ok(g.offsets[nSeries - 1] + g.barWidth <= band + 1e-9, "group ends outside its band");
      for (let i = 1; i < nSeries; i++) {
        const between = g.offsets[i] - (g.offsets[i - 1] + g.barWidth);
        assert.ok(Math.abs(between - S.BAR_GAP) < 1e-9, `gap ${between}px, expected 2px`);
      }
    }
  }
  assert.throws(() => S.barGeometry(5, 6), /cannot hold/);
  // Too narrow a container: the plot widens instead of squeezing bars.
  assert.equal(S.barPlotWidth(200, 24, 6), 24 * S.minBandWidth(6));
  assert.equal(S.barPlotWidth(800, 4, 2), 800);

  // Rounded data end, square baseline; upward and downward; zero height draws nothing.
  const up = S.barPath(10, 20, 100, 40);
  assert.match(up, /^M10,100V44A4,4 0 0 1 14,40H26A4,4 0 0 1 30,44V100Z$/);
  const down = S.barPath(10, 20, 100, 160);
  assert.match(down, /^M10,100V156A4,4 0 0 0 14,160H26A4,4 0 0 0 30,156V100Z$/);
  assert.equal(S.barPath(10, 20, 100, 100), "");
  assert.match(S.barPath(0, 4, 100, 99), /A1,1 /); // radius limited by height
  assert.equal(S.linePath([[0, 1], [2.345, 3]]), "M0,1L2.35,3");

  assert.equal(S.markerRadius(40), 4);
  assert.equal(S.markerRadius(15), 3);
  assert.equal(S.markerRadius(5), 0);
}

// -- labels -----------------------------------------------------------------------------------

{
  const measure = (text) => Array.from(text).length * 7;
  assert.equal(S.truncateToWidth("Short", 100, measure), "Short");
  const cut = S.truncateToWidth("A very long category name indeed", 70, measure);
  assert.ok(cut.endsWith("…") && measure(cut) <= 70, cut);
  assert.ok("A very long category name indeed".startsWith(cut.slice(0, -1)));
  assert.equal(S.truncateToWidth("Anything", 3, measure), "");

  const flat = S.categoryLabelLayout({ widths: [30, 40], band: 80, left: 40, lineHeight: 15 });
  assert.equal(flat.angle, 0);
  const tilted = S.categoryLabelLayout({ widths: [300, 40, 90], band: 60, left: 30, lineHeight: 15 });
  assert.equal(tilted.angle, -35);
  assert.ok(tilted.limits.every((l) => l <= 140));
  // The first label is cut so it does not run off the left edge.
  assert.ok(tilted.limits[0] * Math.cos((35 * Math.PI) / 180) <= 30 + 30);
  assert.ok(tilted.height > 15);
  const steep = S.categoryLabelLayout({ widths: [60, 60], band: 20, left: 30, lineHeight: 15 });
  assert.equal(steep.angle, -90);

  const r = (x0, x1, y0, y1) => ({ x0, x1, y0, y1 });
  assert.ok(S.rectsOverlap(r(0, 10, 0, 10), r(5, 15, 5, 15)));
  assert.ok(!S.rectsOverlap(r(0, 10, 0, 10), r(10, 20, 0, 10)));
  const bounds = r(0, 100, 0, 100);
  assert.ok(S.labelsFit([r(0, 10, 0, 10), r(20, 30, 0, 10)], [r(0, 10, 10, 50), r(20, 30, 10, 50)], bounds));
  assert.ok(!S.labelsFit([r(0, 25, 0, 10), r(20, 30, 0, 10)], [], bounds)); // labels collide
  assert.ok(!S.labelsFit([r(0, 25, 0, 10)], [r(0, 5, 0, 1), r(20, 30, 5, 50)], bounds)); // hits a bar
  assert.ok(!S.labelsFit([r(-5, 5, 0, 10)], [], bounds)); // outside
}

// -- hover ------------------------------------------------------------------------------------

{
  assert.equal(S.nearestIndex([10, 20, 30], 24), 1);
  assert.equal(S.nearestIndex([], 24), -1);
  assert.equal(S.nearestPoint([[0, 0], [10, 10]], 8, 9, 5), 1);
  assert.equal(S.nearestPoint([[0, 0], [10, 10]], 50, 50, 5), -1);
}

console.log("chart ok");
