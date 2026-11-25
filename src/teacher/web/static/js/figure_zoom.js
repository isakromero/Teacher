// Zooming and panning of the enlarged figure (blocks/figure.js).
//
// The image sits in a scrolling frame. Its size is `scale` times its own pixel size; the
// smallest scale is "fit" (the whole image inside the frame) and that is where it opens.
// Ways to zoom, all anchored at the point under the pointer (or the frame's centre):
//   - the bar's buttons: zoom out, zoom in, and the percentage (back to fit);
//   - a click or tap on the image: in to twice fit, and from anywhere closer back to fit;
//   - pinching with two fingers, and a trackpad pinch or Ctrl/Cmd + wheel;
//   - the keys + and - and 0 while the dialog is open.
// Panning: the frame scrolls (wheel, one finger, arrow keys once it has focus), and a mouse
// drags it. A drag is not a click, so it does not zoom.
//
// The functions down to `anchoredScroll` are pure and tested under node
// (tests/web/js/figure_zoom_test.mjs); `mountFigureZoom` is the DOM part.

/** Each press of a zoom button multiplies or divides the scale by this. */
export const STEP = 1.5;
/** What a click on the fitted image zooms to, as a multiple of the fit scale. */
export const CLICK_ZOOM = 2;
/** A pointer that moved further than this (px) between press and release dragged, not clicked. */
export const DRAG_SLOP = 4;

function positive(value, name) {
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`figure zoom: ${name} must be a positive number, got ${value}`);
  }
  return value;
}

/** The scale at which the whole image fits a frame of the given inner size. */
export function fitScale(image, frame) {
  positive(image.width, "image width");
  positive(image.height, "image height");
  positive(frame.width, "frame width");
  positive(frame.height, "frame height");
  return Math.min(frame.width / image.width, frame.height / image.height);
}

/** The largest scale allowed: four times the image's own pixels, and at least eight times fit
 * (so a large image that fits at a small scale can still be read closely). */
export function maxScale(fit) {
  return Math.max(4, positive(fit, "fit scale") * 8);
}

/** `scale` held between fit and the maximum. */
export function clampScale(scale, fit) {
  if (!Number.isFinite(scale)) throw new Error(`figure zoom: scale must be a number, got ${scale}`);
  return Math.min(maxScale(fit), Math.max(fit, scale));
}

/** One button press in (`direction` 1) or out (-1) from `scale`. */
export function stepScale(scale, fit, direction) {
  if (direction !== 1 && direction !== -1) {
    throw new Error(`figure zoom: direction must be 1 or -1, got ${direction}`);
  }
  return clampScale(direction === 1 ? scale * STEP : scale / STEP, fit);
}

/** Whether `scale` is the fit scale (to within rounding). */
export function isFit(scale, fit) {
  return Math.abs(scale - fit) <= fit * 1e-6;
}

/** What a click on the image zooms to: in from fit, back to fit from anywhere closer. */
export function clickScale(scale, fit) {
  return isFit(scale, fit) ? clampScale(fit * CLICK_ZOOM, fit) : fit;
}

/** The label of the percentage button: the scale relative to fit ("100%" is the whole image). */
export function percentLabel(scale, fit) {
  return `${Math.round((scale / positive(fit, "fit scale")) * 100)}%`;
}

/** How far to scroll (dx, dy) so the image point that was under `anchor` stays under it.
 * `before` and `after` are the image's rectangles (left, top, width, height) in the same
 * coordinates as `anchor`, before and after the resize and with the scroll unchanged. */
export function anchoredScroll(anchor, before, after) {
  const rx = (anchor.x - before.left) / positive(before.width, "image width before");
  const ry = (anchor.y - before.top) / positive(before.height, "image height before");
  return {
    dx: after.left + rx * after.width - anchor.x,
    dy: after.top + ry * after.height - anchor.y,
  };
}

/** Wire zooming of `image` (own pixel size `size`) inside the scrolling `frame` of `dialog`.
 * `controls` are the bar's buttons: { zoomIn, zoomOut, reset }. Returns { refit } to call when
 * the dialog opens (the frame has no size before that). */
export function mountFigureZoom({ dialog, frame, image, size, controls }) {
  let fit = null;
  let scale = null;

  const frameSize = () => {
    const style = getComputedStyle(frame);
    const width = frame.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const height =
      frame.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
    return { width, height };
  };
  const centre = () => {
    const r = frame.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  };

  function show() {
    image.style.width = `${size.width * scale}px`;
    image.style.height = `${size.height * scale}px`;
    const fitted = isFit(scale, fit);
    frame.classList.toggle("is-zoomed", !fitted);
    controls.reset.textContent = percentLabel(scale, fit);
    controls.reset.setAttribute(
      "aria-label",
      fitted ? "Zoom: 100%, the whole figure" : `Zoom: ${percentLabel(scale, fit)}. Show the whole figure`
    );
    controls.zoomOut.disabled = fitted;
    controls.reset.disabled = fitted;
    controls.zoomIn.disabled = scale >= maxScale(fit);
  }

  function zoomTo(next, anchor = centre()) {
    const target = clampScale(next, fit);
    if (target === scale) return;
    const before = image.getBoundingClientRect();
    scale = target;
    show();
    const after = image.getBoundingClientRect();
    const { dx, dy } = anchoredScroll(anchor, before, after);
    frame.scrollLeft += dx;
    frame.scrollTop += dy;
  }

  /** Measure the frame again; an image that was at fit stays at fit. */
  function refit({ reset = false } = {}) {
    const inner = frameSize();
    if (inner.width <= 0 || inner.height <= 0) return; // the dialog is closed: nothing to fit
    const wasFit = reset || scale === null || isFit(scale, fit);
    fit = fitScale(size, inner);
    scale = wasFit ? fit : clampScale(scale, fit);
    show();
  }

  controls.zoomIn.addEventListener("click", () => zoomTo(stepScale(scale, fit, 1)));
  controls.zoomOut.addEventListener("click", () => zoomTo(stepScale(scale, fit, -1)));
  controls.reset.addEventListener("click", () => zoomTo(fit));

  // A mouse drags the zoomed image; a press that did not move is a click, which zooms.
  let drag = null;
  let dragged = false;
  image.addEventListener("pointerdown", (event) => {
    dragged = false;
    if (event.pointerType !== "mouse" || event.button !== 0) return;
    drag = { x: event.clientX, y: event.clientY, left: frame.scrollLeft, top: frame.scrollTop };
    image.setPointerCapture(event.pointerId);
    event.preventDefault(); // no native image drag
  });
  image.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (Math.hypot(dx, dy) > DRAG_SLOP) dragged = true;
    if (!dragged) return;
    frame.classList.add("is-dragging");
    frame.scrollLeft = drag.left - dx;
    frame.scrollTop = drag.top - dy;
  });
  const endDrag = () => {
    drag = null;
    frame.classList.remove("is-dragging");
  };
  image.addEventListener("pointerup", endDrag);
  image.addEventListener("pointercancel", endDrag);
  image.addEventListener("click", (event) => {
    if (dragged) return;
    zoomTo(clickScale(scale, fit), { x: event.clientX, y: event.clientY });
  });

  // Trackpad pinch arrives as a wheel event with ctrlKey set; Ctrl/Cmd + wheel zooms too. A
  // plain wheel scrolls the frame.
  frame.addEventListener(
    "wheel",
    (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      zoomTo(scale * Math.exp(-event.deltaY / 100), { x: event.clientX, y: event.clientY });
    },
    { passive: false }
  );

  // Two fingers pinch; one finger scrolls the frame natively.
  let pinch = null;
  const spread = (touches) =>
    Math.hypot(touches[0].clientX - touches[1].clientX, touches[0].clientY - touches[1].clientY);
  const middle = (touches) => ({
    x: (touches[0].clientX + touches[1].clientX) / 2,
    y: (touches[0].clientY + touches[1].clientY) / 2,
  });
  frame.addEventListener(
    "touchstart",
    (event) => {
      if (event.touches.length !== 2) return;
      event.preventDefault();
      pinch = { distance: spread(event.touches), scale };
    },
    { passive: false }
  );
  frame.addEventListener(
    "touchmove",
    (event) => {
      if (!pinch || event.touches.length !== 2) return;
      event.preventDefault();
      const distance = spread(event.touches);
      if (pinch.distance > 0) zoomTo((pinch.scale * distance) / pinch.distance, middle(event.touches));
    },
    { passive: false }
  );
  const endPinch = (event) => {
    if (event.touches.length < 2) pinch = null;
  };
  frame.addEventListener("touchend", endPinch);
  frame.addEventListener("touchcancel", endPinch);

  dialog.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "+" || event.key === "=") zoomTo(stepScale(scale, fit, 1));
    else if (event.key === "-" || event.key === "_") zoomTo(stepScale(scale, fit, -1));
    else if (event.key === "0") zoomTo(fit);
    else return;
    event.preventDefault();
  });

  new ResizeObserver(() => {
    if (dialog.open) refit();
  }).observe(frame);

  return { refit };
}
