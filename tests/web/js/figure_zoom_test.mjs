// Run with `node figure_zoom_test.mjs`: the pure half of the enlarged figure's zoom (fit, the
// limits, button steps, what a click does, and keeping the point under the pointer in place).

import assert from "node:assert/strict";

const STATIC = new URL("../../../src/teacher/web/static/", import.meta.url);
const z = await import(new URL("js/figure_zoom.js", STATIC));

// Fit: the whole image inside the frame, limited by whichever side is tighter.
assert.equal(z.fitScale({ width: 1000, height: 500 }, { width: 500, height: 500 }), 0.5);
assert.equal(z.fitScale({ width: 500, height: 1000 }, { width: 500, height: 500 }), 0.5);
// A small image is enlarged to fill the frame.
assert.equal(z.fitScale({ width: 300, height: 200 }, { width: 1200, height: 900 }), 4);
assert.throws(() => z.fitScale({ width: 0, height: 5 }, { width: 5, height: 5 }), /image width/);
assert.throws(() => z.fitScale({ width: 5, height: 5 }, { width: 5, height: NaN }), /frame height/);

// Limits: never below fit; up to 4x the image's pixels, or 8x fit when that is more.
assert.equal(z.maxScale(0.25), 4);
assert.equal(z.maxScale(2), 16);
assert.equal(z.clampScale(0.1, 0.5), 0.5);
assert.equal(z.clampScale(99, 0.5), 4);
assert.equal(z.clampScale(1, 0.5), 1);
assert.throws(() => z.clampScale(NaN, 0.5), /scale must be a number/);

// Steps multiply and divide, and stop at the limits.
assert.equal(z.stepScale(1, 0.5, 1), 1.5);
assert.equal(z.stepScale(1.5, 0.5, -1), 1);
assert.equal(z.stepScale(0.6, 0.5, -1), 0.5);
assert.equal(z.stepScale(3.9, 0.5, 1), 4);
assert.throws(() => z.stepScale(1, 0.5, 0), /direction/);

// A click zooms in from fit and back to fit from anywhere closer.
assert.equal(z.clickScale(0.5, 0.5), 1);
assert.equal(z.clickScale(1, 0.5), 0.5);
assert.equal(z.clickScale(3, 0.5), 0.5);
assert.ok(z.isFit(0.5 + 1e-9, 0.5));
assert.ok(!z.isFit(0.51, 0.5));

assert.equal(z.percentLabel(0.5, 0.5), "100%");
assert.equal(z.percentLabel(0.75, 0.5), "150%");

// The point under the anchor stays under it: an image at (100, 50) 200x100 doubles in place
// (same left/top before scrolling); the anchor at its centre needs the scroll to move by half
// the growth.
assert.deepEqual(
  z.anchoredScroll(
    { x: 200, y: 100 },
    { left: 100, top: 50, width: 200, height: 100 },
    { left: 100, top: 50, width: 400, height: 200 }
  ),
  { dx: 100, dy: 50 }
);
// Anchored at the image's top-left corner nothing moves.
assert.deepEqual(
  z.anchoredScroll(
    { x: 100, y: 50 },
    { left: 100, top: 50, width: 200, height: 100 },
    { left: 100, top: 50, width: 400, height: 200 }
  ),
  { dx: 0, dy: 0 }
);
// A centred image that grows past the frame moves left (its new left is smaller).
assert.deepEqual(
  z.anchoredScroll(
    { x: 300, y: 200 },
    { left: 200, top: 150, width: 200, height: 100 },
    { left: 0, top: 100, width: 600, height: 300 }
  ),
  { dx: 0, dy: 50 }
);

console.log("figure_zoom_test ok");
